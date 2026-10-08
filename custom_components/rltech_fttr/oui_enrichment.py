"""Best-effort station MAC vendor enrichment."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import logging

from .models import RltechData, RltechStation

_LOGGER = logging.getLogger(__name__)


async def async_load_oui() -> None:
    """Load the local OUI database if aiooui is available."""
    try:
        import aiooui
    except ImportError as err:  # pragma: no cover - dependency is installed by HA
        _LOGGER.debug("aiooui is unavailable for station vendor enrichment: %s", err)
        return
    try:
        if not aiooui.is_loaded():
            await aiooui.async_load()
    except Exception as err:  # pragma: no cover - defensive around package data
        _LOGGER.debug("Unable to load OUI vendor data: %s", err)


def enrich_station_vendors(
    data: RltechData,
    lookup_fn: Callable[[str], str | None] | None = None,
) -> RltechData:
    """Fill missing station vendor names from the local OUI database."""
    lookup_fn = lookup_fn or _aiooui_vendor_lookup
    stations: dict[str, RltechStation] = {}
    changed = False
    for mac, station in data.stations.items():
        if station.vendor:
            stations[mac] = station
            continue
        vendor = _clean_vendor(lookup_fn(station.mac))
        if vendor is None:
            stations[mac] = station
            continue
        stations[mac] = replace(station, vendor=vendor)
        changed = True
    if not changed:
        return data
    return replace(data, stations=stations)


def _aiooui_vendor_lookup(mac: str) -> str | None:
    """Return an aiooui vendor lookup, or None if data is unavailable."""
    try:
        import aiooui
    except ImportError:
        return None
    try:
        if not aiooui.is_loaded():
            return None
        return aiooui.get_vendor(mac)
    except RuntimeError:
        return None


def _clean_vendor(value: object) -> str | None:
    """Return a useful vendor name or None."""
    if value is None:
        return None
    vendor = str(value).strip()
    return vendor or None
