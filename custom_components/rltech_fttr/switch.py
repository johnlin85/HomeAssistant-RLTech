"""Switch entities for RLTech FTTR."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import WEB_POLLING_PAUSE
from .coordinator import RltechConfigEntry, RltechCoordinator
from .entity import RltechEntity, controller_device_info
from .identifiers import controller_sensor_object_id
from .settings import object_id_host

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RltechConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RLTech FTTR switches."""
    coordinator = entry.runtime_data
    async_add_entities([RltechWebPollingSwitch(entry, coordinator)])


class RltechWebPollingSwitch(RltechEntity, SwitchEntity):
    """Pause 8080 polling while an administrator uses the AC Web UI.

    The AC allows a single Web UI login shared by the admin and user
    accounts, and each poll logs in. Turning this off stops 8080 polling so
    it cannot block or log out a person in the AC Web UI; it turns itself
    back on after 15 minutes.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "web_polling"

    def __init__(self, entry: ConfigEntry, coordinator: RltechCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_web_polling"
        object_id = controller_sensor_object_id(object_id_host(entry), "web_polling")
        self._attr_suggested_object_id = object_id
        self.entity_id = f"switch.{object_id}"
        self._attr_device_info = controller_device_info(entry)

    @property
    def available(self) -> bool:
        """Stay usable while 8080 is failing so polling can be paused."""
        return True

    @property
    def is_on(self) -> bool:
        return not self.coordinator.web_polling_paused()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        until = self.coordinator.web_polling_paused_until
        return {
            "paused_until": until.isoformat()
            if until is not None and self.coordinator.web_polling_paused()
            else None,
            "pause_minutes": int(WEB_POLLING_PAUSE.total_seconds() // 60),
        }

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Pause 8080 polling for 15 minutes."""
        self.coordinator.async_pause_web_polling()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Resume 8080 polling now."""
        await self.coordinator.async_resume_web_polling()
