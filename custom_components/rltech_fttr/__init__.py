"""RLTech FTTR integration."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from urllib.parse import urlsplit

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import (
    CONF_RESOURCE_TYPE_WS,
    LOVELACE_DATA,
    MODE_STORAGE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    ENDPOINT_PROBE_INTERVAL,
    AP_CARD_FILENAME,
    AP_CARD_URL,
    DOMAIN,
    PANEL_COMPONENT,
    PANEL_FILENAME,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    PANEL_URL_PATH,
    PLATFORMS,
    STATION_CARD_FILENAME,
    STATION_CARD_URL,
)
from .ap_device_registry import device_removal_allowed
from .coordinator import RltechConfigEntry, RltechCoordinator, build_client
from .devices import RltechDeviceManager, async_remove_store
from .entity_cleanup import (
    async_fix_temperature_units,
    async_fix_uptime_unit,
    async_remove_extra_lanpon_entities,
    async_remove_retired_entities,
    async_rename_entities,
)
from .issues import async_clear_web_issues
from .migration import CURRENT_MINOR_VERSION, CURRENT_VERSION, migrate_1_1_to_1_2
from .oui_enrichment import async_load_oui
from .websocket import async_setup_websocket

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_CARD_RESOURCES = (STATION_CARD_URL, AP_CARD_URL)
_CARD_RESOURCE_FILES = {
    STATION_CARD_URL: STATION_CARD_FILENAME,
    AP_CARD_URL: AP_CARD_FILENAME,
}


def _get_lovelace_data(hass: HomeAssistant):
    """Return Lovelace data across supported Home Assistant versions."""
    return hass.data.get(LOVELACE_DATA) or hass.data.get("lovelace")


@callback
def _lovelace_resource_collection(lovelace_data):
    """Return the Lovelace resource collection from dataclass or legacy dict data."""
    if lovelace_data is None:
        return None
    if isinstance(lovelace_data, dict):
        return lovelace_data.get("resources")
    return getattr(lovelace_data, "resources", None)


@callback
def _lovelace_resource_mode(lovelace_data) -> str | None:
    """Return the Lovelace resource mode from dataclass or legacy dict data."""
    if lovelace_data is None:
        return None
    if isinstance(lovelace_data, dict):
        return lovelace_data.get("resource_mode") or lovelace_data.get("mode")
    return getattr(lovelace_data, "resource_mode", None)


def _card_resource_version(url: str) -> str:
    """Return a cache-busting version for a bundled card resource."""
    filename = _CARD_RESOURCE_FILES[url]
    path = Path(__file__).parent / "www" / filename
    return str(int(path.stat().st_mtime))


def _versioned_card_resource_url(url: str) -> str:
    """Return card resource URL with a cache-busting query parameter."""
    return f"{url}?v={_card_resource_version(url)}"


async def _ensure_lovelace_card_resources(hass: HomeAssistant) -> None:
    """Register bundled custom cards with Lovelace storage resources."""
    lovelace_data = _get_lovelace_data(hass)
    resources = _lovelace_resource_collection(lovelace_data)
    if resources is None:
        _LOGGER.debug("Lovelace resources are not available yet")
        return

    if _lovelace_resource_mode(lovelace_data) != MODE_STORAGE:
        _LOGGER.info(
            "Lovelace resources are not in storage mode; add RLTech FTTR card "
            "resources manually in Lovelace YAML configuration"
        )
        return

    try:
        await resources.async_get_info()
    except Exception:
        _LOGGER.exception("Unable to load Lovelace resources")
        return

    existing_by_base_url = {
        str(item.get(CONF_URL, "")).split("?", 1)[0]: item
        for item in resources.async_items()
    }
    for url in _CARD_RESOURCES:
        resource_url = _versioned_card_resource_url(url)
        if existing := existing_by_base_url.get(url):
            if existing.get(CONF_URL) != resource_url:
                await resources.async_update_item(
                    existing["id"],
                    {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: resource_url},
                )
                _LOGGER.info(
                    "Updated RLTech FTTR Lovelace resource: %s", resource_url
                )
            continue
        await resources.async_create_item(
            {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: resource_url}
        )
        _LOGGER.info("Registered RLTech FTTR Lovelace resource: %s", resource_url)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the static files and websocket commands once per HA run."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(url, str(Path(__file__).parent / "www" / filename), True)
            for url, filename in _CARD_RESOURCE_FILES.items()
        ]
        + [
            StaticPathConfig(
                PANEL_URL, str(Path(__file__).parent / "www" / PANEL_FILENAME), True
            )
        ]
    )
    async_setup_websocket(hass)
    await _ensure_lovelace_card_resources(hass)
    return True


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Add the sidebar panel (admins only, D5) if it is not there yet."""
    if frontend.async_panel_exists(hass, PANEL_URL_PATH):
        return
    version = int((Path(__file__).parent / "www" / PANEL_FILENAME).stat().st_mtime)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{PANEL_URL}?v={version}",
        require_admin=True,
        # The panel imports the same versioned card modules as Lovelace.
        config={
            "card_urls": [_versioned_card_resource_url(url) for url in _CARD_RESOURCES]
        },
    )


