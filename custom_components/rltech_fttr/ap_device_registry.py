"""Pure device/entity registry decisions for RLTech FTTR.

Nothing here imports Home Assistant, so every rule of the device model
(plan 3.4.2) can be unit tested. devices.py applies the decisions to the
real registries.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlunsplit

from .models import RltechAp

DOMAIN = "rltech_fttr"
CONNECTION_NETWORK_MAC = "mac"
AP_MANUFACTURER = "RLTech"

# 3.4.2: never start the missing timer while the AC may still be rebuilding
# its /tmp AP database after a reboot.
AC_REBOOT_SETTLE = timedelta(minutes=30)
DEFAULT_STALE_DEVICE_DAYS = 7

# Entities older versions created that the current code only creates when
# port 80 works (stage 2 S5). Unique-id suffixes after "<entry_id>_".
LEGACY_ONLY_CONTROLLER_UNIQUE_SUFFIXES = ("olt_cpu_usage", "olt_memory_usage")
LEGACY_ONLY_AP_UNIQUE_SUFFIX = "_source_host"


def ap_configuration_url(ap: RltechAp) -> str | None:
    """Return the AP web UI URL when the AP has an IP address."""
    if not ap.ip:
        return None
    return urlunsplit(("http", ap.ip, "", "", ""))


def controller_identifier(entry_id: str) -> tuple[str, str]:
    """Return the controller (AC) device identifier.

    Single place to change when stage 4 (D8) moves the controller identity to
    the AC LAN MAC / serial.
    """
    return (DOMAIN, entry_id)


def ap_identifier(entry_id: str, sn: str) -> tuple[str, str]:
    """Return the AP device identifier (AP identity is its SN)."""
    return (DOMAIN, f"{entry_id}_ap_{sn}")


def legacy_olt_identifier(entry_id: str, host: str) -> tuple[str, str]:
    """Return the identifier of an additional port-80 OLT device."""
    return (DOMAIN, f"{entry_id}_legacy_olt_{host}")


def ap_mac_connection(ap: RltechAp) -> tuple[str, str]:
    """Return the device registry MAC connection of an AP (lowercase)."""
    return (CONNECTION_NETWORK_MAC, ap.mac.lower())


def ap_device_name(ap: RltechAp) -> str:
    """Return the integration-owned AP device name (alias, else SN, else MAC)."""
    return f"RLTech AP {ap.alias or ap.sn or ap.mac}"


def sn_from_ap_identifiers(
    entry_id: str, identifiers: Iterable[Iterable[str]]
) -> str | None:
    """Return the AP SN encoded in a device's identifiers, if it is our AP."""
    prefix = f"{entry_id}_ap_"
    for item in identifiers:
        domain, value = tuple(item)
        if domain == DOMAIN and value.startswith(prefix):
            return value[len(prefix) :] or None
    return None


def ap_device_registry_updates(
    ap: RltechAp,
    device: Any,
    *,
    controller_device_id: str | None,
    area_id: str | None = None,
) -> dict[str, Any]:
    """Return AP device registry updates for integration-owned metadata.

    Only fields the integration owns are compared. ``name`` is the
    integration's name; a user rename lives in ``name_by_user`` and is never
    touched. Missing values in the AP row never clear stored ones.
    """
    updates: dict[str, Any] = {}

    name = ap_device_name(ap)
    if getattr(device, "name", None) != name:
        updates["name"] = name

    if ap.model and getattr(device, "model", None) != ap.model:
        updates["model"] = ap.model

    if ap.version and getattr(device, "sw_version", None) != ap.version:
        updates["sw_version"] = ap.version

    if ap.sn and getattr(device, "serial_number", None) != ap.sn:
        updates["serial_number"] = ap.sn

    configuration_url = ap_configuration_url(ap)
    if (
        configuration_url
        and getattr(device, "configuration_url", None) != configuration_url
    ):
        updates["configuration_url"] = configuration_url

    if (
        controller_device_id
        and getattr(device, "via_device_id", None) != controller_device_id
    ):
        updates["via_device_id"] = controller_device_id

    # Same SN, new MAC (NIC/optical module replaced): replace, not merge, the
    # MAC connection; keep any non-MAC connections.
    connection = ap_mac_connection(ap)
    connections = set(getattr(device, "connections", set()) or set())
    if connection not in connections:
        updates["new_connections"] = {
            item for item in connections if item[0] != CONNECTION_NETWORK_MAC
        } | {connection}

    if area_id and getattr(device, "area_id", None) is None:
        updates["area_id"] = area_id

    return updates


@dataclass(frozen=True)
class ConnectionStrip:
    """Remove a MAC connection from another device before registering an AP."""

    device_id: str
    new_connections: set[tuple[str, str]]


