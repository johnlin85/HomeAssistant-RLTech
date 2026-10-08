"""Explicit device registry management for RLTech FTTR (plan 3.4).

Devices are created and updated here with ``async_get_or_create`` /
``async_update_device`` and ``via_device_id``; entities only carry
``identifiers`` in their DeviceInfo. This replaces the deprecated
``DeviceInfo(via_device=...)`` and ``async_get_device`` usage.
The decisions themselves live in ap_device_registry.py (pure, tested).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .ap_device_registry import (
    AP_MANUFACTURER,
    DEFAULT_STALE_DEVICE_DAYS,
    ap_configuration_url,
    ap_device_name,
    ap_device_registry_updates,
    ap_identifier,
    ap_mac_connection,
    controller_identifier,
    legacy_olt_identifier,
    legacy_only_entities_to_remove,
    mac_connection_conflict,
    should_warn,
    sn_from_ap_identifiers,
    stale_sns,
    update_missing_since,
    wan_deployment_confirmed,
)
from .const import (
    CONF_AP_AREA_ID,
    CONF_STALE_DEVICE_DAYS,
    DEFAULT_ENABLE_HARDWARE_STATUS,
    DOMAIN,
)
from .migration import DUPLICATE, MISMATCH, UPGRADE, decide_unique_id_upgrade
from .models import RltechAp, RltechData, RltechOltStatus
from .settings import (
    entry_base_url,
    entry_host,
    entry_value,
    hardware_status_enabled,
    object_id_host,
)
from .sources import STATE_OK

if TYPE_CHECKING:
    from .coordinator import RltechCoordinator

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1


def storage_key(entry_id: str) -> str:
    """Return the Store key holding per-entry device bookkeeping."""
    return f"{DOMAIN}.{entry_id}.devices"


def olt_device_updates(
    status: RltechOltStatus | None, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Return controller/OLT metadata updates; never clear stored values."""
    if status is None:
        return {}
    wanted = {
        "model": status.gateway_type,
        "hw_version": status.hardware_version,
        "sw_version": status.software_version,
        "serial_number": status.serial_number,
    }
    return {
        key: value
        for key, value in wanted.items()
        if value is not None and getattr(device, key, None) != value
    }


