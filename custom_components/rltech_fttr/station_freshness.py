"""Station freshness helpers for RLTech FTTR."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import RltechData, RltechStation

MIN_STATION_STALE_AFTER = 60


def effective_station_stale_after(stale_after: int, retention: int) -> int:
    """Return a bounded station stale age."""
    effective_retention = max(int(retention), MIN_STATION_STALE_AFTER)
    return min(max(int(stale_after), MIN_STATION_STALE_AFTER), effective_retention)


def effective_station_retention(retention: int) -> int:
    """Return a sane station retention age."""
    return max(int(retention), MIN_STATION_STALE_AFTER)


def age_station_rows(
    stations: dict[str, RltechStation],
    *,
    now: datetime,
    stale_after: int,
    retention: int,
) -> dict[str, RltechStation]:
    """Mark stale stations inactive and remove expired station rows."""
    effective_retention = effective_station_retention(retention)
    effective_stale = effective_station_stale_after(stale_after, effective_retention)
    changed = False
    aged: dict[str, RltechStation] = {}
    for mac, station in stations.items():
        if station.last_seen is None:
            aged[mac] = station
            continue
        age = (now - station.last_seen).total_seconds()
        if age >= effective_retention:
            changed = True
            continue
        if age >= effective_stale and (station.reported_online or station.home):
            aged[mac] = replace(station, reported_online=False, home=False)
            changed = True
            continue
        aged[mac] = station
    return aged if changed else stations


def age_station_data(
    data: RltechData,
    *,
    now: datetime,
    stale_after: int,
    retention: int,
) -> RltechData:
    """Return data with station freshness applied."""
    stations = age_station_rows(
        data.stations,
        now=now,
        stale_after=stale_after,
        retention=retention,
    )
    if stations is data.stations:
        return data
    return replace(data, stations=stations)
