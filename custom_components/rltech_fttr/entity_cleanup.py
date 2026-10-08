"""Entity registry clean-up for entities this integration no longer creates."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONF_UNIT_OF_MEASUREMENT,
    Platform,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .identifiers import (
    is_retired_sensor_unique_id,
    master_lanpon_port,
    renamed_sensor_entity_id,
)
from .settings import object_id_host

_LOGGER = logging.getLogger(__name__)


@callback
def async_remove_retired_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[str]:
    """Delete this entry's removed sensors from the registry.

    Stage 6 (user decisions): LAN-* status/rate/mode, LANPON1/2 link status,
    rate and mode, and the uplink PON optics summary are deleted, not
    disabled; stage 6b adds the uplink PON link and registration sensors. Only sensor entities of this
    integration and this entry whose unique_id matches exactly are touched;
    LAN-PON optical sensors stay. Idempotent; runs on every setup.
    """
    registry = er.async_get(hass)
    removed: list[str] = []
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.platform != DOMAIN
            or entity.domain != Platform.SENSOR
            or not is_retired_sensor_unique_id(entry.entry_id, entity.unique_id)
        ):
            continue
        registry.async_remove(entity.entity_id)
        removed.append(entity.entity_id)
    if removed:
        _LOGGER.info(
            "Removed %s retired RLTech sensor(s): %s",
            len(removed),
            ", ".join(sorted(removed)),
        )
    return removed


@callback
def async_rename_entities(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Move old auto-generated entity_ids to the stage 6b names.

    ``..._uplink_pon_<key>`` -> ``..._pon_<key>`` and ``..._lanpon<n>_current``
    -> ``..._lanpon<n>_bias_current``. Only when the current entity_id is
    still the old generated one (a user-chosen entity_id is kept), and only
    when the new entity_id is free (else skipped, logged at info). The
    unique_id, and so the history, is unchanged. Idempotent; runs on every
    setup before the platforms.
    """
    registry = er.async_get(hass)
    host = object_id_host(entry)
    renamed: list[str] = []
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform != DOMAIN or entity.domain != Platform.SENSOR:
            continue
        rename = renamed_sensor_entity_id(entry.entry_id, host, entity.unique_id)
        if rename is None:
            continue
        old_id, new_id = rename
        if entity.entity_id != old_id:
            # Customised by the user (or already renamed): leave it.
            continue
        if registry.async_get(new_id) is not None or hass.states.get(new_id):
            _LOGGER.info(
                "Not renaming %s to %s: that entity ID is already in use",
                old_id,
                new_id,
            )
            continue
        registry.async_update_entity(old_id, new_entity_id=new_id)
        renamed.append(f"{old_id} -> {new_id}")
    if renamed:
        _LOGGER.info(
            "Renamed %s RLTech sensor(s): %s", len(renamed), ", ".join(sorted(renamed))
        )
    return renamed


@callback
def async_remove_extra_lanpon_entities(
    hass: HomeAssistant, entry: ConfigEntry, reported_ports: dict | None
) -> list[str]:
    """Delete master LAN-PON sensors of ports the AC does not have.

    ``reported_ports`` is the ponport_info list of the last successful
    sta-user.asp parse (RltechData.web_lanpon_ports; only a non-empty parse
    ever lands there). Empty or None (never parsed, fetch failed): nothing
    is deleted. Otherwise LANPON<n> sensors with n greater than the list
    length, and not a reported ponid, are removed (RH8001GR: only LANPON1).
    """
    if not reported_ports:
        return []
    count = len(reported_ports)
    registry = er.async_get(hass)
    removed: list[str] = []
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform != DOMAIN or entity.domain != Platform.SENSOR:
            continue
        port = master_lanpon_port(entry.entry_id, entity.unique_id)
        if port is None or port <= count or port in reported_ports:
            continue
        registry.async_remove(entity.entity_id)
        removed.append(entity.entity_id)
    if removed:
        _LOGGER.info(
            "Removed %s RLTech LAN-PON sensor(s) of ports the AC does not report: %s",
            len(removed),
            ", ".join(sorted(removed)),
        )
    return removed


_SENSOR_PRIVATE = "sensor.private"
_SUGGESTED_UNIT = "suggested_unit_of_measurement"


@callback
def async_fix_temperature_units(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Show this entry's temperature sensors in °C (stage 6c).

    New entities get it from suggested_unit_of_measurement. An existing
    entity keeps the unit Home Assistant stored for it when it was created
    (``options["sensor.private"]["suggested_unit_of_measurement"]``, °F on
    a US customary system), so that stored value is set to °C once. A
    display unit the user picked (``options["sensor"]["unit_of_measurement"]``)
    always wins and is never touched. Idempotent; runs before the platforms.
    """
    registry = er.async_get(hass)
    fixed: list[str] = []
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.platform != DOMAIN
            or entity.domain != Platform.SENSOR
            or entity.original_device_class != SensorDeviceClass.TEMPERATURE
        ):
            continue
        user_unit = (entity.options.get(Platform.SENSOR) or {}).get(CONF_UNIT_OF_MEASUREMENT)
        if user_unit in {unit.value for unit in UnitOfTemperature}:
            continue  # the user's own choice (HA ignores a null/invalid one)
        private = dict(entity.options.get(_SENSOR_PRIVATE) or {})
        if private.get(_SUGGESTED_UNIT) == UnitOfTemperature.CELSIUS:
            continue
        private[_SUGGESTED_UNIT] = UnitOfTemperature.CELSIUS
        registry.async_update_entity_options(entity.entity_id, _SENSOR_PRIVATE, private)
        fixed.append(entity.entity_id)
    if fixed:
        _LOGGER.info(
            "Set %s RLTech temperature sensor(s) to °C: %s",
            len(fixed),
            ", ".join(sorted(fixed)),
        )
    return fixed


# Units HA accepts for a duration sensor with a native unit in seconds.
_DURATION_UNITS = frozenset(
    {
        UnitOfTime.MICROSECONDS,
        UnitOfTime.MILLISECONDS,
        UnitOfTime.SECONDS,
        UnitOfTime.MINUTES,
        UnitOfTime.HOURS,
        UnitOfTime.DAYS,
        UnitOfTime.WEEKS,
    }
)


@callback
def async_fix_uptime_unit(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Show the AC System uptime in hours (stage 7, user decision).

    Stage 6c created the sensor with days as the suggested unit, which HA
    stored in ``options["sensor.private"]``; that stored value moves from
    d to h once. Any other stored value is left alone (so this runs only
    once), and a display unit the user picked in the entity settings
    (``options["sensor"]["unit_of_measurement"]``, a valid time unit) is
    never overridden. Idempotent; runs before the platforms.
    """
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_olt_system_uptime"
    entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id)
    if entity_id is None or (entity := registry.async_get(entity_id)) is None:
        return []
    user_unit = (entity.options.get(Platform.SENSOR) or {}).get(CONF_UNIT_OF_MEASUREMENT)
    if user_unit in _DURATION_UNITS:
        return []  # the user's own choice
    private = dict(entity.options.get(_SENSOR_PRIVATE) or {})
    if private.get(_SUGGESTED_UNIT) != UnitOfTime.DAYS:
        return []
    private[_SUGGESTED_UNIT] = UnitOfTime.HOURS
    registry.async_update_entity_options(entity_id, _SENSOR_PRIVATE, private)
    _LOGGER.info("Set RLTech %s to hours", entity_id)
    return [entity_id]
