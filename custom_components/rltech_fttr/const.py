"""Constants for RLTech FTTR."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "rltech_fttr"
PLATFORMS = [Platform.SENSOR, Platform.BUTTON, Platform.SWITCH]
SIGNAL_STATIONS_CHANGED = f"{DOMAIN}_stations_changed"

STATION_CARD_URL = "/rltech_fttr/rltech-fttr-station-table-card.js"
STATION_CARD_FILENAME = "rltech-fttr-station-table-card.js"
AP_CARD_URL = "/rltech_fttr/rltech-fttr-ap-table-card.js"
AP_CARD_FILENAME = "rltech-fttr-ap-table-card.js"
PANEL_URL = "/rltech_fttr/rltech-fttr-panel.js"
PANEL_FILENAME = "rltech-fttr-panel.js"
# Sidebar panel (plan 3.3.1); admin-only because it lists client MACs/IPs (D5).
PANEL_URL_PATH = "rltech-fttr"
PANEL_COMPONENT = "rltech-fttr-panel"
PANEL_TITLE = "RLTech FTTR"
PANEL_ICON = "mdi:access-point-network"

CONF_BASE_URL = "base_url"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_STATION_RETENTION = "station_retention"
CONF_STATION_STALE_AFTER = "station_stale_after"
CONF_ENABLE_AP_POLLING = "enable_ap_polling"
CONF_ENABLE_STATION_POLLING = "enable_station_polling"
CONF_AP_AREA_ID = "ap_area_id"
CONF_ENABLE_HARDWARE_STATUS = "enable_hardware_status"
CONF_LEGACY_USERNAME = "legacy_username"
CONF_LEGACY_PASSWORD = "legacy_password"
CONF_LEGACY_HOSTS = "legacy_hosts"
# Retired in stage 6 (reboots go through the AC): no longer read or shown;
# stored values are kept in the entry for rollback.
CONF_AP_USERNAME = "ap_username"
CONF_AP_PASSWORD = "ap_password"
CONF_ENABLE_MQTT = "enable_mqtt"
CONF_MQTT_HOST = "mqtt_host"
CONF_MQTT_PORT = "mqtt_port"
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"
CONF_MQTT_PSK_IDENTITY = "mqtt_psk_identity"
CONF_MQTT_PSK = "mqtt_psk"

DEFAULT_BASE_URL = "http://192.168.1.1:8080"
DEFAULT_USERNAME = "useradmin"
DEFAULT_LEGACY_USERNAME = "admin"
DEFAULT_LEGACY_PASSWORD = "admin"
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_STATION_RETENTION = 3600
DEFAULT_STATION_STALE_AFTER = 900
DEFAULT_ENABLE_AP_POLLING = True
DEFAULT_ENABLE_STATION_POLLING = True
DEFAULT_ENABLE_HARDWARE_STATUS = True
DEFAULT_ENABLE_MQTT = False
DEFAULT_MQTT_PORT = 8883
DEFAULT_MQTT_USERNAME = "admin"
DEFAULT_MQTT_PASSWORD = "123456"
DEFAULT_MQTT_PSK_IDENTITY = "admin"
# Factory PSK from the AC/AP romfile (hex of the text PSK). Only used when the
# user explicitly ticks "use factory defaults" (D4).
FACTORY_MQTT_PSK = "61646d696e21402324"
CONF_MQTT_USE_FACTORY_DEFAULTS = "mqtt_use_factory_defaults"
CONF_OBJECT_ID_HOST = "object_id_host"
CONF_AC_UTC_OFFSET = "ac_utc_offset"
DEFAULT_DHCP_HOSTNAME_REFRESH_INTERVAL = 300
DEFAULT_AP_DETAIL_INTERVAL = 300
WEB_BURST_BUDGET = 25
DEFAULT_AC_STATUS_INTERVAL = 300
# AP detail sensors become unavailable when older than 3 detail cycles.
AP_DETAIL_MAX_AGE = timedelta(seconds=3 * DEFAULT_AP_DETAIL_INTERVAL)
# Options key (no options UI until stage 4); default in sources.py.
CONF_WEB_BUSY_GRACE_POLLS = "web_busy_grace_polls"
WEB_POLLING_PAUSE = timedelta(minutes=15)
# After an AC reboot is submitted (stage 6b): failures in this window are
# expected, so no Repairs issue, no error log and no port-80 breaker trip.
AC_REBOOT_EXPECTED_OFFLINE = timedelta(seconds=300)
ENDPOINT_PROBE_INTERVAL = timedelta(minutes=30)
# D6: remove AP devices missing this many days (0 disables); options UI in
# stage 4, read from options/data for now. Default in ap_device_registry.py.
CONF_STALE_DEVICE_DAYS = "stale_device_days"

MIN_SCAN_INTERVAL = 30
MIN_STATION_STALE_AFTER = 60
DEFAULT_TIMEOUT = 10