def mac_connection_conflict(
    ap: RltechAp,
    *,
    own_device: Any | None,
    connection_holder: Any | None,
) -> ConnectionStrip | None:
    """Return the strip needed so an AP's MAC is not claimed by another device.

    D7: the same MAC with a new SN is a new device. The registry matches a
    device by identifiers *or* connections, so if the old SN's device still
    holds the MAC connection, registering the new SN would be merged into the
    old device (and HA raises if both were registered this session). Strip
    the MAC from the other device first; the old device then ages out via
    the missing/stale rules.
    """
    if connection_holder is None:
        return None
    if own_device is not None and connection_holder.id == own_device.id:
        return None
    connection = ap_mac_connection(ap)
    return ConnectionStrip(
        device_id=connection_holder.id,
        new_connections=set(connection_holder.connections) - {connection},
    )


def update_missing_since(
    missing: Mapping[str, datetime],
    *,
    known_sns: Iterable[str],
    listed_sns: Iterable[str],
    now: datetime,
    list_fresh: bool,
    ac_last_boot: datetime | None,
) -> dict[str, datetime]:
    """Return the updated "missing since" map, keyed by AP SN.

    - An AP listed again is removed from the map.
    - Timers only start/continue on a fresh, non-empty 8080 AP list and when
      the AC has been up for at least 30 minutes (an AC reboot empties
      /tmp/acap.db until the APs register again).
    - Existing timers are kept (not reset) while counting is suspended.
    """
    listed = set(listed_sns)
    result = {sn: since for sn, since in missing.items() if sn not in listed}
    counting = (
        list_fresh
        and bool(listed)
        and not (ac_last_boot is not None and now - ac_last_boot < AC_REBOOT_SETTLE)
    )
    if not counting:
        return result
    for sn in known_sns:
        if sn not in listed and sn not in result:
            result[sn] = now
    return result


def stale_sns(
    missing: Mapping[str, datetime], *, now: datetime, days: float
) -> list[str]:
    """Return SNs missing for at least ``days`` days (0 disables auto removal)."""
    if days <= 0:
        return []
    limit = timedelta(days=days)
    return sorted(sn for sn, since in missing.items() if now - since >= limit)


def device_removal_allowed(
    entry_id: str,
    identifiers: Iterable[Iterable[str]],
    *,
    listed_sns: Iterable[str],
    legacy_hosts: Iterable[str],
) -> bool:
    """Decide ``async_remove_config_entry_device`` (D6).

    The controller can never be removed; an AP or extra port-80 OLT only when
    it is no longer reported (otherwise it would be recreated at once).
    """
    items = [tuple(item) for item in identifiers]
    if controller_identifier(entry_id) in items:
        return False
    sn = sn_from_ap_identifiers(entry_id, items)
    if sn is not None:
        return sn not in set(listed_sns)
    hosts = set(legacy_hosts)
    for domain, value in items:
        prefix = f"{entry_id}_legacy_olt_"
        if domain == DOMAIN and value.startswith(prefix):
            return value[len(prefix) :] not in hosts
    return False


def is_legacy_only_unique_id(entry_id: str, unique_id: str) -> bool:
    """Return whether a unique_id belongs to a port-80-only entity."""
    if unique_id in {
        f"{entry_id}_{suffix}" for suffix in LEGACY_ONLY_CONTROLLER_UNIQUE_SUFFIXES
    }:
        return True
    return unique_id.startswith(f"{entry_id}_ap_") and unique_id.endswith(
        LEGACY_ONLY_AP_UNIQUE_SUFFIX
    )


def legacy_only_entities_to_remove(
    entries: Iterable[Any],
    *,
    entry_id: str,
    hardware_status_enabled: bool,
    legacy_ever_succeeded: bool,
    legacy_confirmed_down: bool,
    wan_deployment_confirmed: bool,
) -> list[str]:
    """Return entity_ids of old port-80-only entities that will not return (F1).

    Strict on purpose: only this integration's entities of this config entry,
    only the known port-80-only unique_ids, and only when the current code
    would not create them: hardware status is off, or port 80 has never
    answered, its circuit breaker tripped, and this is confirmed to be a WAN
    deployment (stage 3 review S5: a LAN install whose port 80 comes up late
    must not lose them).
    """
    if hardware_status_enabled and (
        legacy_ever_succeeded
        or not legacy_confirmed_down
        or not wan_deployment_confirmed
    ):
        return []
    return sorted(
        entry.entity_id
        for entry in entries
        if getattr(entry, "platform", None) == DOMAIN
        and getattr(entry, "config_entry_id", None) == entry_id
        and is_legacy_only_unique_id(entry_id, getattr(entry, "unique_id", ""))
    )


def wan_deployment_confirmed(
    *, host: str, lan_ip: str | None, reachable: Mapping[str, bool] | None
) -> bool:
    """Return True only for a confirmed WAN deployment (plan 5.4).

    The AC LAN IP is known and differs from the configured host, and the last
    endpoint probe found port 80 closed on both.
    """
    if not lan_ip or lan_ip == host or reachable is None:
        return False
    keys = (f"{host}:80", f"{lan_ip}:80")
    return all(key in reachable and reachable[key] is False for key in keys)


def should_warn(step: str, error_type: str, seen: set[tuple[str, str]]) -> bool:
    """Warn once per (step, exception type); record it in ``seen``."""
    key = (step, error_type)
    if key in seen:
        return False
    seen.add(key)
    return True
