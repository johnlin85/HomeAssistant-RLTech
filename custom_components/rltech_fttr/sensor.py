"""Sensors for RLTech FTTR."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from urllib.parse import urlsplit

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    AP_DETAIL_MAX_AGE,
    DEFAULT_ENABLE_HARDWARE_STATUS,
)
from .api import PON_LINK_TYPES, PON_ONLINE_STATES
from .coordinator import RltechConfigEntry, RltechCoordinator
from .entity import (
    RltechEntity,
    ap_device_info,
    controller_device_info,
    legacy_olt_device_info,
)
from .identifiers import (
    ap_sensor_object_id,
    ap_sensor_unique_id,
    controller_sensor_object_id,
    lanpon_object_id,
    uplink_pon_object_id,
)
from .settings import object_id_host
from .settings import hardware_status_enabled as settings_hardware_status_enabled
from .sources import (
    SOURCE_LEGACY,
    ap_detail_entity_available,
    ap_entity_available,
    source_available,
)
from .models import (
    RltechAp,
    RltechApDetail,
    RltechData,
    RltechLanPonPort,
    RltechOltStatus,
    RltechUplinkPon,
)

_LOGGER = logging.getLogger(__name__)

# Coordinator-driven: sensors never poll on their own.
PARALLEL_UPDATES = 0


def _entry_host(entry: ConfigEntry) -> str:
    """Return the host frozen into controller entity_ids (object_id_host)."""
    return object_id_host(entry)


def _set_entity_object_id(entity: SensorEntity, object_id: str) -> None:
    """Set a stable initial HA entity ID and matching suggested object ID."""
    entity._attr_suggested_object_id = object_id
    entity.entity_id = f"sensor.{object_id}"


@dataclass(frozen=True, kw_only=True)
class RltechSensorDescription(SensorEntityDescription):
    """Description for a controller sensor."""

    value_fn: Callable[[RltechData], int | float | str | datetime | None]


CONTROLLER_SENSORS = (
    RltechSensorDescription(
        key="ap_count",
        translation_key="ap_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.aps),
    ),
    RltechSensorDescription(
        key="online_ap_count",
        translation_key="online_ap_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(1 for ap in data.aps.values() if ap.online),
    ),
    RltechSensorDescription(
        key="reported_station_count",
        translation_key="reported_station_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(1 for sta in data.stations.values() if sta.reported_online),
    ),
    RltechSensorDescription(
        key="poll_duration",
        translation_key="poll_duration",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.poll_duration_ms,
    ),
)


@dataclass(frozen=True, kw_only=True)
class OltSensorDescription(SensorEntityDescription):
    """Description for an optional OLT status sensor."""

    value_fn: Callable[[RltechOltStatus], int | float | str | bool | datetime | None]


OLT_SENSORS = (
    OltSensorDescription(
        key="last_boot",
        translation_key="last_boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.last_boot,
    ),
    OltSensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.cpu_usage,
    ),
    OltSensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.memory_usage,
    ),
    # Diagnostic again since stage 6b (user request).
    OltSensorDescription(
        key="cpu_temperature",
        translation_key="cpu_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # Shown in °C whatever the HA unit system (stage 6c).
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.cpu_temperature,
    ),
    # sta-device.asp "system uptime" as of the last read (every 5 minutes,
    # stage 6c); unknown when the page gave no usable value or the last
    # good read is older than two status intervals (stage 7).
    OltSensorDescription(
        key="system_uptime",
        translation_key="system_uptime",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # Hours since stage 7 (user decision); 0.1 h = 6 min matches the
        # 5-minute read interval. Existing entities: entity_cleanup.py.
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.uptime_seconds,
    ),
)
# Only on the 8080 master (sta-device.asp); extra port-80 hosts do not get
# them.
MASTER_ONLY_OLT_KEYS = {"cpu_temperature", "system_uptime"}
# CPU/memory usage only exist on the port-80 runinfo page; sta-device.asp
# (8080) has boot time and CPU temperature but no usage figures.
LEGACY_ONLY_OLT_KEYS = {"cpu_usage", "memory_usage"}


@dataclass(frozen=True, kw_only=True)
class ApSensorDescription(SensorEntityDescription):
    """Description for an AP sensor."""

    value_fn: Callable[[RltechAp], int | float | str | None]
    # Live values that are meaningless while the AC reports the AP offline.
    requires_online: bool = False


AP_SENSORS = (
    ApSensorDescription(
        key="online",
        translation_key="ap_online",
        value_fn=lambda ap: "online" if ap.online else "offline" if ap.online is False else None,
    ),
    ApSensorDescription(
        key="assoc_count",
        translation_key="ap_assoc_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ap: ap.assoc_count,
        requires_online=True,
    ),
    ApSensorDescription(
        key="profile",
        translation_key="ap_profile",
        value_fn=lambda ap: ap.profile,
    ),
    ApSensorDescription(
        key="alias",
        translation_key="ap_alias",
        value_fn=lambda ap: ap.alias,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ApDetailSensorDescription(SensorEntityDescription):
    """Description for a slow AP detail sensor."""

    value_fn: Callable[[RltechApDetail], int | float | str | datetime | None]
    # Fallback from the every-poll AP list row when the detail has no value.
    ap_value_fn: Callable[[RltechAp], int | float | str | None] | None = None


AP_DETAIL_SENSORS = (
    ApDetailSensorDescription(
        key="optical_rx_power",
        translation_key="ap_optical_rx_power",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda detail: detail.optical_rx_power,
        ap_value_fn=lambda ap: ap.optical_rx_power,
    ),
    ApDetailSensorDescription(
        key="optical_tx_power",
        translation_key="ap_optical_tx_power",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda detail: detail.optical_tx_power,
        ap_value_fn=lambda ap: ap.optical_tx_power,
    ),
    ApDetailSensorDescription(
        key="reg_off_time",
        translation_key="ap_reg_off_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.reg_off_time,
    ),
    ApDetailSensorDescription(
        key="last_down_cause",
        translation_key="ap_last_off_reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.last_down_cause,
    ),
    ApDetailSensorDescription(
        key="source_host",
        translation_key="ap_source_host",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.source_host,
    ),
    ApDetailSensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.cpu_usage,
    ),
    ApDetailSensorDescription(
        key="cpu_temperature",
        translation_key="cpu_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # Shown in °C whatever the HA unit system (stage 6c).
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.cpu_temperature,
    ),
    ApDetailSensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.memory_usage,
    ),
    ApDetailSensorDescription(
        key="flash_usage",
        translation_key="flash_usage",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.flash_usage,
    ),
    ApDetailSensorDescription(
        key="last_boot",
        translation_key="last_boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda detail: detail.last_boot,
    ),
)

# Only port-80 legacy rows carry the OLT host an AP was seen on.
LEGACY_ONLY_AP_DETAIL_KEYS = {"source_host"}


@dataclass(frozen=True, kw_only=True)
class LanPonPortSensorDescription(SensorEntityDescription):
    """Description for a LAN-PON port sensor."""

    value_fn: Callable[[RltechLanPonPort], int | float | str | None]
    # Name / entity_id key when it differs from the unique_id key (stage 6b:
    # the unique_id key "current" is shown as "bias current").
    name_key: str | None = None


def _lanpon_link(port: RltechLanPonPort) -> str:
    # sta-user.asp creat_lanpon_info_tab (:1223): status "up" is connected,
    # anything else disconnected.
    return "connected" if port.status == "up" else "disconnected"


# LAN-PON link (connected/disconnected, stage 6b) and optical module values,
# all Diagnostic. The stage 6 link entities (status, rate, mode) stay
# deleted: the new link sensor has the unique_id key "link", never matched
# by the retired rule.
LANPON_PORT_SENSORS = (
    LanPonPortSensorDescription(
        key="link",
        translation_key="lanpon_port_link",
        device_class=SensorDeviceClass.ENUM,
        options=["connected", "disconnected"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_lanpon_link,
    ),
    LanPonPortSensorDescription(
        key="tx_power",
        translation_key="lanpon_port_tx_power",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.tx_power,
    ),
    LanPonPortSensorDescription(
        key="rx_power",
        translation_key="lanpon_port_rx_power",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.rx_power,
    ),
    LanPonPortSensorDescription(
        key="temperature",
        translation_key="lanpon_port_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # Shown in °C whatever the HA unit system (stage 6c).
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.temperature,
    ),
    LanPonPortSensorDescription(
        key="voltage",
        translation_key="lanpon_port_voltage",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.voltage,
    ),
    LanPonPortSensorDescription(
        key="current",
        name_key="bias_current",
        translation_key="lanpon_port_bias_current",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement="mA",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.current,
    ),
)


@dataclass(frozen=True, kw_only=True)
class UplinkPonSensorDescription(SensorEntityDescription):
    """Description for an AC uplink PON sensor (sta-network.asp)."""

    value_fn: Callable[[RltechUplinkPon], int | float | str | None]
    attrs_fn: Callable[[RltechUplinkPon], dict[str, object]] | None = None


def _uplink_link_type_attrs(pon: RltechUplinkPon) -> dict[str, object]:
    return {
        "page_link_type": pon.page_link_type,
        "pon_mode": pon.pon_mode,
        "phy_status": pon.phy_status,
        "ae_wan_speed": pon.ae_wan_speed,
    }


def _uplink_online_attrs(pon: RltechUplinkPon) -> dict[str, object]:
    return {
        "pon_state": pon.pon_state,
        "link_state": pon.link_state,
        "registration_state": pon.registration_state,
        "traffic_state": pon.traffic_state,
        "link_sta": pon.link_sta,
        "loid_status": pon.loid_status,
    }


UPLINK_PON_SENSORS = (
    # Main group (stage 6b): the two lines of the page's "network link
    # connection information" table. The uplink link / registration sensors
    # of stage 6 are retired (entity_cleanup.py).
    UplinkPonSensorDescription(
        key="link_type",
        translation_key="pon_link_type",
        device_class=SensorDeviceClass.ENUM,
        options=list(PON_LINK_TYPES.values()),
        value_fn=lambda p: p.link_type,
        attrs_fn=_uplink_link_type_attrs,
    ),
    UplinkPonSensorDescription(
        key="online_status",
        translation_key="pon_online_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(PON_ONLINE_STATES),
        value_fn=lambda p: p.online_status,
        attrs_fn=_uplink_online_attrs,
    ),
    # TX/RX power sit with the module readings in Diagnostic (decision D).
    UplinkPonSensorDescription(
        key="tx_power",
        translation_key="pon_tx_power",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.tx_power,
    ),
    UplinkPonSensorDescription(
        key="rx_power",
        translation_key="pon_rx_power",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.rx_power,
    ),
    UplinkPonSensorDescription(
        key="temperature",
        translation_key="pon_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # Shown in °C whatever the HA unit system (stage 6c).
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.temperature,
    ),
    UplinkPonSensorDescription(
        key="voltage",
        translation_key="pon_voltage",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement="V",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.voltage,
    ),
    UplinkPonSensorDescription(
        key="bias_current",
        translation_key="pon_bias_current",
        # Low-value diagnostic: disabled for new installs only (stage 7).
        entity_registry_enabled_default=False,
        native_unit_of_measurement="mA",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: p.bias_current,
    ),
)

# Names come from the translations (strings.json, stage 7). One naming
# scheme for the uplink and the LAN-PON ports (stage 6b): "PON <suffix>" and
# "LANPON{port} <suffix>" with the port number as a translation placeholder;
# the entity_ids follow the same rule ("pon_<key>", "lanpon<n>_<key>") and
# are set explicitly, so names never affect them.


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RltechConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RLTech FTTR sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    entities.extend(
        RltechControllerSensor(entry, coordinator, description)
        for description in CONTROLLER_SENSORS
    )
    hardware_status_enabled = settings_hardware_status_enabled(
        entry, DEFAULT_ENABLE_HARDWARE_STATUS
    )
    # AP detail values come from 8080 ap_online_detail.asp on every
    # deployment, so these sensors no longer depend on port 80 or MQTT.
    ap_detail_sensors = tuple(
        description
        for description in AP_DETAIL_SENSORS
        if description.key not in LEGACY_ONLY_AP_DETAIL_KEYS
    )
    legacy_ap_detail_sensors = tuple(
        description
        for description in AP_DETAIL_SENSORS
        if description.key in LEGACY_ONLY_AP_DETAIL_KEYS
    )
    # Controller status now also comes from 8080 sta-device.asp, so the
    # master OLT sensors no longer require port 80.
    entities.extend(
        RltechOltStatusSensor(entry, coordinator, description)
        for description in OLT_SENSORS
        if description.key not in LEGACY_ONLY_OLT_KEYS
    )
    async_add_entities(entities)

    # AP entities are keyed by SN, the AP identity (same as devices/buttons).
    known_aps: set[str] = set()
    warned_no_sn: set[str] = set()
    # Port-80-only entities are created once port 80 has answered at least
    # once (plan 3.5): a WAN deployment where it is closed never gets them.
    legacy_olt_sensors_added = False
    known_legacy_detail_aps: set[str] = set()
    known_lanpon_ports: set[int] = set()
    uplink_pon_added = False
    known_legacy_sources: set[str] = set()
    known_legacy_lanpon_ports: set[tuple[str, int]] = set()

    @callback
    def forget_removed_ap(sn: str) -> None:
        # A removed device takes its entities with it; forget the SN so the
        # AP gets new entities if it is reported again.
        known_aps.discard(sn)
        known_legacy_detail_aps.discard(sn)

    entry.async_on_unload(
        coordinator.device_manager.async_add_ap_removed_listener(forget_removed_ap)
    )

    @callback
    def add_dynamic_entities() -> None:
        nonlocal legacy_olt_sensors_added, uplink_pon_added
        if coordinator.data is None:
            return
        new_entities = []
        for mac, ap in coordinator.data.aps.items():
            if not ap.sn:
                if mac not in warned_no_sn:
                    # Once per MAC, not on every poll.
                    warned_no_sn.add(mac)
                    _LOGGER.warning(
                        "Skipping AP %s sensors because the AP row has no SN", mac
                    )
                continue
            if ap.sn in known_aps:
                continue
            known_aps.add(ap.sn)
            new_entities.extend(
                RltechApSensor(entry, coordinator, ap.sn, description)
                for description in AP_SENSORS
            )
            new_entities.extend(
                RltechApDetailSensor(entry, coordinator, ap.sn, description)
                for description in ap_detail_sensors
            )
        legacy_seen = hardware_status_enabled and bool(
            coordinator.data.legacy_sources
        )
        # Master CPU/memory come from the master port-80 source only (review
        # N4): a slave-only success must not create them.
        legacy_urls = coordinator.client.legacy_base_urls
        master_host = (
            urlsplit(legacy_urls[0]).hostname or legacy_urls[0] if legacy_urls else None
        )
        master_seen = legacy_seen and master_host in coordinator.data.legacy_sources
        if master_seen and not legacy_olt_sensors_added:
            legacy_olt_sensors_added = True
            new_entities.extend(
                RltechOltStatusSensor(entry, coordinator, description)
                for description in OLT_SENSORS
                if description.key in LEGACY_ONLY_OLT_KEYS
            )
        if legacy_seen:
            for sn in known_aps - known_legacy_detail_aps:
                if sn not in coordinator.data.aps_by_sn:
                    continue
                known_legacy_detail_aps.add(sn)
                new_entities.extend(
                    RltechApDetailSensor(entry, coordinator, sn, description)
                    for description in legacy_ap_detail_sensors
                )
        # Uplink PON: once sta-network.asp (8080) has been parsed.
        if not uplink_pon_added and coordinator.data.uplink_pon is not None:
            uplink_pon_added = True
            new_entities.extend(
                RltechUplinkPonSensor(entry, coordinator, description)
                for description in UPLINK_PON_SENSORS
            )
        # Master LAN-PON ports come from sta-user.asp (8080) or port 80; once
        # the AC's own ponport_info list is known only its ports get
        # entities (RH8001GR: LANPON1 only, stage 6b).
        reported_lanpon = coordinator.data.web_lanpon_ports
        for ponid in coordinator.data.lanpon_ports:
            if ponid in known_lanpon_ports:
                continue
            if reported_lanpon and ponid not in reported_lanpon:
                continue
            known_lanpon_ports.add(ponid)
            new_entities.extend(
                RltechLanPonPortSensor(entry, coordinator, ponid, description)
                for description in LANPON_PORT_SENSORS
            )
        if hardware_status_enabled:
            legacy_hosts = list(coordinator.data.legacy_sources)
            master_host = legacy_hosts[0] if legacy_hosts else None
            for host, source in coordinator.data.legacy_sources.items():
                if host == master_host:
                    continue
                if host not in known_legacy_sources:
                    known_legacy_sources.add(host)
                    new_entities.extend(
                        RltechLegacyOltStatusSensor(entry, coordinator, host, description)
                        for description in OLT_SENSORS
                        if description.key not in MASTER_ONLY_OLT_KEYS
                    )
                for ponid in source.lanpon_ports:
                    key = (host, ponid)
                    if key in known_legacy_lanpon_ports:
                        continue
                    known_legacy_lanpon_ports.add(key)
                    new_entities.extend(
                        RltechLanPonPortSensor(
                            entry,
                            coordinator,
                            ponid,
                            description,
                            source_host=host,
                        )
                        for description in LANPON_PORT_SENSORS
                    )
        if new_entities:
            async_add_entities(new_entities)
    add_dynamic_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_dynamic_entities))


class RltechControllerSensor(RltechEntity, SensorEntity):
    """Controller aggregate sensor."""

    entity_description: RltechSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        description: RltechSensorDescription,
    ) -> None:
        super().__init__(entry, coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        _set_entity_object_id(
            self,
            controller_sensor_object_id(_entry_host(entry), description.key),
        )
        self._attr_device_info = controller_device_info(entry)

    @property
    def native_value(self) -> int | float | str | datetime | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class RltechOltStatusSensor(RltechEntity, SensorEntity):
    """Optional OLT status sensor."""

    entity_description: OltSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        description: OltSensorDescription,
    ) -> None:
        super().__init__(entry, coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_olt_{description.key}"
        _set_entity_object_id(
            self,
            controller_sensor_object_id(_entry_host(entry), description.key),
        )
        self._attr_device_info = controller_device_info(entry)

    @property
    def native_value(self) -> int | float | str | bool | datetime | None:
        if self.coordinator.data is None or self.coordinator.data.olt_status is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data.olt_status)

    def _object_available(self) -> bool:
        data = self.coordinator.data
        if data is None or data.olt_status is None:
            return False
        if self.entity_description.key in LEGACY_ONLY_OLT_KEYS:
            return source_available(self._source_state(SOURCE_LEGACY))
        return True



class RltechUplinkPonSensor(RltechEntity, SensorEntity):
    """AC uplink PON link / optical module sensor (8080 sta-network.asp)."""

    entity_description: UplinkPonSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        description: UplinkPonSensorDescription,
    ) -> None:
        super().__init__(entry, coordinator)
        self.entity_description = description
        # unique_id keeps the stage 6 "uplink_pon_" key (history); name
        # (translation pon_*) and entity_id use the unified "PON" prefix.
        self._attr_unique_id = f"{entry.entry_id}_uplink_pon_{description.key}"
        _set_entity_object_id(
            self, uplink_pon_object_id(_entry_host(entry), description.key)
        )
        self._attr_device_info = controller_device_info(entry)

    @property
    def _pon(self) -> RltechUplinkPon | None:
        data = self.coordinator.data
        return data.uplink_pon if data is not None else None

    @property
    def native_value(self) -> int | float | str | None:
        # None (unknown) when the page has no real reading, e.g. no fibre.
        pon = self._pon
        return self.entity_description.value_fn(pon) if pon is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        pon = self._pon
        if pon is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(pon)

    def _object_available(self) -> bool:
        return self._pon is not None


class RltechLegacyOltStatusSensor(RltechEntity, SensorEntity):
    """Hardware status sensor for an additional legacy OLT source."""

    entity_description: OltSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        host: str,
        description: OltSensorDescription,
    ) -> None:
        super().__init__(entry, coordinator)
        self._host = host
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_legacy_olt_{host}_{description.key}"
        _set_entity_object_id(
            self,
            controller_sensor_object_id(host, description.key),
        )

    @property
    def native_value(self) -> int | float | str | bool | datetime | None:
        source = self._source
        if source is None or source.olt_status is None:
            return None
        return self.entity_description.value_fn(source.olt_status)

    @property
    def device_info(self):
        return legacy_olt_device_info(self.config_entry, self._host)

    @property
    def _source(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.legacy_sources.get(self._host)

    def _object_available(self) -> bool:
        return self._source is not None and source_available(
            self._source_state(f"{SOURCE_LEGACY}:{self._host}")
        )


class RltechApSensor(RltechEntity, SensorEntity):
    """Managed AP sensor."""

    entity_description: ApSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        sn: str,
        description: ApSensorDescription,
    ) -> None:
        super().__init__(entry, coordinator)
        self._sn = sn
        self.entity_description = description
        ap = self._ap
        unique_id = ap_sensor_unique_id(entry.entry_id, ap, description.key)
        if unique_id is None:
            raise ValueError(f"AP {sn} is not reported")
        self._attr_unique_id = unique_id
        object_id = ap_sensor_object_id(ap, description.key)
        if object_id is None:
            raise ValueError(f"AP {sn} is not reported")
        _set_entity_object_id(
            self,
            object_id,
        )

    @property
    def native_value(self) -> int | float | str | None:
        ap = self._ap
        return self.entity_description.value_fn(ap) if ap else None

    def _object_available(self) -> bool:
        return ap_entity_available(
            self._ap, requires_online=self.entity_description.requires_online
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        ap = self._ap
        if ap is None:
            return {}
        return {
            "ip": ap.ip,
            "model": ap.model,
            "version": ap.version,
            "sn": ap.sn,
            "dev_sn": ap.dev_sn,
        }

    @property
    def device_info(self):
        return ap_device_info(self.config_entry, self._sn)

    @property
    def _ap(self) -> RltechAp | None:
        if self.coordinator.data is None:
            return None
        # Looked up by SN so an AP whose MAC changed keeps its entities.
        return self.coordinator.data.aps_by_sn.get(self._sn)


class RltechApDetailSensor(RltechEntity, SensorEntity):
    """Slow managed AP detail sensor."""

    entity_description: ApDetailSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        sn: str,
        description: ApDetailSensorDescription,
    ) -> None:
        super().__init__(entry, coordinator)
        self._sn = sn
        self.entity_description = description
        ap = self._ap
        unique_id = ap_sensor_unique_id(entry.entry_id, ap, description.key)
        if unique_id is None:
            raise ValueError(f"AP {sn} is not reported")
        self._attr_unique_id = unique_id
        object_id = ap_sensor_object_id(ap, description.key)
        if object_id is None:
            raise ValueError(f"AP {sn} is not reported")
        _set_entity_object_id(
            self,
            object_id,
        )

    @property
    def native_value(self) -> int | float | str | datetime | None:
        detail = self._detail
        value = self.entity_description.value_fn(detail) if detail else None
        if value is None and self.entity_description.ap_value_fn is not None:
            ap = self._ap
            if ap is not None:
                value = self.entity_description.ap_value_fn(ap)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        detail = self._detail
        if detail is None:
            return {}
        return {
            "last_update": detail.last_update.isoformat() if detail.last_update else None,
            "detail_source": detail.detail_source,
        }

    def _object_available(self) -> bool:
        ap = self._ap
        description = self.entity_description
        if description.key in LEGACY_ONLY_AP_DETAIL_KEYS and not source_available(
            self._source_state(SOURCE_LEGACY)
        ):
            return False
        return ap_detail_entity_available(
            ap,
            self._detail,
            now=dt_util.utcnow(),
            max_age=AP_DETAIL_MAX_AGE,
            fallback_value=(
                description.ap_value_fn(ap)
                if ap is not None and description.ap_value_fn is not None
                else None
            ),
        )

    @property
    def device_info(self):
        return ap_device_info(self.config_entry, self._sn)

    @property
    def _ap(self) -> RltechAp | None:
        if self.coordinator.data is None:
            return None
        # Looked up by SN so an AP whose MAC changed keeps its entities.
        return self.coordinator.data.aps_by_sn.get(self._sn)

    @property
    def _detail(self) -> RltechApDetail | None:
        ap = self._ap
        if ap is None or self.coordinator.data is None:
            return None
        return self.coordinator.data.ap_details.get(ap.mac)


class RltechLanPonPortSensor(RltechEntity, SensorEntity):
    """LAN-PON port sensor."""

    entity_description: LanPonPortSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: RltechCoordinator,
        ponid: int,
        description: LanPonPortSensorDescription,
        *,
        source_host: str | None = None,
    ) -> None:
        super().__init__(entry, coordinator)
        self._ponid = ponid
        self._source_host = source_host
        self.entity_description = description
        source_prefix = f"legacy_olt_{source_host}_" if source_host else ""
        self._attr_unique_id = (
            f"{entry.entry_id}_{source_prefix}lanpon_port_{ponid}_{description.key}"
        )
        name_key = description.name_key or description.key
        self._attr_translation_placeholders = {"port": str(ponid)}
        _set_entity_object_id(
            self,
            lanpon_object_id(source_host or _entry_host(entry), ponid, name_key),
        )

    @property
    def native_value(self) -> int | float | str | None:
        port = self._lanpon_port
        return self.entity_description.value_fn(port) if port else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        port = self._lanpon_port
        if port is None:
            return {}
        return {
            "ponid": port.ponid,
            "status": port.status,
            "active": port.active,
            "fec": port.fec,
            "autoregister": port.autoregister,
        }

    def _object_available(self) -> bool:
        if self._lanpon_port is None:
            return False
        if self._source_host:
            return source_available(
                self._source_state(f"{SOURCE_LEGACY}:{self._source_host}")
            )
        return True

    @property
    def _lanpon_port(self) -> RltechLanPonPort | None:
        if self.coordinator.data is None:
            return None
        if self._source_host:
            source = self.coordinator.data.legacy_sources.get(self._source_host)
            return source.lanpon_ports.get(self._ponid) if source else None
        return self.coordinator.data.lanpon_ports.get(self._ponid)

    @property
    def device_info(self):
        if not self._source_host:
            return controller_device_info(self.config_entry)
        return legacy_olt_device_info(self.config_entry, self._source_host)
