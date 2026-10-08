"""Diagnostics for RLTech FTTR."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .settings import entry_value

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

DOMAIN = "rltech_fttr"
CONF_STATION_STALE_AFTER = "station_stale_after"

REDACTED = "***REDACTED***"
REDACT_KEYS = {
    "password",
    "username",
    "legacy_username",
    "legacy_password",
    "ap_username",
    "ap_password",
    "mqtt_username",
    "mqtt_password",
    "mqtt_psk_identity",
    "mqtt_psk",
    "mqtt_host",
    "legacy_hosts",
    "base_url",
    "ecnttoken",
    "token",
    "cookie",
    "cookies",
    "headers",
    "raw_html",
    "html",
    "mac",
    "ip",
    "hostname",
    "ssid",
    "bssid_24",
    "bssid_5",
    "sn",
    "dev_sn",
    "serial_number",
    "device_identifier",
    "object_id_host",
    "unique_id",
    "lan_ip",
    "lan_mac",
}

# Bookkeeping-only fields: a detail with nothing else is a failed attempt.
_DETAIL_STATUS_FIELDS = {
    "mac",
    "last_update",
    "detail_source",
    "detail_error",
    "web_detail_update",
}


def _detail_has_values(detail: Any) -> bool:
    """Return whether an AP detail carries any real value (not just an error)."""
    return any(
        value is not None
        for key, value in vars(detail).items()
        if key not in _DETAIL_STATUS_FIELDS
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).lower() in REDACT_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


# Effective (options > data) settings shown in diagnostics; secrets and
# addresses are redacted by key below.
_ENTRY_SETTINGS = (
    "scan_interval",
    "web_busy_grace_polls",
    "stale_device_days",
    "ac_utc_offset",
    "station_retention",
    "station_stale_after",
    "enable_ap_polling",
    "enable_station_polling",
    "enable_hardware_status",
    "enable_mqtt",
    "mqtt_host",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
    "mqtt_psk_identity",
    "mqtt_psk",
    "mqtt_use_factory_defaults",
    "legacy_username",
    "legacy_password",
    "legacy_hosts",
)


def _entry_summary(entry: Any, coordinator: Any) -> dict[str, Any]:
    """Return the entry layout and effective settings (plan 5.7)."""
    from .migration import unique_id_kind  # noqa: PLC0415

    data = getattr(coordinator, "data", None) if coordinator is not None else None
    status = getattr(data, "olt_status", None)
    lan_ip = getattr(status, "lan_ip", None)
    mqtt_in_use = getattr(coordinator, "mqtt_host_in_use", None)
    probe = getattr(coordinator, "endpoint_probe", None)
    host = None
    base_url = (getattr(entry, "data", None) or {}).get("base_url")
    if base_url:
        from .settings import base_url_to_host  # noqa: PLC0415

        host = base_url_to_host(base_url)

    def role(target: str) -> str:
        address, _, port = target.rpartition(":")
        name = (
            "host" if address == host else "lan_ip" if address == lan_ip else "other"
        )
        return f"{name}:{port}"

    manager = getattr(coordinator, "device_manager", None)
    return {
        "version": getattr(entry, "version", None),
        "minor_version": getattr(entry, "minor_version", None),
        "unique_id_kind": unique_id_kind(getattr(entry, "unique_id", None)),
        "unique_id_decision": getattr(manager, "unique_id_decision", None),
        "data_keys": sorted((getattr(entry, "data", None) or {}).keys()),
        "base_url": base_url,
        "username": (getattr(entry, "data", None) or {}).get("username"),
        "password": (getattr(entry, "data", None) or {}).get("password"),
        "settings": {key: entry_value(entry, key) for key in _ENTRY_SETTINGS},
        "mqtt_host_mode": "explicit" if entry_value(entry, "mqtt_host") else "auto",
        "mqtt_host_is_lan_ip": bool(mqtt_in_use and mqtt_in_use == lan_ip),
        "endpoint_probe": {
            "checked_at": probe.checked_at.isoformat(),
            # Keys are roles (host / lan_ip), never addresses.
            "reachable": {role(t): ok for t, ok in probe.reachable.items()},
        }
        if probe is not None
        else None,
        "ac_last_boot": status.last_boot.isoformat()
        if status is not None and status.last_boot
        else None,
    }


def _devices_summary(coordinator: Any) -> dict[str, Any] | None:
    """Return AP device ageing state (counts only; SNs are identifiers)."""
    manager = getattr(coordinator, "device_manager", None)
    if manager is None:
        return None
    missing = manager.missing_since()
    return {
        "controller_device_registered": manager.controller_device_id is not None,
        "missing_ap_count": len(missing),
        "oldest_missing_since": min(missing.values()) if missing else None,
        "stale_device_days": manager.stale_days(),
    }


def _sources_summary(coordinator: Any) -> dict[str, Any] | None:
    """Return per-source state, back-off and burst timing."""
    if coordinator is None:
        return None
    client = getattr(coordinator, "client", None)
    web_state = getattr(coordinator, "web_state", None)
    web = web_state.as_dict() if web_state is not None else None
    busy_since = (
        web_state.busy_since.isoformat()
        if web_state is not None and web_state.busy_since is not None
        else None
    )
    paused_until = getattr(coordinator, "web_polling_paused_until", None)
    legacy_states = getattr(client, "legacy_states", {}) or {}
    legacy_breakers = getattr(client, "legacy_breakers", {}) or {}
    detail_stats = getattr(client, "ap_detail_stats", {}) or {}
    return {
        "web": web,
        "busy_since": busy_since,
        "web_issue": getattr(coordinator, "web_issue", None),
        "web_polling_paused_until": paused_until.isoformat() if paused_until else None,
        "web_burst_ms": getattr(client, "last_burst_ms", None),
        "detail_parse_errors": detail_stats.get("parse_errors"),
        "devices": _devices_summary(coordinator),
        # A list, not a host-keyed dict: host names/IPs must not leak.
        "legacy": [
            {
                **state.as_dict(),
                "breaker": legacy_breakers[host].as_dict()
                if host in legacy_breakers
                else None,
            }
            for host, state in legacy_states.items()
        ],
    }


def _reboot_summary(coordinator: Any) -> dict[str, Any] | None:
    """Return the last AP reboot request through the AC (no identifiers)."""
    client = getattr(coordinator, "client", None)
    record = getattr(client, "last_reboot", None)
    if record is None:
        return None
    data = getattr(coordinator, "data", None)
    tasks = getattr(data, "ap_tasks", None) or {}
    task = tasks.get(record.mac)
    return {
        "requested_at": record.requested_at.isoformat(),
        "result": record.result,
        "error": record.error,
        "reboot_status_at_request": record.reboot_status,
        # From the latest AP list (task rows stay after completion).
        "reboot_status": task.reboot_status
        if task is not None and task.action == "2"
        else None,
        "reboot_state": task.reboot_state if task is not None else None,
        "aps_with_requests": len(getattr(client, "reboot_records", {}) or {}),
    }


def _ac_reboot_summary(coordinator: Any) -> dict[str, Any] | None:
    """Return the last AC reboot request and the expected-offline window."""
    client = getattr(coordinator, "client", None)
    record = getattr(client, "last_ac_reboot", None)
    until = getattr(coordinator, "expected_offline_until", None)
    if record is None and until is None:
        return None
    return {
        "requested_at": record.requested_at.isoformat() if record else None,
        "result": record.result if record else None,
        "error": record.error if record else None,
        # mag-reset.asp answer: status and first 300 bytes, identifiers
        # stripped in api.sanitize_response_head (review S1).
        "http_status": record.http_status if record else None,
        "response_head": record.response_head if record else None,
        "expected_offline_until": until.isoformat() if until else None,
        # True: the AC went away (or its boot time moved) after the request;
        # False: neither was seen in the window; None: not decided yet.
        "reboot_observed": getattr(coordinator, "ac_reboot_observed", None),
    }


async def async_get_config_entry_diagnostics(
    hass: "HomeAssistant", entry: "ConfigEntry"
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = getattr(entry, "runtime_data", None)
    data = coordinator.data if coordinator is not None else None
    mqtt_summary = (
        coordinator.mqtt_stats.as_dict()
        if coordinator is not None and hasattr(coordinator, "mqtt_stats")
        else None
    )
    summary = None
    dhcp_summary = None
    sources_summary = _sources_summary(coordinator)
    if data is not None:
        try:
            from .hostname_enrichment import dhcp_match_summary

            dhcp_summary = dhcp_match_summary(hass, data.stations.values())
        except Exception as err:  # pragma: no cover - defensive around HA internals
            dhcp_summary = {"error": str(err)}
        summary = {
            "ap_count": len(data.aps),
            # Failed-attempt placeholders are not counted as details.
            "ap_detail_count": sum(
                1
                for mac, detail in data.ap_details.items()
                if mac in data.aps and _detail_has_values(detail)
            ),
            "ap_missing_detail_count": sum(
                1
                for mac in data.aps
                if mac not in data.ap_details
                or not _detail_has_values(data.ap_details[mac])
            ),
            "ap_detail_stats": dict(
                getattr(getattr(coordinator, "client", None), "ap_detail_stats", {})
                or {}
            ),
            "ap_detail_error_count": sum(
                1 for detail in data.ap_details.values() if detail.detail_error
            ),
            "ap_detail_missing_optical_count": sum(
                1
                for mac, ap in data.aps.items()
                if ap.online is True
                and (
                    (
                        data.ap_details.get(mac) is None
                        or data.ap_details[mac].optical_rx_power is None
                    )
                    and ap.optical_rx_power is None
                    or (
                        data.ap_details.get(mac) is None
                        or data.ap_details[mac].optical_tx_power is None
                    )
                    and ap.optical_tx_power is None
                )
            ),
            "online_ap_count": sum(1 for ap in data.aps.values() if ap.online),
            "station_count": len(data.stations),
            "station_stale_after": entry_value(entry, CONF_STATION_STALE_AFTER),
            "station_active_count": sum(
                1 for station in data.stations.values() if station.reported_online
            ),
            "station_inactive_count": sum(
                1 for station in data.stations.values() if not station.reported_online
            ),
            "station_oldest_last_seen": (
                min(
                    station.last_seen
                    for station in data.stations.values()
                    if station.last_seen is not None
                ).isoformat()
                if any(
                    station.last_seen is not None
                    for station in data.stations.values()
                )
                else None
            ),
            "station_http_polling_active": getattr(
                coordinator, "station_http_polling_active", None
            ),
            "station_http_polling_reason": getattr(
                coordinator, "station_http_polling_reason", None
            ),
            "lanpon_port_count": len(data.lanpon_ports),
            "legacy_source_count": len(data.legacy_sources),
            # Keyed by position: host names/IPs must not leak (review S7).
            "legacy_sources": {
                f"source_{index}": {
                    "olt_status_present": source.olt_status is not None,
                    "lanpon_port_count": len(source.lanpon_ports),
                    "last_success": source.last_success.isoformat()
                    if source.last_success
                    else None,
                }
                for index, source in enumerate(data.legacy_sources.values())
            },
            "reported_station_count": sum(
                1 for station in data.stations.values() if station.reported_online
            ),
            "last_success": data.last_success.isoformat()
            if data.last_success
            else None,
            "last_success_8080": data.last_success_8080.isoformat()
            if data.last_success_8080
            else None,
            "last_success_80": data.last_success_80.isoformat()
            if data.last_success_80
            else None,
            "poll_duration_ms": data.poll_duration_ms,
            "olt_status_present": data.olt_status is not None,
            "web_status_update": data.web_status_update.isoformat()
            if data.web_status_update
            else None,
            # Last good sta-device/sta-user read; readings are dropped once
            # it is older than 2 status intervals (stage 7).
            "web_status_parsed": data.web_status_parsed.isoformat()
            if getattr(data, "web_status_parsed", None)
            else None,
            "web_lanpon_port_count": len(data.web_lanpon_ports),
            "uplink_pon_update": data.uplink_pon_update.isoformat()
            if data.uplink_pon_update
            else None,
            # Uplink PON (sta-network.asp): states and which values are known.
            "uplink_pon": {
                "online_status": data.uplink_pon.online_status,
                "pon_state": data.uplink_pon.pon_state,
                "link_type": data.uplink_pon.link_type,
                "page_link_type": data.uplink_pon.page_link_type,
                "link_state": data.uplink_pon.link_state,
                "registration_state": data.uplink_pon.registration_state,
                "optics_state": data.uplink_pon.optics_state,
                "link_sta": data.uplink_pon.link_sta,
                "traffic_state": data.uplink_pon.traffic_state,
                "phy_status": data.uplink_pon.phy_status,
                "known_values": sorted(
                    key
                    for key in (
                        "tx_power",
                        "rx_power",
                        "temperature",
                        "voltage",
                        "bias_current",
                    )
                    if getattr(data.uplink_pon, key) is not None
                ),
            }
            if data.uplink_pon is not None
            else None,
            "ac_identity": {
                "serial_number": data.olt_status.serial_number
                if data.olt_status
                else None,
                "device_identifier": data.olt_status.device_identifier
                if data.olt_status
                else None,
                "lan_mac": data.olt_status.lan_mac if data.olt_status else None,
                "lan_ip": data.olt_status.lan_ip if data.olt_status else None,
                "serial_number_present": bool(
                    data.olt_status and data.olt_status.serial_number
                ),
                "lan_mac_present": bool(data.olt_status and data.olt_status.lan_mac),
            },
            "olt_status_fields": sorted(
                key
                for key, value in vars(data.olt_status).items()
                if value is not None
            )
            if data.olt_status is not None
            else [],
            "ap_detail_fields": sorted(
                {
                    key
                    for detail in data.ap_details.values()
                    for key, value in vars(detail).items()
                    if value is not None
                    and key
                    not in {
                        "mac",
                        "ip",
                        "hostname",
                        "sn",
                        "dev_sn",
                        "pon_sn",
                        "sn_address",
                    }
                }
            ),
        }
    return _redact(
        {
            "entry": _entry_summary(entry, coordinator),
            "summary": summary,
            "sources": sources_summary,
            "ap_reboot": _reboot_summary(coordinator),
            "ac_reboot": _ac_reboot_summary(coordinator),
            "mqtt": mqtt_summary,
            "dhcp_hostname_enrichment": dhcp_summary,
        }
    )
