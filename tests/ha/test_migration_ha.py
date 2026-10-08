"""Entry migration against a real Home Assistant (plan 4.6 / 4.7)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from rltech_ha_helpers import AP_B_SN, DOMAIN, HOST, V1_DATA, make_entry


async def test_field_v1_entry_migrates_without_changing_ids(
    hass: HomeAssistant, fake_ac
) -> None:
    # The field entry, MQTT on (its task is patched out below).
    entry = make_entry(data={**V1_DATA})
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # Registry state of the field install before the upgrade.
    controller = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"RLTech OLT {HOST}",
    )
    ap_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_ap_{AP_B_SN}")},
        connections={("mac", "e0:21:fe:b0:b0:00")},
        name="RLTech AP 820",
    )
    dev_reg.async_update_device(ap_device.id, name_by_user="Hall AP")
    before = {
        "sensor.rltech_olt_198_51_100_29_ap_count": f"{entry.entry_id}_ap_count",
        f"sensor.rltech_ap_{AP_B_SN.lower()}_assoc_count": (
            f"{entry.entry_id}_ap_{AP_B_SN}_assoc_count"
        ),
        "switch.rltech_olt_198_51_100_29_web_polling": f"{entry.entry_id}_web_polling",
    }
    for entity_id, unique_id in before.items():
        domain, object_id = entity_id.split(".", 1)
        registered = ent_reg.async_get_or_create(
            domain,
            DOMAIN,
            unique_id,
            suggested_object_id=object_id,
            config_entry=entry,
        )
        assert registered.entity_id == entity_id
    ent_reg.async_update_entity(
        "sensor.rltech_olt_198_51_100_29_ap_count", name="AC AP count"
    )

    # The MQTT task itself is not under test here.
    with patch(
        "custom_components.rltech_fttr.coordinator.RltechCoordinator.async_start_mqtt"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (1, 2)
    assert set(V1_DATA) <= set(entry.data)
    assert entry.data["object_id_host"] == HOST
    assert entry.options["mqtt_host"] == ""
    assert entry.options["enable_mqtt"] is True
    assert entry.options["scan_interval"] == 60

    for entity_id, unique_id in before.items():
        registered = ent_reg.async_get(entity_id)
        assert registered is not None and registered.unique_id == unique_id
    assert ent_reg.async_get("sensor.rltech_olt_198_51_100_29_ap_count").name == (
        "AC AP count"
    )
    assert dev_reg.async_get(controller.id) is not None
    ap_after = dev_reg.async_get(ap_device.id)
    assert ap_after is not None and ap_after.name_by_user == "Hall AP"
    assert hass.states.get("sensor.rltech_olt_198_51_100_29_ap_count").state == "2"

    def registry_snapshot():
        entities = {
            (e.entity_id, e.unique_id, e.device_id)
            for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        }
        devices = {
            (d.id, frozenset(d.identifiers))
            for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        }
        return entities, devices

    # Full registry of the running install, then replay the upgrade from a
    # v1.1 entry on top of it: every entity_id / unique_id / device id stays.
    installed = registry_snapshot()
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry, data={**V1_DATA}, options={}, version=1, minor_version=1
    )
    with patch(
        "custom_components.rltech_fttr.coordinator.RltechCoordinator.async_start_mqtt"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert (entry.version, entry.minor_version) == (1, 2)
    assert registry_snapshot() == installed


async def test_newer_major_version_is_refused(hass: HomeAssistant, fake_ac) -> None:
    entry = make_entry(version=2, minor_version=1)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_bad_v1_entry_is_left_untouched(hass: HomeAssistant, fake_ac) -> None:
    entry = make_entry(data={"username": "u", "password": "p"})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert (entry.version, entry.minor_version) == (1, 1)
    assert dict(entry.data) == {"username": "u", "password": "p"}


async def test_legacy_unique_id_is_upgraded_at_runtime_without_reload(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    assert entry.unique_id == f"http://{HOST}:8080"
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.unique_id == "e0:21:fe:c0:c0:00"
    reload.assert_not_called()
    assert fake_ac.calls == 1  # one setup, one first refresh
    manager = entry.runtime_data.device_manager
    assert manager.unique_id_decision == "upgrade"


async def test_duplicate_ac_keeps_legacy_unique_id(
    hass: HomeAssistant, fake_ac
) -> None:
    make_entry(
        data={"base_url": "http://192.0.2.1:8080", "username": "u", "password": "p"},
        unique_id="e0:21:fe:c0:c0:00",
        minor_version=2,
    ).add_to_hass(hass)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.unique_id == f"http://{HOST}:8080"
    manager = entry.runtime_data.device_manager
    assert manager.unique_id_decision == "duplicate"
