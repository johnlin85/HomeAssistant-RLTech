"""Hostname enrichment helpers for RLTech FTTR stations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any, Iterable

from .dhcp_enrichment import clean_hostname, enrich_station_hostnames, normalize_lookup_mac
from .models import RltechData, RltechStation

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

else:
    HomeAssistant = Any


LookupFn = Callable[[HomeAssistant], tuple[dict[str, str], dict[str, str]]]


def enrich_from_home_assistant_dhcp(
    hass: HomeAssistant,
    data: RltechData,
    lookup_fn: LookupFn | None = None,
) -> RltechData:
    """Fill station hostnames from Home Assistant's DHCP discovery cache."""
    lookup_fn = lookup_fn or _dhcp_hostname_lookup
    return enrich_station_hostnames(data, *lookup_fn(hass))


def dhcp_match_summary(
    hass: HomeAssistant,
    stations: Iterable[RltechStation],
    lookup_fn: LookupFn | None = None,
) -> dict[str, Any]:
    """Return redacted DHCP enrichment match counts for diagnostics."""
    lookup_fn = lookup_fn or _dhcp_hostname_lookup
    by_mac, by_ip = lookup_fn(hass)
    station_list = list(stations)
    stations_missing_hostname = [
        station for station in station_list if clean_hostname(station.hostname) is None
    ]
    mac_matches = sum(
        1
        for station in station_list
        if (normalize_lookup_mac(station.mac) or "") in by_mac
    )
    ip_matches = sum(1 for station in station_list if station.ip in by_ip)
    fillable_missing = sum(
        1
        for station in stations_missing_hostname
        if (normalize_lookup_mac(station.mac) or "") in by_mac or station.ip in by_ip
    )
    return {
        "dhcp_mac_count": len(by_mac),
        "dhcp_ip_count": len(by_ip),
        "station_count": len(station_list),
        "station_with_hostname_count": len(station_list)
        - len(stations_missing_hostname),
        "station_missing_hostname_count": len(stations_missing_hostname),
        "station_mac_match_count": mac_matches,
        "station_ip_match_count": ip_matches,
        "station_missing_hostname_fillable_count": fillable_missing,
    }


def _dhcp_hostname_lookup(hass: HomeAssistant) -> tuple[dict[str, str], dict[str, str]]:
    """Build DHCP hostname lookup maps once for a coordinator poll."""
    by_mac: dict[str, str] = {}
    by_ip: dict[str, str] = {}

    for mac_address, data in _dhcp_address_data(hass).items():
        hostname = clean_hostname(_info_value(data, "hostname"))
        if hostname is None:
            continue

        mac = normalize_lookup_mac(mac_address)
        if mac:
            by_mac[mac] = hostname

        ip = _info_value(data, "ip")
        if ip:
            by_ip[str(ip)] = hostname

    return by_mac, by_ip


def _dhcp_address_data(hass: HomeAssistant) -> dict[str, dict[str, str]]:
    """Return DHCP address data across Home Assistant helper API versions."""
    try:
        from homeassistant.components.dhcp.helpers import async_discovered_service_info
    except ImportError:
        async_discovered_service_info = None

    if async_discovered_service_info is not None:
        return {
            str(_info_value(info, "macaddress") or ""): {
                "hostname": str(_info_value(info, "hostname") or ""),
                "ip": str(_info_value(info, "ip") or ""),
            }
            for info in async_discovered_service_info(hass)
            if _info_value(info, "macaddress")
        }

    from homeassistant.components.dhcp.const import HOSTNAME, IP_ADDRESS
    from homeassistant.components.dhcp.helpers import async_get_address_data_internal

    return {
        str(mac_address): {
            "hostname": str(_info_value(data, HOSTNAME) or ""),
            "ip": str(_info_value(data, IP_ADDRESS) or ""),
        }
        for mac_address, data in async_get_address_data_internal(hass).items()
    }


def _info_value(info, key: str):
    """Return a DHCP discovery value from object or dict-like info."""
    if isinstance(info, dict):
        return info.get(key)
    return getattr(info, key, None)
