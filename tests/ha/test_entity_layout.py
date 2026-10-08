"""Stage 6 AC entity changes against a real Home Assistant."""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from rltech_ha_helpers import DOMAIN, make_entry

from custom_components.rltech_fttr.identifiers import is_retired_sensor_unique_id

OBJ = "rltech_olt_198_51_100_29"


async def _setup(hass: HomeAssistant, entry):
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def test_retired_lan_entities_are_removed_from_the_registry(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    # A second entry that is not set up here (another domain, so setting up
    # rltech_fttr does not load it), owning an entity with the same suffix.
    other_entry = MockConfigEntry(domain="other_integration", title="Other")
    other_entry.add_to_hass(hass)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    eid = entry.entry_id

    def register(unique_id, object_id, *, config_entry=entry, platform=DOMAIN):
        return registry.async_get_or_create(
            "sensor",
            platform,
            unique_id,
            config_entry=config_entry,
            suggested_object_id=object_id,
        ).entity_id

    retired = [
        register(f"{eid}_lan_port_1_status", f"{OBJ}_lan_1_status"),
        register(f"{eid}_lan_port_1_rate", f"{OBJ}_lan_1_rate"),
        register(f"{eid}_lan_port_4_mode", f"{OBJ}_lan_4_mode"),
        register(f"{eid}_lan_port_5_status", f"{OBJ}_lanpon1_status"),
        register(f"{eid}_lanpon_port_1_status", f"{OBJ}_lanpon1_status"),
        register(f"{eid}_lan_port_6_rate", f"{OBJ}_lanpon2_rate"),
        register(
            f"{eid}_legacy_olt_198.18.11.2_lan_port_2_status",
            "rltech_olt_198_18_11_2_lan_2_status",
        ),
        # Optics summary dropped by dual-group decision D.
        register(f"{eid}_uplink_pon_optics", f"{OBJ}_uplink_pon_optics"),
        # Stage 6b: replaced by PON link type / online status.
        register(f"{eid}_uplink_pon_link_state", f"{OBJ}_uplink_pon_link_state"),
        register(
            f"{eid}_uplink_pon_registration_state",
            f"{OBJ}_uplink_pon_registration_state",
        ),
    ]
    assert retired[4].endswith("_lanpon1_status_2")
    kept = [
        register(f"{eid}_lanpon_port_1_tx_power", f"{OBJ}_lanpon1_tx_power"),
        register(f"{eid}_lanpon_port_2_temperature", f"{OBJ}_lanpon2_temperature"),
        # Same suffix, other entry / other integration: never touched.
        register(
            f"{other_entry.entry_id}_lan_port_1_status",
            "other_lan_1_status",
            config_entry=other_entry,
        ),
        register(f"{eid}_lan_port_2_status", "foreign_lan_2", platform="other"),
        register(
            f"{other_entry.entry_id}_uplink_pon_optics",
            "other_uplink_pon_optics",
            config_entry=other_entry,
        ),
    ]

    await _setup(hass, entry)

    for entity_id in retired:
        assert registry.async_get(entity_id) is None, entity_id
        assert hass.states.get(entity_id) is None
    for entity_id in kept:
        assert registry.async_get(entity_id) is not None, entity_id

    # And none are created again from the sta-user LAN rows.
    ours = [
        e
        for e in er.async_entries_for_config_entry(registry, eid)
        if e.platform == DOMAIN
    ]
    assert not [
        e.entity_id
        for e in ours
        if "_lan_" in e.entity_id
        or (
            (e.original_name or "").startswith(("LAN-", "LANPON"))
            and (e.original_name or "").endswith((" link", " rate", " mode", " optical"))
            # Stage 6b LANPONn link (connected/disconnected) is a new entity.
            and not e.unique_id.endswith("_link")
        )
    ]
    assert not [e for e in ours if is_retired_sensor_unique_id(eid, e.unique_id)]
    assert not [e for e in ours if e.unique_id.endswith("_status") and "lanpon" in e.unique_id]
    assert hass.states.get(f"sensor.{OBJ}_lanpon2_rx_power") is not None


async def test_controller_sensor_and_diagnostic_groups(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    await _setup(hass, entry)
    registry = er.async_get(hass)

    def category(object_id: str):
        entity = registry.async_get(f"sensor.{OBJ}_{object_id}")
        assert entity is not None, object_id
        return entity.entity_category

    main = [
        "ap_count",
        "online_ap_count",
        "reported_station_count",
        "last_boot",
        "pon_link_type",
        "pon_online_status",
    ]
    diagnostic = [
        "poll_duration",
        "cpu_temperature",
        "pon_temperature",
        "pon_voltage",
        "pon_bias_current",
        "pon_tx_power",
        "pon_rx_power",
        "lanpon1_link",
        "lanpon1_tx_power",
        "lanpon1_rx_power",
        "lanpon1_temperature",
        "lanpon1_voltage",
        "lanpon1_bias_current",
        "lanpon2_link",
        "lanpon2_tx_power",
    ]
    for object_id in main:
        assert category(object_id) is None, object_id
    for object_id in diagnostic:
        assert category(object_id) == EntityCategory.DIAGNOSTIC, object_id

    # Field state (ActiveEtherWan, GE module, no PON fibre): the page shows
    # online / GE; optical values stay unknown.
    state = hass.states.get
    online = state(f"sensor.{OBJ}_pon_online_status")
    assert online.state == "online"
    assert online.attributes["pon_state"] == "up"
    assert online.attributes["link_state"] == "down"
    link_type = state(f"sensor.{OBJ}_pon_link_type")
    assert link_type.state == "ge"
    assert link_type.attributes["options"][:2] == ["gpon", "epon"]
    assert state(f"sensor.{OBJ}_uplink_pon_link_state") is None
    assert state(f"sensor.{OBJ}_uplink_pon_registration_state") is None
    for key in ("tx_power", "rx_power", "temperature"):
        assert state(f"sensor.{OBJ}_pon_{key}").state == "unknown", key
    for key in ("voltage", "bias_current"):
        # Disabled by default for new installs (stage 7): no state.
        assert state(f"sensor.{OBJ}_pon_{key}") is None, key
        assert registry.async_get(f"sensor.{OBJ}_pon_{key}").disabled_by == (
            er.RegistryEntryDisabler.INTEGRATION
        )
    # Dual-group decision D: no optics summary entity.
    assert state(f"sensor.{OBJ}_uplink_pon_optics") is None
    # Main group: only link type and online status of the uplink.
    registry_main = [
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if "uplink_pon_" in e.unique_id and e.entity_category is None
    ]
    assert sorted(registry_main) == [
        f"{entry.entry_id}_uplink_pon_link_type",
        f"{entry.entry_id}_uplink_pon_online_status",
    ]


async def test_uplink_pon_values_when_linked(
    hass: HomeAssistant, fake_ac
) -> None:
    from dataclasses import replace

    from custom_components.rltech_fttr import api
    from rltech_ha_helpers import fixture_text

    fake_ac.data = replace(
        fake_ac.data,
        uplink_pon=api.parse_uplink_pon(fixture_text("sta_network_link_up.html")),
    )
    await _setup(hass, make_entry())
    state = hass.states.get
    assert state(f"sensor.{OBJ}_pon_online_status").state == "online"
    assert state(f"sensor.{OBJ}_pon_link_type").state == "gpon"
    assert float(state(f"sensor.{OBJ}_pon_rx_power").state) == -14.0
    assert float(state(f"sensor.{OBJ}_pon_tx_power").state) == 3.0
    assert float(state(f"sensor.{OBJ}_pon_temperature").state) == 47.0


async def test_existing_cpu_temperature_moves_back_to_diagnostic(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Stage 6 put it in the main group; stage 6b moves it back.
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_olt_cpu_temperature",
        config_entry=entry,
        suggested_object_id=f"{OBJ}_cpu_temperature",
    ).entity_id
    await _setup(hass, entry)
    assert registry.async_get(entity_id).entity_category == EntityCategory.DIAGNOSTIC


async def test_uplink_pon_entities_turn_unavailable_when_values_are_dropped(
    hass: HomeAssistant, fake_ac
) -> None:
    from dataclasses import replace

    coordinator = await _setup(hass, make_entry())
    assert hass.states.get(f"sensor.{OBJ}_pon_online_status").state == "online"
    # api.py drops uplink_pon after 2 status cycles without a parse (S3).
    fake_ac.data = replace(fake_ac.data, uplink_pon=None)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{OBJ}_pon_online_status").state == (
        "unavailable"
    )
    assert hass.states.get(f"sensor.{OBJ}_ap_count").state == "2"


async def test_existing_uplink_power_moves_to_diagnostic(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ids = [
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_uplink_pon_{key}",
            config_entry=entry,
            suggested_object_id=f"{OBJ}_uplink_pon_{key}",
        ).entity_id
        for key in ("tx_power", "rx_power")
    ]
    await _setup(hass, entry)
    for entity_id in ids:
        # Renamed to the stage 6b entity_id on the way (same unique_id).
        new_id = entity_id.replace("_uplink_pon_", "_pon_")
        assert registry.async_get(entity_id) is None
        assert registry.async_get(new_id).entity_category == (
            EntityCategory.DIAGNOSTIC
        )
