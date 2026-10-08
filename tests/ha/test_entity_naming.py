"""Stage 6b unified PON/LANPON naming and LAN-PON port count, real HA core."""

from __future__ import annotations

from dataclasses import replace
import logging

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from rltech_ha_helpers import DOMAIN, fixture_text, make_entry

from custom_components.rltech_fttr import api

OBJ = "rltech_olt_198_51_100_29"


def _one_port_sta_user() -> str:
    """real_sta_user.html as an RL8001GR would render it (LANPON1 only)."""
    page = fixture_text("real_sta_user.html")
    start = page.index(', { "ponid": 2')
    end = page.index("} ] }", start)
    return page[:start] + page[end + 1 :]


def _set_one_port(fake_ac) -> None:
    ports = api.parse_lanpon_ports(_one_port_sta_user())
    assert list(ports) == [1]
    fake_ac.data = replace(fake_ac.data, lanpon_ports=ports, web_lanpon_ports=ports)


async def _setup(hass: HomeAssistant, entry):
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _register(registry, entry, unique_suffix: str, object_id: str) -> str:
    return registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_{unique_suffix}",
        config_entry=entry,
        suggested_object_id=object_id,
    ).entity_id


async def test_pon_and_lanpon_names_use_one_scheme(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)

    def name(object_id: str) -> str:
        entity = registry.async_get(f"sensor.{OBJ}_{object_id}")
        assert entity is not None, object_id
        return entity.original_name

    suffixes = {
        "tx_power": "TX power",
        "rx_power": "RX power",
        "temperature": "temperature",
        "voltage": "voltage",
        "bias_current": "bias current",
    }
    for key, suffix in suffixes.items():
        assert name(f"pon_{key}") == f"PON {suffix}"
        assert name(f"lanpon1_{key}") == f"LANPON1 {suffix}"
        assert name(f"lanpon2_{key}") == f"LANPON2 {suffix}"
    assert name("pon_link_type") == "PON link type"
    assert name("pon_online_status") == "PON online status"
    assert name("lanpon1_link") == "LANPON1 link"
    ours = [
        e for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]
    assert not [e for e in ours if "Uplink" in (e.original_name or "")]
    assert not [e for e in ours if "module" in (e.original_name or "")]
    # unique_ids keep their stage 6 keys (history).
    assert registry.async_get(f"sensor.{OBJ}_lanpon1_bias_current").unique_id == (
        f"{entry.entry_id}_lanpon_port_1_current"
    )
    assert registry.async_get(f"sensor.{OBJ}_pon_tx_power").unique_id == (
        f"{entry.entry_id}_uplink_pon_tx_power"
    )


async def test_lanpon_link_follows_the_port_status(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)
    # real_sta_user.html: LANPON1 status down, LANPON2 up.
    link1 = hass.states.get(f"sensor.{OBJ}_lanpon1_link")
    link2 = hass.states.get(f"sensor.{OBJ}_lanpon2_link")
    assert link1.state == "disconnected"
    assert link2.state == "connected"
    assert link1.attributes["options"] == ["connected", "disconnected"]
    entity = registry.async_get(f"sensor.{OBJ}_lanpon1_link")
    assert entity.entity_category == EntityCategory.DIAGNOSTIC
    assert entity.unique_id == f"{entry.entry_id}_lanpon_port_1_link"


async def test_old_generated_entity_ids_are_renamed(
    hass: HomeAssistant, fake_ac, caplog
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = {
        "uplink_pon_tx_power": f"{OBJ}_uplink_pon_tx_power",
        "uplink_pon_rx_power": f"{OBJ}_uplink_pon_rx_power",
        "uplink_pon_temperature": f"{OBJ}_uplink_pon_temperature",
        "uplink_pon_voltage": f"{OBJ}_uplink_pon_voltage",
        "uplink_pon_bias_current": f"{OBJ}_uplink_pon_bias_current",
        "lanpon_port_1_current": f"{OBJ}_lanpon1_current",
        "lanpon_port_2_current": f"{OBJ}_lanpon2_current",
    }
    for suffix, object_id in old.items():
        assert _register(registry, entry, suffix, object_id) == f"sensor.{object_id}"

    caplog.set_level(logging.INFO)
    await _setup(hass, entry)

    expected = {
        "uplink_pon_tx_power": f"sensor.{OBJ}_pon_tx_power",
        "uplink_pon_rx_power": f"sensor.{OBJ}_pon_rx_power",
        "uplink_pon_temperature": f"sensor.{OBJ}_pon_temperature",
        "uplink_pon_voltage": f"sensor.{OBJ}_pon_voltage",
        "uplink_pon_bias_current": f"sensor.{OBJ}_pon_bias_current",
        "lanpon_port_1_current": f"sensor.{OBJ}_lanpon1_bias_current",
        "lanpon_port_2_current": f"sensor.{OBJ}_lanpon2_bias_current",
    }
    for suffix, entity_id in expected.items():
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{suffix}"
        ) == entity_id, suffix
        assert hass.states.get(entity_id) is not None
        assert hass.states.get(f"sensor.{old[suffix]}") is None
    assert any("Renamed 7 RLTech sensor(s)" in r.getMessage() for r in caplog.records)
    # Idempotent: a reload renames nothing more.
    caplog.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not any("Renamed" in r.getMessage() for r in caplog.records)


