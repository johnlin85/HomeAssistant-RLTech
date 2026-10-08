"""Base entities for RLTech FTTR."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ap_device_registry import (
    ap_identifier,
    controller_identifier,
    legacy_olt_identifier,
)
from .coordinator import RltechCoordinator


class RltechEntity(CoordinatorEntity[RltechCoordinator]):
    """Base coordinator entity.

    DeviceInfo only carries identifiers: devices are created and kept up to
    date by devices.py before entities are added, so entity registration
    never passes the deprecated ``via_device`` nor overwrites device names.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: RltechCoordinator) -> None:
        super().__init__(coordinator)
        self.config_entry = entry

    @property
    def available(self) -> bool:
        """Available while the last update succeeded and the object exists.

        The coordinator only fails an update once the 8080 busy grace period
        is over or on network errors, so short Web UI conflicts keep entities
        available; longer ones make them unavailable instead of frozen.
        """
        return (
            super().available
            and self.coordinator.has_web_baseline
            # A deliberate pause serves frozen data: show it as unavailable
            # rather than current (review S1). The pause switch overrides this.
            and not self.coordinator.web_polling_paused()
            and self.coordinator.data is not None
            and self._object_available()
        )

    def _object_available(self) -> bool:
        """Return whether this entity's AP/port/source is present and fresh."""
        return True

    def _source_state(self, name: str) -> str | None:
        data = self.coordinator.data
        return data.sources.get(name) if data is not None else None


def controller_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Link an entity to the controller device created by devices.py."""
    return DeviceInfo(identifiers={controller_identifier(entry.entry_id)})


def legacy_olt_device_info(entry: ConfigEntry, host: str) -> DeviceInfo:
    """Link an entity to an additional port-80 OLT device (devices.py)."""
    return DeviceInfo(identifiers={legacy_olt_identifier(entry.entry_id, host)})


def ap_device_info(entry: ConfigEntry, sn: str) -> DeviceInfo:
    """Link an entity to its AP device (devices.py owns name, via, MAC)."""
    return DeviceInfo(identifiers={ap_identifier(entry.entry_id, sn)})
