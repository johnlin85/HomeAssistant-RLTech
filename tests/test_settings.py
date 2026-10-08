"""Tests for settings.py (entry value resolution, AC time zone)."""

from __future__ import annotations

from datetime import timedelta, timezone
from importlib import util
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PKG_ROOT = Path(__file__).parents[1] / "custom_components" / "rltech_fttr"


def load_module(name: str):
    module_name = f"custom_components.rltech_fttr.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = util.spec_from_file_location(module_name, PKG_ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


settings = load_module("settings")


def entry(data=None, options=None):
    return SimpleNamespace(data=data or {}, options=options or {})


def test_entry_value_prefers_options_then_data_then_default() -> None:
    e = entry({"scan_interval": 60, "enable_mqtt": True}, {"scan_interval": 30})
    assert settings.entry_value(e, "scan_interval", 99) == 30
    assert settings.entry_value(e, "enable_mqtt", False) is True
    assert settings.entry_value(e, "missing", 7) == 7
    # Presence, not truthiness: False / 0 / "" in options still win.
    e = entry({"enable_mqtt": True, "stale_device_days": 7}, {"enable_mqtt": False})
    assert settings.entry_value(e, "enable_mqtt", True) is False
    e = entry({"stale_device_days": 7}, {"stale_device_days": 0})
    assert settings.entry_value(e, "stale_device_days", 7) == 0
    e = entry({"mqtt_host": "198.51.100.29"}, {"mqtt_host": ""})
    assert settings.entry_value(e, "mqtt_host") == ""


def test_hardware_status_enabled_falls_back_to_the_old_key() -> None:
    assert settings.hardware_status_enabled(entry()) is True
    assert settings.hardware_status_enabled(entry({"enable_olt_status": False})) is False
    assert (
        settings.hardware_status_enabled(
            entry({"enable_olt_status": False, "enable_hardware_status": True})
        )
        is True
    )
    assert (
        settings.hardware_status_enabled(
            entry({"enable_hardware_status": True}, {"enable_hardware_status": False})
        )
        is False
    )


def test_entry_host_and_object_id_host() -> None:
    e = entry({"base_url": "http://198.51.100.29:8080/"})
    assert settings.entry_host(e) == "198.51.100.29"
    assert settings.entry_base_url(e) == "http://198.51.100.29:8080"
    assert settings.object_id_host(e) == "198.51.100.29"
    e = entry({"base_url": "http://192.0.2.1:8080", "object_id_host": "198.51.100.29"})
    assert settings.entry_host(e) == "192.0.2.1"
    assert settings.object_id_host(e) == "198.51.100.29"
    assert settings.base_url_to_host("http://ac.lan:8080") == "ac.lan"


def test_ac_timezone() -> None:
    ha_tz = ZoneInfo("Asia/Singapore")
    assert settings.ac_timezone("ha", ha_tz) is ha_tz
    assert settings.ac_timezone("+07:00", ha_tz) == timezone(timedelta(hours=7))
    assert settings.ac_timezone("+05:45", ha_tz) == timezone(
        timedelta(hours=5, minutes=45)
    )
    assert settings.ac_timezone("-03:30", ha_tz) == timezone(
        -timedelta(hours=3, minutes=30)
    )
    for bad in (None, "", "UTC+7", "+15:00", "+07:99", "junk"):
        assert settings.ac_timezone(bad, ha_tz) is ha_tz
    choices = settings.utc_offset_choices()
    assert choices[0] == "ha"
    assert {"-12:00", "+00:00", "+07:00", "+05:45", "+08:45", "+12:45", "+14:00"} <= set(
        choices
    )
    for choice in choices[1:]:
        assert settings.ac_timezone(choice, ha_tz) is not ha_tz
