"""Button entities for RLTech FTTR."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    AccountBusyError,
    AccountLockedError,
    ApRebootRejected,
    AuthenticationError,
    RltechError,
    error_kind,
)
from .const import DOMAIN
from .coordinator import RltechConfigEntry, RltechCoordinator
from .entity import RltechEntity, ap_device_info, controller_device_info
from .identifiers import (
    ap_sensor_object_id,
    ap_sensor_unique_id,
    controller_sensor_object_id,
)
from .models import REBOOT_STATUS_STATES, RltechAp
from .settings import entry_host, object_id_host
from .sources import SOURCE_WEB, STATE_OK, STATE_UNREACHABLE

_LOGGER = logging.getLogger(__name__)

# One press at a time per platform; each press is one short 8080 burst.
PARALLEL_UPDATES = 1

_REJECTED_KEYS = {
    "offline": "ap_reboot_offline",
    "not_listed": "ap_reboot_not_listed",
    "task_pending": "ap_reboot_task_pending",
    "slow": "ap_reboot_slow",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RltechConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the RLTech FTTR AC and AP reboot buttons."""
    coordinator = entry.runtime_data
    async_add_entities([RltechAcRebootButton(entry, coordinator)])
    known_aps: set[str] = set()

    @callback
    def add_dynamic_entities() -> None:
        if coordinator.data is None:
            return
        new_entities: list[RltechApRebootButton] = []
        for sn in coordinator.data.aps_by_sn:
            if sn in known_aps:
                continue
            known_aps.add(sn)
            new_entities.append(RltechApRebootButton(entry, coordinator, sn))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        coordinator.device_manager.async_add_ap_removed_listener(known_aps.discard)
    )
    add_dynamic_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_dynamic_entities))


class RltechApRebootButton(RltechEntity, ButtonEntity):
    """Reboot one managed AP through the AC (XAdd_Task, stage 6).

    Availability: the AP must be online in the latest AP list. Outside a
    pause the 8080 source must also be ok (a press needs the Web UI login).
    While 8080 polling is paused the button stays available on the last
    known state: a press is a deliberate, one-off login that re-checks the
    AP on the AC before submitting (review S3).
    """

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "ap_reboot"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        sn: str,
    ) -> None:
        super().__init__(entry, coordinator)
        self._sn = sn
        ap = self._ap
        unique_id = ap_sensor_unique_id(entry.entry_id, ap, "reboot")
        if unique_id is None:
            raise ValueError(f"AP {sn} is not reported")
        self._attr_unique_id = unique_id
        object_id = ap_sensor_object_id(ap, "reboot")
        if object_id is None:
            raise ValueError(f"AP {sn} is not reported")
        self.entity_id = f"button.{object_id}"

    @property
    def available(self) -> bool:
        coordinator = self.coordinator
        ap = self._ap
        if ap is None or ap.online is not True or not coordinator.has_web_baseline:
            return False
        if coordinator.web_polling_paused():
            return True
        return (
            coordinator.last_update_success
            and self._source_state(SOURCE_WEB) == STATE_OK
        )

    @property
    def device_info(self):
        return ap_device_info(self.config_entry, self._sn)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ap = self._ap
        record = (
            self.coordinator.client.reboot_records.get(ap.mac) if ap is not None else None
        )
        reboot_status = record.reboot_status if record is not None else None
        data = self.coordinator.data
        task = data.ap_tasks.get(ap.mac) if data is not None and ap is not None else None
        polled_after_request = (
            record is None
            or (
                data is not None
                and data.last_success_8080 is not None
                and data.last_success_8080 >= record.requested_at
            )
        )
        if task is not None and task.action == "2" and polled_after_request:
            # The AP list read after the request carries the newer status
            # (0 queued -> 3 rebooting -> 1 completed).
            reboot_status = task.reboot_status
        return {
            "last_reboot_requested": record.requested_at.isoformat()
            if record is not None
            else None,
            "last_reboot_result": record.result if record is not None else None,
            "reboot_status": reboot_status,
            "reboot_state": REBOOT_STATUS_STATES.get(reboot_status, "unknown")
            if reboot_status is not None
            else None,
        }

    async def async_press(self) -> None:
        """Ask the AC to reboot the AP."""
        ap = self._ap
        if ap is None or ap.online is not True:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ap_reboot_offline"
            )
        session = async_get_clientsession(self.coordinator.hass)
        placeholders = {"host": entry_host(self.config_entry)}
        try:
            await self.coordinator.client.reboot_ap_via_ac(session, ap)
        except ApRebootRejected as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=_REJECTED_KEYS.get(err.reason, "ap_reboot_failed"),
                translation_placeholders=placeholders,
            ) from err
        except AccountBusyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_ui_busy",
                translation_placeholders=placeholders,
            ) from err
        except AccountLockedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_ui_locked",
                translation_placeholders=placeholders,
            ) from err
        except AuthenticationError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_ui_auth_failed",
                translation_placeholders=placeholders,
            ) from err
        except Exception as err:
            kind = error_kind(err)
            if not isinstance(err, RltechError) and kind != STATE_UNREACHABLE:
                _LOGGER.exception("Unexpected RLTech AP reboot failure")
            record = self.coordinator.client.reboot_records.get(ap.mac)
            if kind == STATE_UNREACHABLE:
                key = "web_ui_unreachable"
            elif record is not None and record.result == "unconfirmed":
                key = "ap_reboot_unconfirmed"
            else:
                key = "ap_reboot_failed"
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=key,
                translation_placeholders=placeholders,
            ) from err
        finally:
            self.async_write_ha_state()

    @property
    def _ap(self) -> RltechAp | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.aps_by_sn.get(self._sn)


