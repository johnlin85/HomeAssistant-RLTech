"""Tests for migration.py and config_logic.py (plan 4.7, M1-M11)."""

from __future__ import annotations

import pytest
from test_settings import load_module

config_logic = load_module("config_logic")
migration = load_module("migration")

HOST = "198.51.100.29"
FIELD_V1 = {
    "base_url": f"http://{HOST}:8080",
    "username": "useradmin",
    "password": "fake",
    "enable_hardware_status": True,
    "legacy_username": "admin",
    "legacy_password": "admin",
    "legacy_hosts": "",
    "enable_ap_polling": True,
    "enable_station_polling": True,
    "scan_interval": 60,
    "station_retention": 3600,
    "station_stale_after": 900,
    "enable_mqtt": True,
    "mqtt_host": HOST,
    "mqtt_port": 8883,
    "mqtt_username": "admin",
    "mqtt_password": "fake-mqtt",
    "mqtt_psk_identity": "admin",
    "mqtt_psk": "61646d696e21402324",
    "ap_username": "useradmin",
    "ap_password": "1234",
}
OPTION_KEYS = {
    "scan_interval",
    "web_busy_grace_polls",
    "stale_device_days",
    "ac_utc_offset",
    "station_retention",
    "station_stale_after",
    "enable_ap_polling",
    "enable_station_polling",
    "enable_hardware_status",
    "legacy_username",
    "legacy_password",
    "legacy_hosts",
    "enable_mqtt",
    "mqtt_host",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
    "mqtt_psk_identity",
    "mqtt_psk",
    "mqtt_use_factory_defaults",
    "ap_username",
    "ap_password",
}


def test_m1_field_entry() -> None:
    data, options = migration.migrate_1_1_to_1_2(FIELD_V1, {})
    assert data == {**FIELD_V1, "object_id_host": HOST}
    assert set(options) == OPTION_KEYS
    assert options["mqtt_host"] == ""  # was forced = host: now automatic
    assert options["mqtt_psk"] == FIELD_V1["mqtt_psk"]
    assert options["enable_mqtt"] is True
    assert options["ac_utc_offset"] == "ha"
    assert options["web_busy_grace_polls"] == 3
    assert options["stale_device_days"] == 7
    assert options["mqtt_use_factory_defaults"] is False


def test_m1b_retired_ap_login_is_kept_but_not_defaulted() -> None:
    _, options = migration.migrate_1_1_to_1_2(FIELD_V1, {})
    assert options["ap_username"] == FIELD_V1["ap_username"]
    assert options["ap_password"] == FIELD_V1["ap_password"]
    assert "ap_username" not in config_logic.DEFAULT_OPTIONS
    assert "ap_password" not in config_logic.SECRET_OPTIONS
    assert set(config_logic.RETIRED_OPTIONS) == {"ap_username", "ap_password"}


def test_m2_minimal_entry_gets_defaults() -> None:
    data, options = migration.migrate_1_1_to_1_2(
        {"base_url": f"http://{HOST}:8080", "username": "u", "password": "p"}, {}
    )
    assert options == config_logic.DEFAULT_OPTIONS
    assert options["enable_mqtt"] is False
    assert data["object_id_host"] == HOST


def test_m3_hand_edited_mqtt_host_is_kept() -> None:
    _, options = migration.migrate_1_1_to_1_2({**FIELD_V1, "mqtt_host": "192.0.2.1"}, {})
    assert options["mqtt_host"] == "192.0.2.1"


def test_m4_old_hardware_key() -> None:
    data = {k: v for k, v in FIELD_V1.items() if k != "enable_hardware_status"}
    _, options = migration.migrate_1_1_to_1_2({**data, "enable_olt_status": False}, {})
    assert options["enable_hardware_status"] is False


def test_m5_existing_options_win() -> None:
    _, options = migration.migrate_1_1_to_1_2(
        {**FIELD_V1, "web_busy_grace_polls": 2}, {"web_busy_grace_polls": 5}
    )
    assert options["web_busy_grace_polls"] == 5


def test_m6_scan_interval_minimum() -> None:
    _, options = migration.migrate_1_1_to_1_2({**FIELD_V1, "scan_interval": 15}, {})
    assert options["scan_interval"] == 30


def test_m7_stale_after_clamped_to_retention() -> None:
    _, options = migration.migrate_1_1_to_1_2(
        {**FIELD_V1, "station_stale_after": 7200, "station_retention": 3600}, {}
    )
    assert options["station_stale_after"] == 3600


def test_m8_idempotent() -> None:
    once = migration.migrate_1_1_to_1_2(FIELD_V1, {})
    twice = migration.migrate_1_1_to_1_2(*once)
    assert twice == once


def test_m9_object_id_host_matches_v1_slug() -> None:
    for base_url, host in (
        (f"http://{HOST}:8080/", HOST),
        ("http://AC.Lan:8080", "ac.lan"),
    ):
        data, _ = migration.migrate_1_1_to_1_2({**FIELD_V1, "base_url": base_url}, {})
        assert data["object_id_host"] == host


