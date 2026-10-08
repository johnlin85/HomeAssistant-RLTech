"""Websocket API for RLTech FTTR."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SIGNAL_STATIONS_CHANGED
from .ap_inventory import ap_rows
from .identifiers import AP_SENSOR_KEYS, ap_sensor_unique_id
from .runtime import loaded_coordinator
from .station_inventory import station_rows
from .table_query import (
    ap_filter_options,
    station_query,
    table_result,
    uplink_label,
)

def async_setup_websocket(hass: HomeAssistant) -> None:
    """Register RLTech FTTR websocket commands."""
    websocket_api.async_register_command(hass, websocket_get_entries)
    websocket_api.async_register_command(hass, websocket_get_stations)
    websocket_api.async_register_command(hass, websocket_subscribe_station_changes)
    websocket_api.async_register_command(hass, websocket_get_access_points)


# Open to every user: only entry ids, titles and data source states, which
# the AP card needs to find its entry.
@websocket_api.websocket_command(
    {
        vol.Required("type"): "rltech_fttr/get_entries",
    }
)
@websocket_api.async_response
async def websocket_get_entries(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return loaded RLTech FTTR config entries for card auto-configuration."""
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = loaded_coordinator(entry)
        if coordinator is None:
            continue
        entries.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "sources": sources_payload(coordinator),
            }
        )

    connection.send_result(msg["id"], {"entries": entries})


# Client MACs, IPs and host names: administrators only (user decision,
# stage 7), like the sidebar panel.
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "rltech_fttr/get_stations",
        vol.Required("entry_id"): str,
        vol.Optional("page", default=0): vol.Coerce(int),
        vol.Optional("page_size", default=0): vol.Coerce(int),
        vol.Optional("search", default=""): str,
        vol.Optional("sort_key", default="mac"): str,
        vol.Optional("sort_dir", default=1): vol.In([1, -1]),
        vol.Optional("filters", default={}): dict,
    }
)
@websocket_api.async_response
async def websocket_get_stations(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return latest station inventory rows for one config entry."""
    entry_id = msg["entry_id"]
    coordinator = _entry_coordinator(hass, entry_id)
    if coordinator is None:
        connection.send_error(
            msg["id"], "not_found", "Unknown RLTech FTTR config entry"
        )
        return

    result = station_query(
        station_rows(coordinator.data),
        ap_filter_options(coordinator.data),
        search=msg["search"],
        filters=msg["filters"],
        sort_key=msg["sort_key"],
        sort_dir=msg["sort_dir"],
        page=msg["page"],
        page_size=msg["page_size"],
    )
    connection.send_result(
        msg["id"],
        {
            "entry_id": entry_id,
            "sources": sources_payload(coordinator),
            "stations": result["rows"],
            **{key: value for key, value in result.items() if key != "rows"},
        },
    )


# Client MACs, IPs and host names: administrators only (user decision,
# stage 7), like the sidebar panel.
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "rltech_fttr/subscribe_station_changes",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_subscribe_station_changes(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Subscribe to tiny station-inventory changed events for one config entry."""
    entry_id = msg["entry_id"]
    coordinator = _entry_coordinator(hass, entry_id)
    if coordinator is None:
        connection.send_error(
            msg["id"], "not_found", "Unknown RLTech FTTR config entry"
        )
        return

    @callback
    def forward_station_change(changed_at) -> None:
        connection.send_event(
            msg["id"],
            {
                "entry_id": entry_id,
                "changed_at": changed_at.isoformat() if changed_at else None,
            },
        )

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass,
        f"{SIGNAL_STATIONS_CHANGED}_{entry_id}",
        forward_station_change,
    )
    connection.send_result(msg["id"])


