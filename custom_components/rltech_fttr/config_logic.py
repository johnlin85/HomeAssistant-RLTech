"""Config form helpers for RLTech FTTR (pure; no Home Assistant imports)."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit

WEB_PORT = 8080
MQTT_PORT = 8883
MIN_SCAN_INTERVAL = 30
MIN_STATION_STALE_AFTER = 60

# Non-connection settings kept in entry.options from version 1.2 on
# (plan 3.6 / 4.2). Values mirror const.py defaults; tests/ha checks that.
DEFAULT_OPTIONS: dict[str, Any] = {
    "scan_interval": 60,
    "web_busy_grace_polls": 3,
    "stale_device_days": 7,
    "ac_utc_offset": "ha",
    "station_retention": 3600,
    "station_stale_after": 900,
    "enable_ap_polling": True,
    "enable_station_polling": True,
    "enable_hardware_status": True,
    "legacy_username": "admin",
    "legacy_password": "admin",
    "legacy_hosts": "",
    "enable_mqtt": False,
    "mqtt_host": "",
    "mqtt_port": MQTT_PORT,
    "mqtt_username": "admin",
    "mqtt_password": "123456",
    "mqtt_psk_identity": "admin",
    "mqtt_psk": "",
    "mqtt_use_factory_defaults": False,
}
# Secrets: an empty form value means "keep the stored one".
SECRET_OPTIONS = ("legacy_password", "mqtt_password", "mqtt_psk")
# Retired settings (stage 6: AP reboots go through the AC, no AP login). Not
# shown, not read, not defaulted; values already stored are kept untouched so
# an older release still finds them after a rollback.
RETIRED_OPTIONS = ("ap_username", "ap_password")

_HOST_RE = re.compile(r"[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\]")


class InvalidHost(ValueError):
    """The address entered is not a bare host / IP (optionally :8080)."""


def normalize_host(value: Any, *, allowed_port: int | None = WEB_PORT) -> str:
    """Return a bare host from ``host``, ``host:8080`` or ``http://host:8080/``.

    Any other port, a path, a query or whitespace inside is rejected.
    """
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        raise InvalidHost(text)
    parsed = urlsplit(text if "://" in text else f"http://{text}")
    if parsed.scheme not in {"http", "https"}:
        raise InvalidHost(text)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise InvalidHost(text)
    try:
        port = parsed.port
    except ValueError as err:
        raise InvalidHost(text) from err
    if port is not None and port != allowed_port:
        raise InvalidHost(text)
    host = parsed.hostname or ""
    if not host or not _HOST_RE.fullmatch(host if ":" not in host else f"[{host}]"):
        raise InvalidHost(text)
    return host


def host_to_base_url(host: str) -> str:
    """Return the fixed 8080 Web UI base URL for a host."""
    return f"http://{host}:{WEB_PORT}"


def auto_title(host: str) -> str:
    """Return the entry title the integration generates for a host."""
    return f"RLTech FTTR {host}"


def normalize_mqtt_psk(value: object) -> str:
    """Return a hex PSK, accepting either pasted hex or plain text."""
    text = str(value or "").strip()
    if not text:
        return ""
    compact = (
        text.replace(":", "")
        .replace("-", "")
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("\t", "")
    )
    if compact and len(compact) % 2 == 0:
        try:
            bytes.fromhex(compact)
        except ValueError:
            pass
        else:
            return compact.lower()
    return text.encode().hex()


def clamp_station_times(retention: Any, stale_after: Any) -> tuple[int, int]:
    """Return (retention, stale_after), both ≥ 60 and stale ≤ retention."""
    retention_value = max(int(retention), MIN_STATION_STALE_AFTER)
    stale_value = max(int(stale_after), MIN_STATION_STALE_AFTER)
    return retention_value, min(stale_value, retention_value)


def default_options() -> dict[str, Any]:
    """Return a fresh copy of the default options."""
    return dict(DEFAULT_OPTIONS)


def flatten_sections(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten ``section()`` groups of an options form into plain keys."""
    flat: dict[str, Any] = {}
    for key, value in user_input.items():
        if isinstance(value, Mapping):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def merge_options(current: Mapping[str, Any], user_input: Mapping[str, Any]) -> dict:
    """Merge an options form into the current options.

    Sections are flattened, blank secrets keep the stored value, the station
    times are clamped, the PSK is normalized to hex and the MQTT host is
    reduced to a bare host ("" = automatic).
    """
    merged = {**DEFAULT_OPTIONS, **dict(current)}
    for key, value in flatten_sections(user_input).items():
        if key in RETIRED_OPTIONS:
            # No field any more; never let a stale form overwrite them.
            continue
        if key in SECRET_OPTIONS and (value is None or str(value).strip() == ""):
            continue
        merged[key] = value
    merged["station_retention"], merged["station_stale_after"] = clamp_station_times(
        merged["station_retention"], merged["station_stale_after"]
    )
    merged["scan_interval"] = max(MIN_SCAN_INTERVAL, int(merged["scan_interval"]))
    merged["web_busy_grace_polls"] = max(0, int(merged["web_busy_grace_polls"]))
    merged["stale_device_days"] = max(0, float(merged["stale_device_days"]))
    merged["mqtt_psk"] = normalize_mqtt_psk(merged.get("mqtt_psk"))
    mqtt_host = str(merged.get("mqtt_host") or "").strip()
    merged["mqtt_host"] = (
        normalize_host(mqtt_host, allowed_port=MQTT_PORT) if mqtt_host else ""
    )
    merged["mqtt_port"] = MQTT_PORT
    if "ap_area_id" in merged and not merged["ap_area_id"]:
        # Stored as None (not removed) so it overrides a v1 data value.
        merged["ap_area_id"] = None
    return merged
