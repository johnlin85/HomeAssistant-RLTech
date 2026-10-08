"""Shared helpers for the Home Assistant runtime tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rltech_fttr import api
from custom_components.rltech_fttr.const import DOMAIN
from custom_components.rltech_fttr.models import RltechData

FIXTURES = Path(__file__).parents[1] / "fixtures"
HOST = "198.51.100.29"
LAN_MAC = "E0:21:FE:C0:C0:00"
AP_A_SN = "RLGMFEA0A000"
AP_B_SN = "RLGMFEB0B000"

# Same key set as a real version 1 entry from the field,
# fake values.
V1_DATA: dict[str, Any] = {
    "base_url": f"http://{HOST}:8080",
    "username": "useradmin",
    "password": "fake-password",
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
    "mqtt_password": "fake-mqtt-password",
    "mqtt_psk_identity": "admin",
    "mqtt_psk": "0011223344556677",
    "ap_username": "useradmin",
    "ap_password": "1234",
}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def real_snapshot(now: datetime | None = None) -> RltechData:
    """Build a snapshot from the sanitized real 8080 pages."""
    now = now or datetime.now(UTC)
    ap_page = api.extract_embedded_json(
        fixture_text("real_ap_online_list.html"), "AP_manage"
    )
    sta_page = api.extract_embedded_json(
        fixture_text("real_ap_wlan_ac_client_list.html"), "STA_manage"
    )
    data = api.normalize_snapshot(
        [ap_page],
        [sta_page],
        olt_html=fixture_text("real_sta_device.html"),
        user_html=fixture_text("real_sta_user.html"),
        now=now,
    )
    lan_ip, lan_mac = api.parse_ac_lan_identity(fixture_text("real_sta_user.html"))
    status = replace(data.olt_status, lan_ip=lan_ip, lan_mac=lan_mac)
    return replace(
        data,
        olt_status=status,
        web_status=status,
        web_lan_ports=data.lan_ports,
        web_lanpon_ports=data.lanpon_ports,
        web_status_update=now,
        # Uplink PON without fibre, as on site (stage 6).
        uplink_pon=api.parse_uplink_pon(fixture_text("sta_network_link_down.html")),
    )


class FakeAc:
    """Scripted replacement for RltechClient.fetch_snapshot."""

    def __init__(self) -> None:
        self.data = real_snapshot()
        self.error: Exception | None = None
        self.port80_answers = False
        self.calls = 0
        self.kwargs: dict[str, Any] = {}
        self.hook: Callable[[api.RltechClient], None] | None = None

    async def fetch_snapshot(self, client: api.RltechClient, session, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        client.last_web_attempted = True
        if self.hook is not None:
            self.hook(client)
        if self.error is not None:
            client.last_web_error = self.error
            if self.port80_answers:
                # 8080 failed but port 80 refreshed: data comes back anyway.
                return self.data
            raise self.error
        client.last_web_error = None
        return self.data


def make_entry(*, mqtt: bool = False, **kwargs) -> MockConfigEntry:
    """Return a v1 entry like the field one; MQTT off unless asked for."""
    values = {
        "domain": DOMAIN,
        "title": f"RLTech FTTR {HOST}",
        "data": {**V1_DATA, "enable_mqtt": mqtt},
        "unique_id": f"http://{HOST}:8080",
        "version": 1,
        "minor_version": 1,
    }
    values.update(kwargs)
    return MockConfigEntry(**values)
