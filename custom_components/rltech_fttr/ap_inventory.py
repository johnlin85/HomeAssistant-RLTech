"""Access point inventory serialization for RLTech FTTR."""

from __future__ import annotations

from typing import Any

from .models import RltechAp, RltechApDetail, RltechData


def _detail_fields(detail: RltechApDetail | None) -> dict[str, Any]:
    if detail is None:
        return {}
    return {
        "detail_last_update": detail.last_update.isoformat() if detail.last_update else None,
        "optical_rx_power": detail.optical_rx_power,
        "optical_tx_power": detail.optical_tx_power,
        "cpu_usage": detail.cpu_usage,
        "cpu_temperature": detail.cpu_temperature,
        "memory_usage": detail.memory_usage,
        "flash_usage": detail.flash_usage,
        "last_boot": detail.last_boot.isoformat() if detail.last_boot else None,
        "reg_off_time": detail.reg_off_time.isoformat() if detail.reg_off_time else None,
        "last_down_cause": detail.last_down_cause,
        "onu_status": detail.onu_status,
        "interface": detail.interface,
        "source_host": detail.source_host,
    }


def _optical_fields(ap: RltechAp, detail: RltechApDetail | None) -> dict[str, Any]:
    """Prefer detail optics, falling back to the every-poll AP list values."""
    rx_power = detail.optical_rx_power if detail is not None else None
    tx_power = detail.optical_tx_power if detail is not None else None
    return {
        "optical_rx_power": rx_power if rx_power is not None else ap.optical_rx_power,
        "optical_tx_power": tx_power if tx_power is not None else ap.optical_tx_power,
    }


def ap_to_row(
    ap: RltechAp,
    registry_info: dict[str, Any] | None = None,
    detail: RltechApDetail | None = None,
) -> dict[str, Any]:
    """Return one AP row for UI/service use."""
    registry_info = registry_info or {}
    return {
        "device_id": registry_info.get("device_id"),
        "hardware_id": ap.sn,
        "mac": ap.mac,
        "alias": ap.alias,
        "ip": ap.ip,
        "online": ap.online,
        "model": ap.model,
        "version": ap.version,
        "profile": ap.profile,
        "profile_idx": ap.profile_idx,
        "channel_24": ap.channel_24,
        "channel_5": ap.channel_5,
        "bssid_24": ap.bssid_24,
        "bssid_5": ap.bssid_5,
        "assoc_count": ap.assoc_count,
        "uplink": ap.uplink,
        "uplink_port": ap.uplink_port,
        "sn": ap.sn,
        "dev_sn": ap.dev_sn,
        "upgrade_flag": ap.upgrade_flag,
        "entities": registry_info.get("entities") or {},
        # Entity keys whose HA entity exists but is disabled (not clickable).
        "disabled_entities": registry_info.get("disabled_entities") or [],
        **_detail_fields(detail),
        **_optical_fields(ap, detail),
    }


def ap_rows(
    data: RltechData | None,
    registry_info: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return stable, sorted AP rows from coordinator data."""
    if data is None:
        return []
    # Clients the AC reports as online per AP, next to the AP's own Assoc.
    counts: dict[str, int] = {}
    for station in data.stations.values():
        if station.reported_online and station.ap_mac:
            counts[station.ap_mac] = counts.get(station.ap_mac, 0) + 1
    return [
        {
            **ap_to_row(
                ap, (registry_info or {}).get(ap.mac), data.ap_details.get(ap.mac)
            ),
            "station_count_reported": counts.get(ap.mac, 0),
        }
        for ap in sorted(
            data.aps.values(), key=lambda item: ((item.alias or "").lower(), item.mac)
        )
    ]