async def async_setup_entry(hass: HomeAssistant, entry: RltechConfigEntry) -> bool:
    """Set up RLTech FTTR from a config entry."""
    client = build_client(entry)
    coordinator = RltechCoordinator(hass, entry, client)
    # Devices are registered explicitly (controller first, then APs and extra
    # OLTs after every update) before any entity refers to them.
    device_manager = RltechDeviceManager(hass, entry, coordinator)
    coordinator.device_manager = device_manager
    await device_manager.async_setup()
    # Before the first refresh, so a pause set before a restart is honoured
    # without logging in to the AC (plan 5.2).
    coordinator.async_restore_pause(device_manager.paused_until)
    await async_load_oui()
    await coordinator.async_config_entry_first_refresh()
    device_manager.async_sync()
    # Registered before the platforms so devices exist when entities are added.
    entry.async_on_unload(coordinator.async_add_listener(device_manager.async_sync))
    await coordinator.async_start_station_aging()

    entry.runtime_data = coordinator
    # Before the platforms: entities removed in stage 6/6b leave the
    # registry, old generated entity_ids move to the stage 6b names, and
    # LAN-PON ports the AC does not report are dropped.
    async_remove_retired_entities(hass, entry)
    async_rename_entities(hass, entry)
    async_fix_temperature_units(hass, entry)
    async_fix_uptime_unit(hass, entry)
    remove_extra_lanpon = _extra_lanpon_remover(hass, entry, coordinator)
    remove_extra_lanpon()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Again whenever a new sta-user.asp port list arrives.
    entry.async_on_unload(coordinator.async_add_listener(remove_extra_lanpon))
    await _async_register_panel(hass)
    await coordinator.async_start_mqtt()
    # MQTT and the port-80 master host are chosen by TCP probing, now and
    # every 30 minutes, in the background (plan 5.1).
    coordinator.async_schedule_endpoint_resolution()
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            coordinator.async_schedule_endpoint_resolution,
            ENDPOINT_PROBE_INTERVAL,
        )
    )

    # Log out on Home Assistant stop too, so a restart never leaves the
    # AC's single shared Web UI login held by this integration.
    remove_stop_listener: CALLBACK_TYPE | None = None

    async def _async_logout_on_stop(_event: Event) -> None:
        nonlocal remove_stop_listener
        remove_stop_listener = None
        with contextlib.suppress(Exception):
            await coordinator.async_close_web_session()

    remove_stop_listener = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, _async_logout_on_stop
    )

    @callback
    def _remove_stop_listener() -> None:
        if remove_stop_listener is not None:
            remove_stop_listener()

    entry.async_on_unload(_remove_stop_listener)
    return True


def _extra_lanpon_remover(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: RltechCoordinator
) -> CALLBACK_TYPE:
    """Return a listener removing LAN-PON ports the AC does not report."""
    checked: list[object] = [None]

    @callback
    def remove() -> None:
        data = coordinator.data
        ports = data.web_lanpon_ports if data is not None else None
        if not ports or ports is checked[0]:
            return
        checked[0] = ports
        async_remove_extra_lanpon_entities(hass, entry, ports)

    return remove


async def async_unload_entry(hass: HomeAssistant, entry: RltechConfigEntry) -> bool:
    """Unload an RLTech FTTR config entry."""
    coordinator = entry.runtime_data
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    with contextlib.suppress(Exception):
        await coordinator.async_stop_mqtt()
    with contextlib.suppress(Exception):
        await coordinator.async_stop_station_aging()
    coordinator.async_cancel_timers()
    with contextlib.suppress(Exception):
        await coordinator.device_manager.async_flush()
    with contextlib.suppress(Exception):
        # Waits for an in-flight burst to release the 8080 lock (review N1).
        await coordinator.async_close_web_session()
    async_clear_web_issues(hass, entry)
    # This entry is "unload in progress" now, so only other entries count;
    # Home Assistant drops entry.runtime_data itself after a clean unload.
    if unload_ok and not hass.config_entries.async_loaded_entries(DOMAIN):
        # Last entry gone: remove the sidebar panel (plan 3.3.1).
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: RltechConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removing an AP or extra OLT device the AC no longer reports (D6)."""
    coordinator: RltechCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is None or coordinator.data is None:
        return False
    return device_removal_allowed(
        entry.entry_id,
        device_entry.identifiers,
        listed_sns=coordinator.data.aps_by_sn,
        legacy_hosts=[
            urlsplit(url).hostname or url for url in coordinator.client.legacy_base_urls
        ],
    )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the per-entry device bookkeeping store."""
    with contextlib.suppress(Exception):
        await async_remove_store(hass, entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry; no network I/O (plan 4.6).

    1.1 -> 1.2 copies non-connection settings into options and keeps every v1
    key in data, so the previous release can still load the entry.
    """
    if entry.version > CURRENT_VERSION:
        _LOGGER.error(
            "Cannot downgrade RLTech entry from version %s.%s",
            entry.version,
            entry.minor_version,
        )
        return False
    if entry.version == 1 and entry.minor_version < CURRENT_MINOR_VERSION:
        try:
            data, options = migrate_1_1_to_1_2(entry.data, entry.options)
        except Exception:  # noqa: BLE001 - entry stays at 1.1, untouched
            _LOGGER.exception("Unable to migrate RLTech entry %s", entry.title)
            return False
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
            version=CURRENT_VERSION,
            minor_version=CURRENT_MINOR_VERSION,
        )
        _LOGGER.info("Migrated RLTech entry %s to version 1.2", entry.title)
    return True
