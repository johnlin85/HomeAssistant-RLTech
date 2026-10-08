"""Read RLTech FTTR config entry settings (pure; no Home Assistant imports).

From entry version 1.2 on, non-connection settings live in ``entry.options``;
``entry.data`` keeps the v1 copies for rollback (plan stage 4, 4.1). Every
reader goes through :func:`entry_value`, which prefers options, then data.
Entries are only accessed through ``.data`` / ``.options`` so tests can pass
a SimpleNamespace.
"""

from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
import re
from typing import Any
from urllib.parse import urlsplit

# Keys (same strings as const.py; duplicated so this module stays HA-free).
KEY_BASE_URL = "base_url"
KEY_OBJECT_ID_HOST = "object_id_host"
KEY_ENABLE_HARDWARE_STATUS = "enable_hardware_status"
KEY_LEGACY_ENABLE_OLT_STATUS = "enable_olt_status"

AC_UTC_OFFSET_HA = "ha"
_OFFSET_RE = re.compile(r"([+-])(\d{2}):(\d{2})")

_MISSING = object()


def entry_value(entry: Any, key: str, default: Any = None) -> Any:
    """Return options[key], else data[key], else default.

    Presence decides, not truthiness: an option set to False or 0 wins over
    data.
    """
    options = getattr(entry, "options", None) or {}
    value = options.get(key, _MISSING)
    if value is not _MISSING:
        return value
    data = getattr(entry, "data", None) or {}
    value = data.get(key, _MISSING)
    if value is not _MISSING:
        return value
    return default


def base_url_to_host(value: str) -> str:
    """Return the host part of a stored base URL (``http://host:8080``)."""
    text = str(value).strip().rstrip("/")
    parsed = urlsplit(text if "://" in text else f"http://{text}")
    return parsed.hostname or parsed.netloc.split(":", 1)[0] or text


def entry_host(entry: Any) -> str:
    """Return the configured AC host (changes with reconfigure)."""
    return base_url_to_host(entry.data[KEY_BASE_URL])


def entry_base_url(entry: Any) -> str:
    """Return the configured 8080 base URL without a trailing slash."""
    return str(entry.data[KEY_BASE_URL]).rstrip("/")


def object_id_host(entry: Any) -> str:
    """Return the host frozen into entity_ids and the controller device name.

    Written at creation / migration and never changed by reconfigure, so a
    new address does not rename anything (plan 4.4, Q9).
    """
    frozen = (getattr(entry, "data", None) or {}).get(KEY_OBJECT_ID_HOST)
    return str(frozen) if frozen else entry_host(entry)


def hardware_status_enabled(entry: Any, default: bool = True) -> bool:
    """Return whether the port-80 enhancement is on (with the old v0 key)."""
    options = getattr(entry, "options", None) or {}
    if KEY_ENABLE_HARDWARE_STATUS in options:
        return bool(options[KEY_ENABLE_HARDWARE_STATUS])
    data = getattr(entry, "data", None) or {}
    if KEY_ENABLE_HARDWARE_STATUS in data:
        return bool(data[KEY_ENABLE_HARDWARE_STATUS])
    return bool(data.get(KEY_LEGACY_ENABLE_OLT_STATUS, default))


def ac_timezone(value: Any, ha_tz: tzinfo) -> tzinfo:
    """Return the AC time zone for page timestamps.

    ``"ha"`` (or anything invalid) follows Home Assistant's zone; ``"+07:00"``
    style offsets give a fixed zone. The AC exposes no zone setting, so this
    is a user choice (plan 5.5, Q8).
    """
    text = str(value or "").strip()
    match = _OFFSET_RE.fullmatch(text)
    if match is None:
        return ha_tz
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    if delta > timedelta(hours=14) or int(minutes) >= 60:
        return ha_tz
    return timezone(-delta if sign == "-" else delta)


def utc_offset_choices() -> list[str]:
    """Return the selectable AC UTC offsets (30-minute steps plus 3 others)."""
    values = set()
    for half_hours in range(-24, 29):
        total = half_hours * 30
        values.add(total)
    values.update({5 * 60 + 45, 8 * 60 + 45, 12 * 60 + 45})
    result = []
    for total in sorted(values):
        sign = "-" if total < 0 else "+"
        hours, minutes = divmod(abs(total), 60)
        result.append(f"{sign}{hours:02d}:{minutes:02d}")
    return [AC_UTC_OFFSET_HA, *result]