def test_m10_missing_base_url_raises() -> None:
    for data in ({}, {"username": "u"}):
        with pytest.raises(ValueError):
            migration.migrate_1_1_to_1_2(data, {})


def test_m11_data_only_grows() -> None:
    data, _ = migration.migrate_1_1_to_1_2(FIELD_V1, {"ap_area_id": "office"})
    assert set(FIELD_V1) <= set(data)
    assert all(data[key] == value for key, value in FIELD_V1.items())


def test_is_legacy_unique_id() -> None:
    assert migration.is_legacy_unique_id(None)
    assert migration.is_legacy_unique_id(f"http://{HOST}:8080")
    assert not migration.is_legacy_unique_id("e0:21:fe:c0:c0:00")
    assert not migration.is_legacy_unique_id("RL0000000000003")


def test_normalize_host() -> None:
    for value in (HOST, f"http://{HOST}:8080/", f"{HOST}:8080", f" {HOST} "):
        assert config_logic.normalize_host(value) == HOST
    assert config_logic.normalize_host("AC.lan") == "ac.lan"
    for bad in (f"{HOST}:8081", "a b", "http://x/path", "", "ftp://x", "http://x?q=1"):
        with pytest.raises(config_logic.InvalidHost):
            config_logic.normalize_host(bad)
    assert config_logic.normalize_host("192.0.2.1:8883", allowed_port=8883) == (
        "192.0.2.1"
    )


def test_merge_options() -> None:
    current = {**config_logic.DEFAULT_OPTIONS, "mqtt_psk": "aabb", "ap_password": "x"}
    merged = config_logic.merge_options(
        current,
        {
            "scan_interval": 45,
            "clients": {"station_retention": 600, "station_stale_after": 900},
            "mqtt": {
                "mqtt_psk": "",
                "mqtt_host": "http://192.0.2.1:8883",
                "mqtt_password": "  ",
            },
            # Retired key from an old form: ignored, stored value kept.
            "ap": {"ap_password": ""},
        },
    )
    assert merged["scan_interval"] == 45
    assert merged["station_retention"] == 600
    assert merged["station_stale_after"] == 600
    assert merged["mqtt_psk"] == "aabb"
    assert merged["mqtt_password"] == "123456"
    assert merged["ap_password"] == "x"
    assert merged["mqtt_host"] == "192.0.2.1"
    assert "clients" not in merged
    # A text PSK is stored as hex.
    assert config_logic.merge_options(current, {"mqtt_psk": "text-psk!"})[
        "mqtt_psk"
    ] == "746578742d70736b21"
    assert config_logic.auto_title(HOST) == "RLTech FTTR 198.51.100.29"


URL = f"http://{HOST}:8080"
MAC = "E0:21:FE:AA:BB:E0"
MAC_ID = "e0:21:fe:aa:bb:e0"


def decide(current, lan_mac=None, serial=None, taken=()):
    return migration.decide_unique_id_upgrade(
        current, lan_mac=lan_mac, serial=serial, taken=taken
    )


def test_unique_id_decisions_u1_to_u9() -> None:
    m = migration
    assert decide(URL, MAC, "RL1") == m.UniqueIdDecision(m.UPGRADE, MAC_ID)  # U1
    assert decide(URL, None, "RL0000000000003") == m.UniqueIdDecision(
        m.UPGRADE, "RL0000000000003"
    )  # U2
    assert decide(URL) == m.UniqueIdDecision(m.WAIT)  # U3
    assert decide(None, MAC).action == m.UPGRADE  # U4
    assert decide(URL, MAC, taken={MAC_ID}) == m.UniqueIdDecision(
        m.DUPLICATE, MAC_ID
    )  # U5
    assert decide(MAC_ID, MAC).action == m.NOOP  # U6
    assert decide(MAC_ID, "E0:21:FE:AA:BB:E1").action == m.MISMATCH  # U7
    assert decide("RL0000000000003", MAC, "RL0000000000003").action == m.NOOP  # U8
    assert decide(URL, "00:00:00:00:00:00").action == m.WAIT  # U9
    assert decide(URL, "FF:FF:FF:FF:FF:FF").action == m.WAIT
    assert decide(URL, "01:00:5E:00:00:01").action == m.WAIT  # multicast


def test_unique_id_kind_and_ac_unique_id() -> None:
    assert migration.unique_id_kind(URL) == "legacy_url"
    assert migration.unique_id_kind(MAC_ID) == "mac"
    assert migration.unique_id_kind("RL0000000000003") == "serial"
    assert migration.unique_id_kind(None) == "none"
    assert migration.ac_unique_id("e021feaabbe0", None) == MAC_ID
    assert migration.ac_unique_id(None, " rl1 ") == "RL1"
    assert migration.ac_unique_id(None, "N/A") is None


def test_integration_registers_no_update_listener() -> None:
    # Runtime unique_id updates must not reload the entry (plan 4.5).
    from test_settings import PKG_ROOT

    for path in PKG_ROOT.glob("*.py"):
        assert "add_update_listener" not in path.read_text(encoding="utf-8"), path
