"""°C display on any unit system (6c), and the System uptime sensor (6c/7)."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from rltech_ha_helpers import AP_B_SN, DOMAIN, fixture_text, make_entry

from custom_components.rltech_fttr import api

OBJ = "rltech_olt_198_51_100_29"
CELSIUS = UnitOfTemperature.CELSIUS
FAHRENHEIT = UnitOfTemperature.FAHRENHEIT


async def _setup(hass: HomeAssistant, entry):
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _linked(fake_ac) -> None:
    fake_ac.data = replace(
        fake_ac.data,
        uplink_pon=api.parse_uplink_pon(fixture_text("sta_network_link_up.html")),
    )


def _assert_celsius(hass: HomeAssistant, entity_id: str, value: float) -> None:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    assert state.attributes["unit_of_measurement"] == CELSIUS, entity_id
    assert float(state.state) == value, entity_id


def _register_old(registry, entry, unique_suffix, object_id, *, unit=FAHRENHEIT):
    """An entity as HA stored it when created on a US customary system."""
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_{unique_suffix}",
        config_entry=entry,
        suggested_object_id=object_id,
        original_device_class="temperature",
        unit_of_measurement=unit,
    ).entity_id
    registry.async_update_entity_options(
        entity_id, "sensor.private", {"suggested_unit_of_measurement": unit}
    )
    return entity_id


async def test_new_temperature_sensors_show_celsius_on_us_units(
    hass: HomeAssistant, fake_ac
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    _linked(fake_ac)
    await _setup(hass, make_entry())
    _assert_celsius(hass, f"sensor.{OBJ}_cpu_temperature", 52)
    _assert_celsius(hass, f"sensor.{OBJ}_pon_temperature", 47.0)
    _assert_celsius(hass, f"sensor.{OBJ}_lanpon1_temperature", 49.61)
    _assert_celsius(hass, f"sensor.{OBJ}_lanpon2_temperature", 48.94)
    ap = hass.states.get(f"sensor.rltech_ap_{AP_B_SN.lower()}_cpu_temperature")
    assert ap is not None
    registry = er.async_get(hass)
    entity = registry.async_get(ap.entity_id)
    assert entity.options["sensor.private"]["suggested_unit_of_measurement"] == CELSIUS


async def test_existing_fahrenheit_entities_move_to_celsius_but_user_choice_stays(
    hass: HomeAssistant, fake_ac
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    _linked(fake_ac)
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    cpu = _register_old(registry, entry, "olt_cpu_temperature", f"{OBJ}_cpu_temperature")
    pon = _register_old(registry, entry, "uplink_pon_temperature", f"{OBJ}_pon_temperature")
    lanpon2 = _register_old(
        registry, entry, "lanpon_port_2_temperature", f"{OBJ}_lanpon2_temperature"
    )
    # A reset display unit may leave a null behind; HA ignores it (review N1).
    registry.async_update_entity_options(
        lanpon2, "sensor", {"unit_of_measurement": None}
    )
    ap = _register_old(
        registry,
        entry,
        f"ap_{AP_B_SN}_cpu_temperature",
        f"rltech_ap_{AP_B_SN.lower()}_cpu_temperature",
    )
    # The user picked °F for LANPON1 in the entity settings: keep it.
    lanpon1 = _register_old(
        registry, entry, "lanpon_port_1_temperature", f"{OBJ}_lanpon1_temperature"
    )
    registry.async_update_entity_options(
        lanpon1, "sensor", {"unit_of_measurement": FAHRENHEIT}
    )

    await _setup(hass, entry)

    _assert_celsius(hass, cpu, 52)
    _assert_celsius(hass, pon, 47.0)
    _assert_celsius(hass, lanpon2, 48.94)
    assert registry.async_get(ap).options["sensor.private"][
        "suggested_unit_of_measurement"
    ] == CELSIUS
    kept = hass.states.get(lanpon1)
    assert kept.attributes["unit_of_measurement"] == FAHRENHEIT
    assert abs(float(kept.state) - (49.61 * 9 / 5 + 32)) < 0.01
    assert registry.async_get(lanpon1).options["sensor.private"][
        "suggested_unit_of_measurement"
    ] == FAHRENHEIT

    # Idempotent: a reload changes nothing more.
    before = {
        e.entity_id: dict(e.options)
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after = {
        e.entity_id: dict(e.options)
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before
    _assert_celsius(hass, cpu, 52)


async def test_non_temperature_entities_are_not_touched(
    hass: HomeAssistant, fake_ac
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = make_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        private = entity.options.get("sensor.private") or {}
        if entity.original_device_class != "temperature":
            assert private.get("suggested_unit_of_measurement") != CELSIUS, (
                entity.entity_id
            )


async def test_system_uptime_sensor(hass: HomeAssistant, fake_ac) -> None:
    entry = make_entry()
    coordinator = await _setup(hass, entry)
    entity_id = f"sensor.{OBJ}_system_uptime"
    registry = er.async_get(hass)
    entity = registry.async_get(entity_id)
    assert entity.unique_id == f"{entry.entry_id}_olt_system_uptime"
    assert entity.entity_category == EntityCategory.DIAGNOSTIC
    assert entity.original_name == "System uptime"
    assert entity.original_device_class == "duration"
    state = hass.states.get(entity_id)
    # real_sta_device.html curTime 1211442 s, shown in hours (stage 7).
    assert state.attributes["unit_of_measurement"] == UnitOfTime.HOURS
    assert abs(float(state.state) - 1211442 / 3600) < 1e-6
    assert entity.options["sensor.private"]["suggested_unit_of_measurement"] == (
        UnitOfTime.HOURS
    )
    assert entity.options["sensor"]["suggested_display_precision"] == 1

    # No usable uptime on the page: unknown, never 0.
    fake_ac.data = replace(
        fake_ac.data,
        olt_status=replace(fake_ac.data.olt_status, uptime_seconds=None),
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"


async def test_system_uptime_only_on_the_master(hass: HomeAssistant, fake_ac) -> None:
    entry = make_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)
    uptimes = [
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.endswith("system_uptime")
    ]
    assert uptimes == [f"{entry.entry_id}_olt_system_uptime"]


def _register_uptime(registry, entry, *, unit=UnitOfTime.DAYS, user_unit=None):
    """The uptime sensor as stage 6c registered it (suggested unit: days)."""
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_olt_system_uptime",
        config_entry=entry,
        suggested_object_id=f"{OBJ}_system_uptime",
        original_device_class="duration",
        unit_of_measurement=unit,
    ).entity_id
    registry.async_update_entity_options(
        entity_id, "sensor.private", {"suggested_unit_of_measurement": unit}
    )
    if user_unit is not None:
        registry.async_update_entity_options(
            entity_id, "sensor", {"unit_of_measurement": user_unit}
        )
    return entity_id


async def test_existing_uptime_in_days_moves_to_hours_once(
    hass: HomeAssistant, fake_ac, caplog
) -> None:
    import logging

    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity_id = _register_uptime(registry, entry)
    caplog.set_level(logging.INFO)
    await _setup(hass, entry)
    state = hass.states.get(entity_id)
    assert state.attributes["unit_of_measurement"] == UnitOfTime.HOURS
    assert abs(float(state.state) - 1211442 / 3600) < 1e-6
    assert any("to hours" in r.getMessage() for r in caplog.records)
    options = registry.async_get(entity_id).options
    # Idempotent: a reload changes nothing and logs nothing.
    caplog.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(entity_id).options == options
    assert not any("to hours" in r.getMessage() for r in caplog.records)


async def test_uptime_unit_chosen_by_the_user_is_kept(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity_id = _register_uptime(registry, entry, user_unit=UnitOfTime.DAYS)
    await _setup(hass, entry)
    state = hass.states.get(entity_id)
    assert state.attributes["unit_of_measurement"] == UnitOfTime.DAYS
    assert registry.async_get(entity_id).options["sensor.private"] == {
        "suggested_unit_of_measurement": UnitOfTime.DAYS
    }


async def test_uptime_with_another_stored_unit_is_not_touched(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Only the stage 6c default (d) moves; anything else is left as it is.
    entity_id = _register_uptime(registry, entry, unit=UnitOfTime.MINUTES)
    await _setup(hass, entry)
    assert hass.states.get(entity_id).attributes["unit_of_measurement"] == (
        UnitOfTime.MINUTES
    )
