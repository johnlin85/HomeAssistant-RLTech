"""Stable identifiers for RLTech FTTR objects."""

from __future__ import annotations

import re

from .models import RltechAp

AP_SENSOR_KEYS = (
    "online",
    "assoc_count",
    "profile",
    "alias",
    "optical_rx_power",
    "optical_tx_power",
    "reg_off_time",
    "last_down_cause",
    "source_host",
    "cpu_usage",
    "cpu_temperature",
    "memory_usage",
    "flash_usage",
    "last_boot",
)


def object_id_slug(value: str) -> str:
    """Return a deterministic HA object-id slug."""
    text = re.sub(r"[^a-z0-9_]+", "_", value.lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def controller_sensor_object_id(host: str, key: str) -> str:
    """Return the object ID for a controller-owned sensor."""
    return f"rltech_olt_{object_id_slug(host)}_{key}"


def ap_sensor_object_id(ap: RltechAp | None, key: str) -> str | None:
    """Return the object ID for an AP sensor."""
    hardware_id = ap_hardware_id(ap)
    if hardware_id is None:
        return None
    return f"rltech_ap_{object_id_slug(hardware_id)}_{key}"


def lan_port_sensor_object_id(host: str, label: str, key: str) -> str:
    """Return the object ID for a LAN/LAN-PON link sensor."""
    return f"rltech_olt_{object_id_slug(host)}_{object_id_slug(label)}_{key}"


def ap_hardware_id(ap: RltechAp | None) -> str | None:
    """Return the AP hardware identity used for HA device/entity identity."""
    return ap.sn if ap is not None else None


def ap_sensor_unique_id(entry_id: str, ap: RltechAp | None, key: str) -> str | None:
    """Return the unique ID for an AP sensor."""
    hardware_id = ap_hardware_id(ap)
    if hardware_id is None:
        return None
    return f"{entry_id}_ap_{hardware_id}_{key}"


# Stage 6 removed the LAN / LAN-PON *link* sensors (user decision: delete,
# not disable). Their unique_ids, for the 8080/port-80 master and for extra
# port-80 hosts (``legacy_olt_<host>_``):
#   <entry>_lan_port_<n>_{status,rate,mode}  (LAN-1..4 and LANPON1/2 = port 5/6)
#   <entry>_lanpon_port_<n>_status           (entity_id ..._lanponN_status_2)
# LAN-PON optical values (<entry>_lanpon_port_<n>_{tx_power,rx_power,
# temperature,voltage,current}) are kept and never match.
_RETIRED_PORT_SUFFIX_RE = re.compile(
    r"(?:legacy_olt_[^\s]+_)?"
    r"(?:lan_port_\d+_(?:status|rate|mode)|lanpon_port_\d+_status)"
)
# Stage 6 dual-group decision D: the uplink PON optics summary sensor was
# dropped (TX/RX power moved to Diagnostic instead). Stage 6b: the uplink
# link and registration sensors were replaced by PON link type and PON
# online status (uplink_pon_link_type / uplink_pon_online_status, kept).
_RETIRED_EXACT_SUFFIXES = frozenset(
    {
        "uplink_pon_optics",
        "uplink_pon_link_state",
        "uplink_pon_registration_state",
    }
)


def is_retired_sensor_unique_id(entry_id: str, unique_id: str | None) -> bool:
    """Return whether a sensor unique_id belongs to a removed sensor.

    Removed LAN/LAN-PON link sensors, the uplink PON optics summary and the
    uplink PON link/registration sensors; exact match after the
    ``<entry_id>_`` prefix.
    """
    if not unique_id or not entry_id:
        return False
    prefix = f"{entry_id}_"
    if not unique_id.startswith(prefix):
        return False
    suffix = unique_id[len(prefix) :]
    return (
        suffix in _RETIRED_EXACT_SUFFIXES
        or _RETIRED_PORT_SUFFIX_RE.fullmatch(suffix) is not None
    )


# Stage 6b unified naming: uplink "pon_<key>" and "lanpon<n>_<key>", the
# LAN-PON bias current as "bias_current". Old auto-generated entity_ids are
# renamed once (entity_cleanup.py) when the user has not changed them; the
# unique_ids never change.
_UPLINK_RENAMED_KEYS = ("tx_power", "rx_power", "temperature", "voltage", "bias_current")
_LANPON_CURRENT_RE = re.compile(r"(?:legacy_olt_(\S+)_)?lanpon_port_(\d+)_current")
_MASTER_LANPON_RE = re.compile(
    r"lanpon_port_(\d+)_(?:tx_power|rx_power|temperature|voltage|current|link)"
)


def uplink_pon_object_id(host: str, key: str) -> str:
    """Return the object ID of an uplink PON sensor (stage 6b naming)."""
    return controller_sensor_object_id(host, f"pon_{key}")


def lanpon_object_id(host: str, ponid: int, key: str) -> str:
    """Return the object ID of a LAN-PON port sensor (``lanpon<n>_<key>``)."""
    return lan_port_sensor_object_id(host, f"LANPON{ponid}", key)


def renamed_sensor_entity_id(
    entry_id: str, host: str, unique_id: str | None
) -> tuple[str, str] | None:
    """Return (old auto-generated entity_id, new entity_id) for a rename.

    Covers the uplink PON values (``..._uplink_pon_<key>`` ->
    ``..._pon_<key>``) and the LAN-PON bias current (``..._lanpon<n>_current``
    -> ``..._lanpon<n>_bias_current``, master and extra port-80 hosts).
    ``host`` is the frozen object_id host of the entry. None otherwise.
    """
    if not unique_id or not entry_id:
        return None
    prefix = f"{entry_id}_"
    if not unique_id.startswith(prefix):
        return None
    suffix = unique_id[len(prefix) :]
    for key in _UPLINK_RENAMED_KEYS:
        if suffix == f"uplink_pon_{key}":
            return (
                f"sensor.{controller_sensor_object_id(host, suffix)}",
                f"sensor.{uplink_pon_object_id(host, key)}",
            )
    match = _LANPON_CURRENT_RE.fullmatch(suffix)
    if match is not None:
        source_host = match.group(1) or host
        ponid = int(match.group(2))
        return (
            f"sensor.{lanpon_object_id(source_host, ponid, 'current')}",
            f"sensor.{lanpon_object_id(source_host, ponid, 'bias_current')}",
        )
    return None


def master_lanpon_port(entry_id: str, unique_id: str | None) -> int | None:
    """Return the port of a master LAN-PON sensor unique_id, else None.

    Only ``<entry>_lanpon_port_<n>_<value>`` of the 8080 master (not the
    extra port-80 hosts, not the retired ``_status``).
    """
    if not unique_id or not entry_id:
        return None
    prefix = f"{entry_id}_"
    if not unique_id.startswith(prefix):
        return None
    match = _MASTER_LANPON_RE.fullmatch(unique_id[len(prefix) :])
    return int(match.group(1)) if match is not None else None
