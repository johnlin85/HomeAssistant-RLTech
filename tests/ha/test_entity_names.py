"""Entity names and entity_ids do not change (stage 7 translation names).

The snapshot in tests/fixtures/entity_names_snapshot.json was taken from the
stage 6c build, where every name still came from ``_attr_name``. Stage 7
names entities through translation_key / translation_placeholders only; the
registry name, the friendly name and the entity_id of every entity must stay
exactly the same, for a new install and for an existing one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from rltech_ha_helpers import FIXTURES, HOST, V1_DATA, make_entry

from custom_components.rltech_fttr import models

EXTRA_HOST = "198.18.11.2"
SNAPSHOT = FIXTURES / "entity_names_snapshot.json"


def _with_port80(fake_ac) -> None:
    """Add a master and an extra port-80 source so every sensor exists."""
    now = datetime.now(UTC)
    port = models.RltechLanPonPort(ponid=1, status="up", tx_power=3.1)
    fake_ac.data = replace(
        fake_ac.data,
        legacy_sources={
            HOST: models.RltechLegacyOltSource(
                host=HOST,
                base_url=f"http://{HOST}",
                olt_status=models.RltechOltStatus(cpu_usage=7, memory_usage=40),
                last_success=now,
            ),
            EXTRA_HOST: models.RltechLegacyOltSource(
                host=EXTRA_HOST,
                base_url=f"http://{EXTRA_HOST}",
                olt_status=models.RltechOltStatus(cpu_usage=3, memory_usage=20),
                lanpon_ports={1: port},
                last_success=now,
            ),
        },
    )


def _entry():
    return make_entry(data={**V1_DATA, "enable_mqtt": False, "legacy_hosts": EXTRA_HOST})


async def _setup(hass: HomeAssistant, entry) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _collect(hass: HomeAssistant, entry) -> dict[str, dict[str, object]]:
    registry = er.async_get(hass)
    result: dict[str, dict[str, object]] = {}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(item.entity_id)
        result[item.unique_id.removeprefix(f"{entry.entry_id}_")] = {
            "entity_id": item.entity_id,
            "name": item.original_name,
            "friendly_name": state.attributes.get("friendly_name") if state else None,
        }
    return dict(sorted(result.items()))


def _snapshot() -> dict[str, dict[str, object]]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


async def test_new_install_keeps_every_name_and_entity_id(
    hass: HomeAssistant, fake_ac
) -> None:
    _with_port80(fake_ac)
    entry = _entry()
    await _setup(hass, entry)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    current = _collect(hass, entry)
    if not SNAPSHOT.exists():  # pragma: no cover - regenerate on purpose only
        print(json.dumps(current, indent=1, ensure_ascii=False, sort_keys=True))
        raise AssertionError("snapshot written to stdout")
    expected = _snapshot()
    assert sorted(current) == sorted(expected)
    for key, item in expected.items():
        assert current[key]["entity_id"] == item["entity_id"], key
        assert current[key]["name"] == item["name"], key
        if current[key]["friendly_name"] is not None:
            # Entities disabled by default (stage 7) have no state.
            assert current[key]["friendly_name"] == item["friendly_name"], key


# Stage 7 entity-disabled-by-default: low-value diagnostics, new installs only.
DISABLED_BY_DEFAULT = {
    "poll_duration",
    "olt_memory_usage",
    "legacy_olt_198.18.11.2_memory_usage",
    "ap_RLGMFEA0A000_source_host",
    "ap_RLGMFEA0A000_memory_usage",
    "ap_RLGMFEA0A000_flash_usage",
    "ap_RLGMFEB0B000_source_host",
    "ap_RLGMFEB0B000_memory_usage",
    "ap_RLGMFEB0B000_flash_usage",
    "uplink_pon_voltage",
    "uplink_pon_bias_current",
    "lanpon_port_1_voltage",
    "lanpon_port_1_current",
    "lanpon_port_2_voltage",
    "lanpon_port_2_current",
    "legacy_olt_198.18.11.2_lanpon_port_1_voltage",
    "legacy_olt_198.18.11.2_lanpon_port_1_current",
}


async def test_new_install_disables_only_the_low_value_diagnostics(
    hass: HomeAssistant, fake_ac
) -> None:
    _with_port80(fake_ac)
    entry = _entry()
    await _setup(hass, entry)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    disabled = {
        item.unique_id.removeprefix(f"{entry.entry_id}_")
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.disabled_by is not None
    }
    assert disabled == DISABLED_BY_DEFAULT
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.disabled_by is not None:
            assert item.disabled_by == er.RegistryEntryDisabler.INTEGRATION
            assert hass.states.get(item.entity_id) is None
        # Icons come from icons.json by translation_key, not the entity.
        assert item.original_icon is None, item.entity_id
        assert item.translation_key, item.entity_id


async def test_existing_install_keeps_names_ids_and_enabled_state(
    hass: HomeAssistant, fake_ac
) -> None:
    _with_port80(fake_ac)
    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    expected = _snapshot()
    # Registered and enabled by an earlier release, with the 6c names.
    for key, item in expected.items():
        domain, object_id = item["entity_id"].split(".", 1)
        created = registry.async_get_or_create(
            domain,
            "rltech_fttr",
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
            original_name=item["name"],
            has_entity_name=True,
        )
        assert created.entity_id == item["entity_id"]
    await _setup(hass, entry)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    current = _collect(hass, entry)
    assert sorted(current) == sorted(expected)
    for key, item in expected.items():
        assert current[key] == item, key
        assert registry.async_get(item["entity_id"]).disabled_by is None, key