async def test_rename_skips_customised_and_occupied_entity_ids(
    hass: HomeAssistant, fake_ac, caplog
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Customised by the user: kept as it is.
    custom = _register(registry, entry, "uplink_pon_tx_power", f"{OBJ}_uplink_pon_tx_power")
    registry.async_update_entity(custom, new_entity_id="sensor.my_uplink_tx")
    # Target taken by another integration's entity: skipped, logged at info.
    registry.async_get_or_create(
        "sensor", "other", "blocker", suggested_object_id=f"{OBJ}_pon_rx_power"
    )
    rx = _register(registry, entry, "uplink_pon_rx_power", f"{OBJ}_uplink_pon_rx_power")
    # Target taken by a plain state (e.g. a template sensor without registry).
    hass.states.async_set(f"sensor.{OBJ}_lanpon1_bias_current", "1")
    current = _register(registry, entry, "lanpon_port_1_current", f"{OBJ}_lanpon1_current")

    caplog.set_level(logging.INFO)
    await _setup(hass, entry)

    uid = entry.entry_id
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{uid}_uplink_pon_tx_power") == (
        "sensor.my_uplink_tx"
    )
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{uid}_uplink_pon_rx_power") == rx
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{uid}_lanpon_port_1_current"
    ) == current
    skipped = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and "already in use" in r.getMessage()
    ]
    assert len(skipped) == 2
    # The rest still moves.
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{uid}_uplink_pon_bias_current"
    ) == f"sensor.{OBJ}_pon_bias_current"


async def test_one_port_ac_gets_no_lanpon2_and_loses_an_old_one(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Left over from an earlier build that created both ports.
    stale = [
        _register(registry, entry, f"lanpon_port_2_{key}", f"{OBJ}_lanpon2_{key}")
        for key in ("tx_power", "rx_power", "temperature", "voltage", "current")
    ]
    keep = [
        _register(registry, entry, "lanpon_port_1_tx_power", f"{OBJ}_lanpon1_tx_power"),
        # Extra port-80 host: never touched by the master port count.
        _register(
            registry,
            entry,
            "legacy_olt_198.18.11.2_lanpon_port_2_tx_power",
            "rltech_olt_198_18_11_2_lanpon2_tx_power",
        ),
    ]
    _set_one_port(fake_ac)

    await _setup(hass, entry)

    for entity_id in stale:
        assert registry.async_get(entity_id) is None, entity_id
    for entity_id in keep:
        assert registry.async_get(entity_id) is not None, entity_id
    ours = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert not [e for e in ours if "lanpon_port_2" in e.unique_id and "legacy" not in e.unique_id]
    assert hass.states.get(f"sensor.{OBJ}_lanpon1_link") is not None
    assert hass.states.get(f"sensor.{OBJ}_lanpon2_link") is None


async def test_lanpon2_is_removed_when_the_port_list_shrinks_at_runtime(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    coordinator = await _setup(hass, entry)
    assert hass.states.get(f"sensor.{OBJ}_lanpon2_link") is not None
    _set_one_port(fake_ac)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get(f"sensor.{OBJ}_lanpon2_link") is None
    assert hass.states.get(f"sensor.{OBJ}_lanpon2_tx_power") is None
    assert hass.states.get(f"sensor.{OBJ}_lanpon1_tx_power") is not None


async def test_no_lanpon_cleanup_without_a_parsed_port_list(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = _register(registry, entry, "lanpon_port_2_tx_power", f"{OBJ}_lanpon2_tx_power")
    # sta-user.asp failed or returned no list: nothing is known, nothing goes.
    fake_ac.data = replace(fake_ac.data, web_lanpon_ports={})
    await _setup(hass, entry)
    assert registry.async_get(stale) is not None