class RltechDeviceManager:
    """Create, update and age out the controller, AP and extra OLT devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.controller_device_id: str | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._missing: dict[str, datetime] = {}
        self._legacy_ever_succeeded = False
        self._legacy_cleanup_done = False
        self._ap_removed_listeners: list[Callable[[str], None]] = []
        self.unique_id_decision: str | None = None
        self._step_errors: set[tuple[str, str]] = set()
        self.paused_until: datetime | None = None
        self._unique_id_warned: set[str] = set()
        self._unique_id_frozen = False

    async def async_setup(self) -> None:
        """Load bookkeeping and register the controller device."""
        stored = await self._store.async_load() or {}
        for sn, value in (stored.get("missing") or {}).items():
            if (since := dt_util.parse_datetime(str(value))) is not None:
                self._missing[str(sn)] = since
        self._legacy_ever_succeeded = bool(stored.get("legacy_ever_succeeded"))
        paused = stored.get("web_polling_paused_until")
        self.paused_until = dt_util.parse_datetime(str(paused)) if paused else None
        self.async_ensure_controller_device()
        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, self._async_device_registry_updated
            )
        )

    @callback
    def async_ensure_controller_device(self) -> str:
        """Create the controller device before any entity refers to it."""
        registry = dr.async_get(self.hass)
        device = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={controller_identifier(self.entry.entry_id)},
            manufacturer=AP_MANUFACTURER,
            # Frozen at creation so reconfigure does not rename (plan 4.4).
            name=f"RLTech OLT {object_id_host(self.entry)}",
            configuration_url=entry_base_url(self.entry),
        )
        self.controller_device_id = device.id
        return device.id

    @callback
    def async_add_ap_removed_listener(
        self, listener: Callable[[str], None]
    ) -> CALLBACK_TYPE:
        """Call ``listener(sn)`` when one of our AP devices is removed."""
        self._ap_removed_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._ap_removed_listeners:
                self._ap_removed_listeners.remove(listener)

        return _remove

    @callback
    def _async_device_registry_updated(
        self, event: Event[dr.EventDeviceRegistryUpdatedData]
    ) -> None:
        if event.data["action"] != "remove":
            return
        device = event.data.get("device") or {}
        if device.get("config_entry_id") != self.entry.entry_id:
            return
        sn = sn_from_ap_identifiers(
            self.entry.entry_id, device.get("identifiers") or []
        )
        if sn is None:
            return
        # Forget the AP so it is recreated (device and entities) if it
        # reappears in the AC list (3.4.2 "deleted AP comes back").
        if self._missing.pop(sn, None) is not None:
            self._async_save()
        for listener in list(self._ap_removed_listeners):
            listener(sn)

    @callback
    def async_sync(self) -> None:
        """Bring the registry in line with the latest coordinator data."""
        data = self.coordinator.data
        if data is None:
            return
        registry = dr.async_get(self.hass)
        if self.controller_device_id is None or registry.async_get(
            self.controller_device_id
        ) is None:
            self.async_ensure_controller_device()
        # Each step is isolated: this listener runs before the platform
        # listeners (registered first in __init__.py), so an exception here
        # must not stop entities from being added in the same update.
        for name, step in (
            ("controller", lambda: self._sync_controller(registry, data)),
            ("aps", lambda: self._sync_aps(registry, data)),
            ("legacy_olts", lambda: self._sync_legacy_olts(registry, data)),
            ("missing_aps", lambda: self._age_missing_aps(registry, data)),
            ("legacy_cleanup", lambda: self._cleanup_legacy_only_entities(data)),
            ("unique_id", lambda: self._sync_entry_unique_id(data)),
        ):
            self._run_step(name, step)

    def _run_step(self, name: str, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception as err:  # noqa: BLE001 - see async_sync
            if should_warn(name, type(err).__name__, self._step_errors):
                _LOGGER.warning(
                    "RLTech device sync step %s failed", name, exc_info=True
                )
            else:
                _LOGGER.debug("RLTech device sync step %s failed: %s", name, err)
            return
        self._step_errors = {key for key in self._step_errors if key[0] != name}

    def _sync_controller(self, registry: dr.DeviceRegistry, data: RltechData) -> None:
        device = registry.async_get(self.controller_device_id)
        if device is None:
            return
        if updates := olt_device_updates(data.olt_status, device):
            registry.async_update_device(device.id, **updates)

    def _sync_aps(self, registry: dr.DeviceRegistry, data: RltechData) -> None:
        area_id = entry_value(self.entry, CONF_AP_AREA_ID)
        for sn, ap in data.aps_by_sn.items():
            try:
                self._sync_ap(registry, sn, ap, area_id)
            except (HomeAssistantError, ValueError) as err:
                # Never let one odd AP row break the listener chain (the
                # entity platforms run right after this listener).
                _LOGGER.warning("Unable to register RLTech AP %s device: %s", sn, err)

    def _sync_ap(
        self,
        registry: dr.DeviceRegistry,
        sn: str,
        ap: RltechAp,
        area_id: str | None,
    ) -> None:
        entry_id = self.entry.entry_id
        identifier = ap_identifier(entry_id, sn)
        connection = ap_mac_connection(ap)
        own = registry.async_get_device_by_identifier(identifier, entry_id)
        holder = registry.async_get_device_by_connection(connection, entry_id)
        if strip := mac_connection_conflict(
            ap, own_device=own, connection_holder=holder
        ):
            _LOGGER.debug(
                "Moving MAC %s from device %s to AP %s", ap.mac, strip.device_id, sn
            )
            registry.async_update_device(
                strip.device_id, new_connections=strip.new_connections
            )
        if own is None:
            device = registry.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={identifier},
                connections={connection},
                manufacturer=AP_MANUFACTURER,
                name=ap_device_name(ap),
                model=ap.model,
                sw_version=ap.version,
                serial_number=sn,
                configuration_url=ap_configuration_url(ap),
                via_device_id=self.controller_device_id,
            )
            if area_id and device.area_id is None:
                registry.async_update_device(device.id, area_id=area_id)
            return
        if updates := ap_device_registry_updates(
            ap, own, controller_device_id=self.controller_device_id
        ):
            registry.async_update_device(own.id, **updates)

    def _sync_legacy_olts(self, registry: dr.DeviceRegistry, data: RltechData) -> None:
        if data.legacy_sources and not self._legacy_ever_succeeded:
            self._legacy_ever_succeeded = True
            self._async_save()
        hosts = list(data.legacy_sources)
        entry_id = self.entry.entry_id
        for host in hosts[1:]:  # the first (master) source is the controller
            source = data.legacy_sources[host]
            identifier = legacy_olt_identifier(entry_id, host)
            device = registry.async_get_device_by_identifier(identifier, entry_id)
            if device is None:
                status = source.olt_status
                registry.async_get_or_create(
                    config_entry_id=entry_id,
                    identifiers={identifier},
                    manufacturer=(status.manufacturer if status else None)
                    or AP_MANUFACTURER,
                    name=f"RLTech OLT {host}",
                    model=status.gateway_type if status else None,
                    hw_version=status.hardware_version if status else None,
                    sw_version=status.software_version if status else None,
                    serial_number=status.serial_number if status else None,
                    configuration_url=f"http://{host}",
                    via_device_id=self.controller_device_id,
                )
                continue
            updates = olt_device_updates(source.olt_status, device)
            if device.via_device_id != self.controller_device_id:
                updates["via_device_id"] = self.controller_device_id
            if updates:
                registry.async_update_device(device.id, **updates)

    def _age_missing_aps(self, registry: dr.DeviceRegistry, data: RltechData) -> None:
        entry_id = self.entry.entry_id
        known_sns = {
            sn
            for device in dr.async_entries_for_config_entry(registry, entry_id)
            if (sn := sn_from_ap_identifiers(entry_id, device.identifiers)) is not None
        }
        now = dt_util.utcnow()
        missing = update_missing_since(
            self._missing,
            known_sns=known_sns,
            listed_sns=data.aps_by_sn,
            now=now,
            list_fresh=self.coordinator.last_update_success
            and self.coordinator.web_state.state == STATE_OK,
            ac_last_boot=data.olt_status.last_boot if data.olt_status else None,
        )
        missing = {sn: since for sn, since in missing.items() if sn in known_sns}
        changed = missing != self._missing
        self._missing = missing
        for sn in stale_sns(missing, now=now, days=self.stale_days()):
            device = registry.async_get_device_by_identifier(
                ap_identifier(entry_id, sn), entry_id
            )
            if device is not None:
                _LOGGER.info(
                    "Removing RLTech AP %s: not reported by the AC for %s days",
                    sn,
                    self.stale_days(),
                )
                # Fires the remove event, which also clears the timer.
                registry.async_remove_device(device.id)
        if changed:
            self._async_save()

    def _cleanup_legacy_only_entities(self, data: RltechData) -> None:
        """Remove port-80-only entities left by older versions (F1)."""
        if self._legacy_cleanup_done:
            return
        hardware_enabled = hardware_status_enabled(
            self.entry, DEFAULT_ENABLE_HARDWARE_STATUS
        )
        client = self.coordinator.client
        master = (
            urlsplit(client.legacy_base_urls[0]).hostname or client.legacy_base_urls[0]
            if client.legacy_base_urls
            else None
        )
        breaker = client.legacy_breakers.get(master) if master else None
        confirmed_down = breaker is not None and breaker.trips > 0
        ever_succeeded = self._legacy_ever_succeeded or bool(data.legacy_sources)
        probe = self.coordinator.endpoint_probe
        wan_confirmed = wan_deployment_confirmed(
            host=entry_host(self.entry),
            lan_ip=data.olt_status.lan_ip if data.olt_status else None,
            reachable=probe.reachable if probe is not None else None,
        )
        registry = er.async_get(self.hass)
        for entity_id in legacy_only_entities_to_remove(
            er.async_entries_for_config_entry(registry, self.entry.entry_id),
            entry_id=self.entry.entry_id,
            hardware_status_enabled=hardware_enabled,
            legacy_ever_succeeded=ever_succeeded,
            legacy_confirmed_down=confirmed_down,
            wan_deployment_confirmed=wan_confirmed,
        ):
            _LOGGER.info(
                "Removing RLTech entity %s: port 80 does not provide it here",
                entity_id,
            )
            registry.async_remove(entity_id)
        # Once decided (removed, or port 80 known to work), stop checking.
        # While port 80 or the WAN deployment is unconfirmed, keep checking.
        if not hardware_enabled or ever_succeeded or (confirmed_down and wan_confirmed):
            self._legacy_cleanup_done = True

    def _sync_entry_unique_id(self, data: RltechData) -> None:
        """Move a legacy URL unique_id to the AC LAN MAC (D8, plan 4.5).

        Runs after a poll, never during migration (no network there). The
        integration registers no update listener, so this does not reload.
        """
        if self._unique_id_frozen:
            return
        status = data.olt_status
        taken = {
            other.unique_id
            for other in self.hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != self.entry.entry_id and other.unique_id
        }
        decision = decide_unique_id_upgrade(
            self.entry.unique_id,
            lan_mac=status.lan_mac if status else None,
            serial=status.serial_number if status else None,
            taken=taken,
        )
        self.unique_id_decision = decision.action
        if decision.action == UPGRADE:
            try:
                self.hass.config_entries.async_update_entry(
                    self.entry, unique_id=decision.unique_id
                )
            except Exception as err:  # noqa: BLE001 - never retry this run
                self._unique_id_frozen = True
                _LOGGER.warning("Unable to update the RLTech entry unique ID: %s", err)
                return
            _LOGGER.info("RLTech entry unique ID updated to the AC hardware ID")
        elif decision.action in (DUPLICATE, MISMATCH):
            if decision.action not in self._unique_id_warned:
                self._unique_id_warned.add(decision.action)
                if decision.action == DUPLICATE:
                    _LOGGER.warning(
                        "Another RLTech entry already configures this AC; delete "
                        "the duplicate entry"
                    )
                else:
                    _LOGGER.warning(
                        "The AC at %s reports a different identity than the one "
                        "configured",
                        entry_host(self.entry),
                    )

    def stale_days(self) -> float:
        value = entry_value(
            self.entry, CONF_STALE_DEVICE_DAYS, DEFAULT_STALE_DEVICE_DAYS
        )
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return float(DEFAULT_STALE_DEVICE_DAYS)

    @callback
    def _async_save(self, delay: float = 10) -> None:
        self._store.async_delay_save(self._data_to_save, delay)

    async def async_flush(self) -> None:
        """Write pending bookkeeping now (unload/reload reads it right back).

        Delayed saves are flushed by HA on shutdown by itself; a reload
        creates a new Store instance that would otherwise read stale data.
        """
        await self._store.async_save(self._data_to_save())

    @callback
    def async_set_paused_until(self, value: datetime | None) -> None:
        """Persist the Web UI polling pause (survives reload/restart, S2)."""
        self.paused_until = value
        # Short delay: a restart right after pausing must not lose it.
        self._async_save(1)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "missing": {sn: since.isoformat() for sn, since in self._missing.items()},
            "legacy_ever_succeeded": self._legacy_ever_succeeded,
            "web_polling_paused_until": self.paused_until.isoformat()
            if self.paused_until
            else None,
        }

    def missing_since(self) -> dict[str, str]:
        """Return the missing timers for diagnostics (SNs are redacted there)."""
        return {sn: since.isoformat() for sn, since in self._missing.items()}


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the per-entry device bookkeeping when the entry is removed."""
    await Store(hass, STORAGE_VERSION, storage_key(entry_id)).async_remove()