# Open to every user: AP inventory only (AP rows carry no client data).
@websocket_api.websocket_command(
    {
        vol.Required("type"): "rltech_fttr/get_access_points",
        vol.Required("entry_id"): str,
        vol.Optional("page", default=0): vol.Coerce(int),
        vol.Optional("page_size", default=0): vol.Coerce(int),
        vol.Optional("search", default=""): str,
        vol.Optional("sort_key", default="online"): str,
        vol.Optional("sort_dir", default=1): vol.In([1, -1]),
        vol.Optional("filters", default={}): dict,
    }
)
@websocket_api.async_response
async def websocket_get_access_points(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return latest AP inventory rows for one config entry."""
    entry_id = msg["entry_id"]
    coordinator = _entry_coordinator(hass, entry_id)
    if coordinator is None:
        connection.send_error(
            msg["id"], "not_found", "Unknown RLTech FTTR config entry"
        )
        return

    rows = ap_rows(
        coordinator.data,
        _ap_registry_info(hass, entry_id, coordinator.data),
    )
    result = table_result(
        rows,
        search=msg["search"],
        filters=msg["filters"],
        sort_key=msg["sort_key"],
        sort_dir=msg["sort_dir"],
        page=msg["page"],
        page_size=msg["page_size"],
        filter_specs={
            "profile": ("Profile", lambda row: row.get("profile")),
            "model": ("Model", lambda row: row.get("model")),
            "uplink": ("Uplink", uplink_label),
        },
        filter_predicates={
            "state": lambda row, value: (
                "online" if row.get("online") else "offline"
            )
            == value,
            "profile": lambda row, value: row.get("profile") == value,
            "model": lambda row, value: row.get("model") == value,
            "uplink": lambda row, value: uplink_label(row) == value,
        },
        sort_values={
            "online": lambda row: 1 if row.get("online") else 0,
            "uplink_label": uplink_label,
        },
    )
    connection.send_result(
        msg["id"],
        {
            "entry_id": entry_id,
            "sources": sources_payload(coordinator),
            "access_points": result["rows"],
            **{key: value for key, value in result.items() if key != "rows"},
        },
    )


def _entry_coordinator(hass: HomeAssistant, entry_id: str):
    """Return the coordinator of one loaded RLTech entry, else None."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    return loaded_coordinator(entry)


def _ap_registry_info(
    hass: HomeAssistant,
    entry_id: str,
    data,
) -> dict[str, dict[str, object]]:
    """Return current HA device/entity registry info, keyed by AP MAC."""
    if data is None:
        return {}

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    ours = [
        entry
        for entry in er.async_entries_for_config_entry(entity_registry, entry_id)
        if entry.domain == "sensor" and entry.platform == DOMAIN and entry.unique_id
    ]
    # Disabled entities (for example memory/flash usage, disabled by default
    # since stage 7) have no state: the AP table must not link to them.
    entity_lookup = {entry.unique_id: entry.entity_id for entry in ours if not entry.disabled}
    disabled_ids = {entry.unique_id for entry in ours if entry.disabled}
    device_lookup: dict[str, str] = {}
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                device_lookup[identifier] = device.id

    result: dict[str, dict[str, object]] = {}
    for mac, ap in data.aps.items():
        entities: dict[str, str] = {}
        disabled: list[str] = []
        device_identifier = f"{entry_id}_ap_{ap.sn}" if ap.sn else None
        for key in AP_SENSOR_KEYS:
            unique_id = ap_sensor_unique_id(entry_id, ap, key)
            if unique_id is None:
                continue
            entity_id = entity_lookup.get(unique_id)
            if entity_id:
                entities[key] = entity_id
            elif unique_id in disabled_ids:
                disabled.append(key)
        result[mac] = {
            "device_id": device_lookup.get(device_identifier) if device_identifier else None,
            "entities": entities,
            "disabled_entities": disabled,
        }
    return result


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def sources_payload(coordinator) -> dict[str, object]:
    """Return data source state for the panel and cards (plan 3.3.1).

    ``web.state`` is ok / busy / unreachable / error / auth_failed / locked /
    paused / unknown; ``web.last_success`` tells the UI when the shown 8080
    data was last refreshed ("data frozen at HH:MM" while busy).
    """
    data = coordinator.data
    web_state = coordinator.web_state
    paused_until = (
        coordinator.web_polling_paused_until
        if coordinator.web_polling_paused()
        else None
    )
    mqtt = coordinator.mqtt_stats
    return {
        "web": {
            "state": "paused" if paused_until is not None else web_state.state,
            "last_success": _iso(data.last_success_8080 if data else None),
            "busy_since": _iso(web_state.busy_since),
            "failing_since": _iso(web_state.failing_since),
            "paused_until": _iso(paused_until),
            "available": bool(coordinator.last_update_success)
            and paused_until is None,
        },
        "mqtt": {
            "enabled": bool(mqtt.enabled),
            "connected": bool(mqtt.connected),
            "last_message": _iso(mqtt.last_message),
            "error_kind": mqtt.last_error_kind,
        },
        "stations_source": (
            "http" if coordinator.station_http_polling_active else "mqtt"
        ),
        "stations_reason": coordinator.station_http_polling_reason,
    }
