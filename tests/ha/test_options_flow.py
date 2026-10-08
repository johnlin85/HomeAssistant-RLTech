"""Options flow against a real Home Assistant."""

from __future__ import annotations

from datetime import timedelta, timezone
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from rltech_ha_helpers import V1_DATA, make_entry

from custom_components.rltech_fttr.config_logic import DEFAULT_OPTIONS
from custom_components.rltech_fttr.const import (
    DEFAULT_MQTT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STATION_RETENTION,
    DEFAULT_STATION_STALE_AFTER,
)

FLOW = "custom_components.rltech_fttr.config_flow"


def test_pure_defaults_match_const() -> None:
    assert DEFAULT_OPTIONS["scan_interval"] == DEFAULT_SCAN_INTERVAL
    assert DEFAULT_OPTIONS["station_retention"] == DEFAULT_STATION_RETENTION
    assert DEFAULT_OPTIONS["station_stale_after"] == DEFAULT_STATION_STALE_AFTER
    assert DEFAULT_OPTIONS["mqtt_port"] == DEFAULT_MQTT_PORT
    # Retired in stage 6 (reboots go through the AC): no default, no field.
    assert "ap_password" not in DEFAULT_OPTIONS


async def _loaded_entry(hass: HomeAssistant):
    entry = make_entry(mqtt=False)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _form_input(**changes):
    values = {
        "scan_interval": 60,
        "web_busy_grace_polls": 3,
        "stale_device_days": 7,
        "ac_utc_offset": "ha",
        "clients": {
            "station_retention": 3600,
            "station_stale_after": 900,
            "enable_ap_polling": True,
            "enable_station_polling": True,
        },
        "port80": {"enable_hardware_status": True, "legacy_username": "admin"},
        "mqtt": {
            "enable_mqtt": False,
            "mqtt_username": "admin",
            "mqtt_psk_identity": "admin",
            "mqtt_use_factory_defaults": False,
        },
    }
    for key, value in changes.items():
        if isinstance(value, dict):
            values[key] = {**values[key], **value}
        else:
            values[key] = value
    return values


async def test_options_save_reloads_and_takes_effect(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = await _loaded_entry(hass)
    assert entry.options["scan_interval"] == 60  # migrated
    calls = fake_ac.calls

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert {"clients", "port80", "mqtt"} <= set(result["data_schema"].schema)
    fields = {str(key) for key in result["data_schema"].schema}
    assert "ap" not in fields
    assert not {"ap_username", "ap_password"} & fields

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form_input(scan_interval=30, ac_utc_offset="+07:00", web_busy_grace_polls=1),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options["scan_interval"] == 30
    assert entry.options["web_busy_grace_polls"] == 1
    # Secrets left empty keep the stored values.
    assert entry.options["mqtt_password"] == V1_DATA["mqtt_password"]
    assert entry.options["mqtt_psk"] == V1_DATA["mqtt_psk"]
    # Retired AP login: no field, stored values kept for rollback.
    assert entry.options["ap_password"] == V1_DATA["ap_password"]
    assert entry.options["ap_username"] == V1_DATA["ap_username"]
    assert entry.data["ap_password"] == V1_DATA["ap_password"]
    # Reloaded: a new first refresh ran, with the new settings.
    assert fake_ac.calls > calls
    coordinator = entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=30)
    assert fake_ac.kwargs["local_timezone"] == timezone(timedelta(hours=7))


async def test_options_explicit_mqtt_host_is_verified(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = await _loaded_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(
        f"{FLOW}.async_mqtt_handshake", AsyncMock(return_value="unreachable")
    ) as shake:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            _form_input(mqtt={"enable_mqtt": True, "mqtt_host": "192.0.2.1"}),
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "mqtt_unreachable"}
    assert shake.await_args.args[0] == "192.0.2.1"

    # Clearing the address (automatic) saves without a handshake.
    with patch(f"{FLOW}.async_mqtt_handshake", AsyncMock()) as shake:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _form_input(mqtt={"enable_mqtt": True})
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    shake.assert_not_awaited()
    await hass.async_block_till_done()
    assert entry.options["mqtt_host"] == ""
    assert entry.options["enable_mqtt"] is True
