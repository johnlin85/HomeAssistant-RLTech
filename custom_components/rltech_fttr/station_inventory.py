"""Station inventory serialization for RLTech FTTR."""

from __future__ import annotations

from typing import Any

from .models import RltechData, RltechStation


def station_to_row(station: RltechStation) -> dict[str, Any]:
    """Return one station row for UI/service use."""
    return {
        "mac": station.mac,
        "ip": station.ip,
        "hostname": station.hostname,
        "vendor": station.vendor,
        "ssid": station.ssid,
        "ap_mac": station.ap_mac,
        "ap_alias": station.ap_alias,
        "rssi": station.rssi,
        "band": station.band,
        "channel": station.channel,
        "vlan": station.vlan,
        "uptime": station.uptime,
        "reported_online": station.reported_online,
        "first_seen": station.first_seen.isoformat() if station.first_seen else None,
        "last_seen": station.last_seen.isoformat() if station.last_seen else None,
        "home": station.home,
        "rx_rate": station.rx_rate,
        "tx_rate": station.tx_rate,
        "rx_nego_rate": station.rx_nego_rate,
        "tx_nego_rate": station.tx_nego_rate,
        "bandwidth": station.bandwidth,
        "total_count": station.total_count,
    }


def station_rows(data: RltechData | None) -> list[dict[str, Any]]:
    """Return stable, sorted station rows from coordinator data."""
    if data is None:
        return []
    return [
        station_to_row(station)
        for station in sorted(data.stations.values(), key=lambda item: item.mac)
    ]