class RltechAcRebootButton(RltechEntity, ButtonEntity):
    """Reboot the AC itself through mag-reset.asp (stage 6b).

    Same availability rule as the AP reboot button: usable while 8080
    polling is paused (a deliberate, one-off login), otherwise only while
    the last poll succeeded with the Web UI ok. Unavailable during the
    expected-offline window after a submitted reboot, so it cannot be
    pressed twice in a row.
    """

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "ac_reboot"

    def __init__(self, entry: ConfigEntry, coordinator: RltechCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ac_reboot"
        object_id = controller_sensor_object_id(object_id_host(entry), "reboot")
        self._attr_suggested_object_id = object_id
        self.entity_id = f"button.{object_id}"
        self._attr_device_info = controller_device_info(entry)

    @property
    def available(self) -> bool:
        coordinator = self.coordinator
        if not coordinator.has_web_baseline or coordinator.expected_offline():
            return False
        if coordinator.web_polling_paused():
            return True
        return (
            coordinator.last_update_success
            and self._source_state(SOURCE_WEB) == STATE_OK
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = self.coordinator.client.last_ac_reboot
        until = self.coordinator.expected_offline_until
        return {
            "last_reboot_requested": record.requested_at.isoformat()
            if record is not None
            else None,
            "last_reboot_result": record.result if record is not None else None,
            "expected_offline_until": until.isoformat()
            if until is not None and self.coordinator.expected_offline()
            else None,
        }

    async def async_press(self) -> None:
        """Ask the AC to reboot (plain reboot, never a factory reset)."""
        coordinator = self.coordinator
        if coordinator.expected_offline():
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="ac_reboot_in_progress"
            )
        session = async_get_clientsession(coordinator.hass)
        placeholders = {"host": entry_host(self.config_entry)}
        try:
            record = await coordinator.client.reboot_ac(
                session, on_submitted=coordinator.async_start_expected_offline
            )
        except AccountBusyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_ui_busy",
                translation_placeholders=placeholders,
            ) from err
        except AccountLockedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_ui_locked",
                translation_placeholders=placeholders,
            ) from err
        except AuthenticationError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_ui_auth_failed",
                translation_placeholders=placeholders,
            ) from err
        except Exception as err:
            kind = error_kind(err)
            if not isinstance(err, RltechError) and kind != STATE_UNREACHABLE:
                _LOGGER.exception("Unexpected RLTech AC reboot failure")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="web_ui_unreachable"
                if kind == STATE_UNREACHABLE
                else "ac_reboot_failed",
                translation_placeholders=placeholders,
            ) from err
        finally:
            self.async_write_ha_state()
        if record.result == "submitted_unconfirmed":
            # Usually the AC dropping the connection as it reboots.
            _LOGGER.info(
                "RLTech AC at %s did not answer the reboot request; it is "
                "probably rebooting (not retried)",
                placeholders["host"],
            )
