"""Config entry migration and unique_id decisions (pure; no HA imports).

Version policy (plan 4.1, Q1): 1.1 -> 1.2 is a *minor* migration that only
adds. Non-connection settings are copied into ``options`` and every v1 key
stays in ``data``, so rolling back to the previous release still loads the
entry (HA allows a higher minor version with the same major version).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Any

from .config_logic import (
    DEFAULT_OPTIONS,
    MIN_SCAN_INTERVAL,
    RETIRED_OPTIONS,
    clamp_station_times,
    normalize_mqtt_psk,
)
from .settings import KEY_BASE_URL, KEY_OBJECT_ID_HOST, base_url_to_host

CURRENT_VERSION = 1
CURRENT_MINOR_VERSION = 2

# v1 keys copied into options with their defaults (plan 4.2).
_COPIED_KEYS = (
    "enable_ap_polling",
    "enable_station_polling",
    "legacy_username",
    "legacy_password",
    "legacy_hosts",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
    "mqtt_psk_identity",
)


def _host_key(value: str) -> str:
    return value.strip().lower().rstrip(".")


def migrate_1_1_to_1_2(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (data, options) for version 1.2. Raises ValueError on bad data.

    Idempotent: running it on its own output returns the same result.
    """
    if not data.get(KEY_BASE_URL):
        raise ValueError("entry has no base_url")
    host = base_url_to_host(str(data[KEY_BASE_URL]))
    new_data = dict(data)
    new_data.setdefault(KEY_OBJECT_ID_HOST, host)

    def value(key: str) -> Any:
        # Existing options win over data, then the default.
        if key in options:
            return options[key]
        if key in data:
            return data[key]
        return DEFAULT_OPTIONS[key]

    migrated: dict[str, Any] = {key: value(key) for key in _COPIED_KEYS}
    # Retired keys: copied only when stored (no default), so a rollback to a
    # release that reads them from options still finds the user's values.
    for key in RETIRED_OPTIONS:
        if key in options or key in data:
            migrated[key] = value(key)
    migrated["scan_interval"] = max(MIN_SCAN_INTERVAL, int(value("scan_interval")))
    migrated["web_busy_grace_polls"] = max(0, int(value("web_busy_grace_polls")))
    migrated["stale_device_days"] = max(0.0, float(value("stale_device_days")))
    migrated["ac_utc_offset"] = str(value("ac_utc_offset"))
    migrated["station_retention"], migrated["station_stale_after"] = (
        clamp_station_times(value("station_retention"), value("station_stale_after"))
    )
    migrated["enable_ap_polling"] = bool(migrated["enable_ap_polling"])
    migrated["enable_station_polling"] = bool(migrated["enable_station_polling"])
    if "enable_hardware_status" in options:
        hardware = options["enable_hardware_status"]
    elif "enable_hardware_status" in data:
        hardware = data["enable_hardware_status"]
    else:
        hardware = data.get("enable_olt_status", True)
    migrated["enable_hardware_status"] = bool(hardware)
    migrated["enable_mqtt"] = bool(value("enable_mqtt"))
    # v1 forced mqtt_host to the main host; that value means "automatic".
    mqtt_host = str(value("mqtt_host") or "").strip()
    migrated["mqtt_host"] = (
        "" if not mqtt_host or _host_key(mqtt_host) == _host_key(host) else mqtt_host
    )
    migrated["mqtt_psk"] = normalize_mqtt_psk(value("mqtt_psk"))
    migrated["mqtt_use_factory_defaults"] = bool(value("mqtt_use_factory_defaults"))
    area = options.get("ap_area_id", data.get("ap_area_id"))
    if area:
        migrated["ap_area_id"] = area

    new_options = {**migrated, **{k: v for k, v in options.items() if k not in migrated}}
    return new_data, new_options


def is_legacy_unique_id(unique_id: str | None) -> bool:
    """Return whether an entry unique_id predates the hardware id (D8)."""
    return unique_id is None or "://" in unique_id


_MAC_RE = re.compile(r"[0-9A-Fa-f]{12}")


def _canonical_mac(value: str | None) -> str | None:
    """Return a valid unicast MAC as HA's format_mac (lowercase, colons)."""
    if not value:
        return None
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if not _MAC_RE.fullmatch(compact):
        return None
    compact = compact.lower()
    if compact in {"000000000000", "ffffffffffff"} or int(compact[:2], 16) & 1:
        return None
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


def ac_unique_id(lan_mac: str | None, serial: str | None) -> str | None:
    """Return the config entry unique_id for an AC (D8, Q2).

    The AC LAN MAC in format_mac form; the serial number only when no valid
    MAC is known. Returns None when neither is usable.
    """
    if (mac := _canonical_mac(lan_mac)) is not None:
        return mac
    text = str(serial or "").strip().upper()
    if not text or text in {"N/A", "NULL", "-"}:
        return None
    return text


UPGRADE = "upgrade"
NOOP = "noop"
WAIT = "wait"
DUPLICATE = "duplicate"
MISMATCH = "mismatch"


@dataclass(frozen=True)
class UniqueIdDecision:
    """Outcome of the runtime unique_id check (plan 4.5)."""

    action: str
    unique_id: str | None = None


def unique_id_kind(unique_id: str | None) -> str:
    """Return legacy_url / mac / serial / none (for diagnostics)."""
    if unique_id is None:
        return "none"
    if "://" in unique_id:
        return "legacy_url"
    return "mac" if _canonical_mac(unique_id) == unique_id else "serial"


def decide_unique_id_upgrade(
    current: str | None,
    *,
    lan_mac: str | None,
    serial: str | None,
    taken: Iterable[str],
) -> UniqueIdDecision:
    """Decide how to move a legacy URL unique_id to the AC hardware id.

    A hardware unique_id is never switched to the other form (serial <-> MAC);
    a different value of the same form is only reported (MISMATCH).
    """
    if not is_legacy_unique_id(current):
        assert current is not None
        if unique_id_kind(current) == "mac":
            same_form = _canonical_mac(lan_mac)
        else:
            same_form = ac_unique_id(None, serial)
        if same_form is not None and same_form != current:
            return UniqueIdDecision(MISMATCH, same_form)
        return UniqueIdDecision(NOOP)
    candidate = ac_unique_id(lan_mac, serial)
    if candidate is None:
        return UniqueIdDecision(WAIT)
    if candidate in set(taken):
        return UniqueIdDecision(DUPLICATE, candidate)
    return UniqueIdDecision(UPGRADE, candidate)
