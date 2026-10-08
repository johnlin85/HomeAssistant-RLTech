"""Best-effort station hostname enrichment."""

from __future__ import annotations

from dataclasses import replace
import re

from .models import RltechData, RltechStation

_JUNK_HOSTNAMES = {"-", "--", "n/a", "na", "none", "null", "unknown"}


def clean_hostname(value: object) -> str | None:
    """Return a useful hostname or None."""
    if value is None:
        return None
    hostname = str(value).strip()
    if not hostname or hostname.lower() in _JUNK_HOSTNAMES:
        return None
    return hostname


def normalize_lookup_mac(value: object) -> str | None:
    """Normalize MAC addresses for lookup-map keys."""
    if value is None:
        return None
    normalized = re.sub(r"[^0-9a-fA-F]", "", str(value)).lower()
    return normalized or None


def enrich_station_hostnames(
    data: RltechData,
    by_mac: dict[str, str],
    by_ip: dict[str, str],
) -> RltechData:
    """Fill missing/junk station hostnames from DHCP lookup maps."""
    stations: dict[str, RltechStation] = {}
    changed = False

    for mac, station in data.stations.items():
        if clean_hostname(station.hostname):
            stations[mac] = station
            continue

        hostname = by_mac.get(normalize_lookup_mac(station.mac) or "")
        if hostname is None and station.ip:
            hostname = by_ip.get(station.ip)

        if hostname is None:
            stations[mac] = station
            continue

        stations[mac] = replace(station, hostname=hostname)
        changed = True

    if not changed:
        return data
    return replace(data, stations=stations)
