"""Websocket API against a real Home Assistant."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from rltech_ha_helpers import AP_B_SN, make_entry

from custom_components.rltech_fttr.api import AccountBusyError

AP_B_MAC = "E0:21:FE:B0:B0:00"


async def _setup(hass: HomeAssistant):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data


async def test_get_stations_filters_by_ap_mac_and_reports_sources(
    hass: HomeAssistant, hass_ws_client, fake_ac
) -> None:
    entry, _ = await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "rltech_fttr/get_stations", "entry_id": entry.entry_id}
    )
    everything = (await client.receive_json())["result"]
    assert everything["filtered"] == 5

    await client.send_json_auto_id(
        {
            "type": "rltech_fttr/get_stations",
            "entry_id": entry.entry_id,
            "filters": {"ap_mac": AP_B_MAC},
        }
    )
    result = (await client.receive_json())["result"]
    assert result["filtered"] == 4
    assert {row["ap_mac"] for row in result["stations"]} == {AP_B_MAC}
    assert {"value": AP_B_MAC, "label": "820"} in result["filter_options"]["ap"]
    sources = result["sources"]
    assert sources["web"]["state"] == "ok"
    assert sources["web"]["last_success"] is not None
    assert sources["mqtt"] == {
        "enabled": False,
        "connected": False,
        "last_message": None,
        "error_kind": None,
    }
    assert sources["stations_source"] == "http"

    # Old cards: the "ap" key with the alias still filters.
    await client.send_json_auto_id(
        {
            "type": "rltech_fttr/get_stations",
            "entry_id": entry.entry_id,
            "filters": {"ap": "820"},
        }
    )
    assert (await client.receive_json())["result"]["filtered"] == 4


async def test_get_access_points_counts_and_busy_sources(
    hass: HomeAssistant, hass_ws_client, fake_ac
) -> None:
    entry, coordinator = await _setup(hass)
    fake_ac.error = AccountBusyError("busy")
    await coordinator.async_refresh()
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "rltech_fttr/get_access_points", "entry_id": entry.entry_id}
    )
    message = await client.receive_json()
    assert message["success"], message
    result = message["result"]
    rows = {row["sn"]: row for row in result["access_points"]}
    assert rows[AP_B_SN]["station_count_reported"] == 4
    assert result["sources"]["web"]["state"] == "busy"
    assert result["sources"]["web"]["busy_since"] is not None

    await client.send_json_auto_id({"type": "rltech_fttr/get_entries"})
    entries = (await client.receive_json())["result"]["entries"]
    assert entries[0]["sources"]["web"]["state"] == "busy"


async def test_panel_is_admin_only_and_removed_with_the_last_entry(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token, fake_ac
) -> None:
    from homeassistant.components import frontend

    first, _ = await _setup(hass)
    second = make_entry(
        data={"base_url": "http://198.51.100.30:8080", "username": "u", "password": "p"},
        unique_id="other",
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    assert frontend.async_panel_exists(hass, "rltech-fttr")

    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id({"type": "get_panels"})
    panels = (await admin.receive_json())["result"]
    panel = panels["rltech-fttr"]
    assert panel["require_admin"] is True
    assert panel["title"] == "RLTech FTTR"
    assert panel["component_name"] == "custom"
    assert panel["config"]["_panel_custom"]["name"] == "rltech-fttr-panel"
    assert all("?v=" in url for url in panel["config"]["card_urls"])

    user = await hass_ws_client(hass, hass_read_only_access_token)
    await user.send_json_auto_id({"type": "get_panels"})
    assert "rltech-fttr" not in (await user.receive_json())["result"]

    # Still there while another entry is loaded; removed with the last one.
    assert await hass.config_entries.async_unload(first.entry_id)
    assert frontend.async_panel_exists(hass, "rltech-fttr")
    assert await hass.config_entries.async_unload(second.entry_id)
    assert not frontend.async_panel_exists(hass, "rltech-fttr")

    # Loading again brings it back.
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    assert frontend.async_panel_exists(hass, "rltech-fttr")


async def test_client_data_is_for_administrators_only(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token, fake_ac
) -> None:
    entry, _ = await _setup(hass)
    user = await hass_ws_client(hass, hass_read_only_access_token)

    for msg in (
        {"type": "rltech_fttr/get_stations", "entry_id": entry.entry_id},
        {"type": "rltech_fttr/subscribe_station_changes", "entry_id": entry.entry_id},
    ):
        await user.send_json_auto_id(msg)
        answer = await user.receive_json()
        assert not answer["success"], msg["type"]
        assert answer["error"]["code"] == "unauthorized", msg["type"]

    # Entry list and AP table stay open (no client data).
    await user.send_json_auto_id({"type": "rltech_fttr/get_entries"})
    answer = await user.receive_json()
    assert answer["success"]
    assert [e["entry_id"] for e in answer["result"]["entries"]] == [entry.entry_id]
    await user.send_json_auto_id(
        {"type": "rltech_fttr/get_access_points", "entry_id": entry.entry_id}
    )
    answer = await user.receive_json()
    assert answer["success"]
    assert answer["result"]["total"] == 2
    assert not any("stations" in row for row in answer["result"]["access_points"])

    # An administrator gets both.
    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id(
        {"type": "rltech_fttr/get_stations", "entry_id": entry.entry_id}
    )
    answer = await admin.receive_json()
    assert answer["success"] and answer["result"]["filtered"] == 5
    await admin.send_json_auto_id(
        {"type": "rltech_fttr/subscribe_station_changes", "entry_id": entry.entry_id}
    )
    assert (await admin.receive_json())["success"]


async def test_ap_rows_do_not_link_to_disabled_entities(
    hass: HomeAssistant, hass_ws_client, fake_ac
) -> None:
    from homeassistant.helpers import entity_registry as er

    entry, _ = await _setup(hass)
    registry = er.async_get(hass)
    # Memory/flash usage are disabled by default on a new install (stage 7).
    memory = registry.async_get_entity_id(
        "sensor", "rltech_fttr", f"{entry.entry_id}_ap_{AP_B_SN}_memory_usage"
    )
    assert registry.async_get(memory).disabled
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "rltech_fttr/get_access_points", "entry_id": entry.entry_id}
    )
    rows = (await client.receive_json())["result"]["access_points"]
    row = next(r for r in rows if r["mac"] == AP_B_MAC)
    assert "memory_usage" not in row["entities"]
    assert "flash_usage" not in row["entities"]
    assert {"memory_usage", "flash_usage"} <= set(row["disabled_entities"])
    assert "cpu_usage" in row["entities"]
    assert "cpu_usage" not in row["disabled_entities"]
