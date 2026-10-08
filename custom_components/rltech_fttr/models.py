"""Data models for RLTech FTTR."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property


@dataclass(frozen=True)
class RltechAp:
    """Managed AP inventory row."""

    mac: str
    ip: str | None = None
    model: str | None = None
    version: str | None = None
    online: bool | None = None
    profile: str | None = None
    profile_idx: str | None = None
    channel_24: int | None = None
    channel_5: int | None = None
    bssid_24: str | None = None
    bssid_5: str | None = None
    assoc_count: int | None = None
    alias: str | None = None
    uplink: int | None = None
    uplink_port: int | None = None
    sn: str | None = None
    dev_sn: str | None = None
    upgrade_flag: str | None = None
    optical_tx_power: float | None = None
    optical_rx_power: float | None = None


@dataclass(frozen=True)
class RltechApDetail:
    """Detailed managed AP status from ap_online_detail.asp."""

    mac: str | None = None
    last_update: datetime | None = None
    ip: str | None = None
    model: str | None = None
    version: str | None = None
    online: bool | None = None
    profile: str | None = None
    alias: str | None = None
    sn: str | None = None
    dev_sn: str | None = None
    uplink: int | None = None
    uplink_port: int | None = None
    assoc_count: int | None = None
    channel_24: int | None = None
    channel_5: int | None = None
    bssid_24: str | None = None
    bssid_5: str | None = None
    hostname: str | None = None
    sys_duration: int | None = None
    last_boot: datetime | None = None
    ram_size: int | None = None
    flash_size: int | None = None
    cpu_usage: float | None = None
    cpu_temperature: float | None = None
    memory_usage: float | None = None
    flash_usage: float | None = None
    pon_id: int | None = None
    onu_id: int | None = None
    pon_sn: str | None = None
    onu_status: str | None = None
    ont_distance: int | None = None
    optical_temperature: float | None = None
    optical_current: float | None = None
    optical_voltage: float | None = None
    optical_tx_power: float | None = None
    optical_rx_power: float | None = None
    downstream_optical_rx_power: float | None = None
    optical_error_status: int | None = None
    active: bool | None = None
    last_up_time: str | None = None
    last_down_time: str | None = None
    last_down_at: datetime | None = None
    last_dying_gasp_time: str | None = None
    last_down_cause: str | None = None
    reg_off_time: datetime | None = None
    interface: str | None = None
    source_host: str | None = None
    register_status: str | None = None
    identify_vendor: str | None = None
    equipment_id: str | None = None
    sn_address: str | None = None
    hardware_version: str | None = None
    software_version: str | None = None
    firmware_version: str | None = None
    detail_source: str | None = None
    detail_error: str | None = None
    web_detail_update: datetime | None = None


# RebootStatus of an XQuery_Task_List row (phase 0, appendix A.5): 0 queued or
# sent, 3 running (AP offline), 1 done; the Web UI treats 1 and 2 as finished,
# so 2 is most likely a failure (not observed).
REBOOT_STATUS_STATES = {
    "0": "queued",
    "3": "rebooting",
    "1": "completed",
    "2": "failed",
}


@dataclass(frozen=True)
class RltechApTask:
    """One AP task row (XQuery_Task_List, embedded in ap_online_list.asp)."""

    mac: str
    action: str | None = None
    upgrade_status: str | None = None
    reboot_status: str | None = None
    restore_status: str | None = None

    @property
    def reboot_state(self) -> str | None:
        """Readable RebootStatus for a reboot (Action 2) task, else None."""
        if self.action != "2" or self.reboot_status is None:
            return None
        return REBOOT_STATUS_STATES.get(self.reboot_status, "unknown")


@dataclass(frozen=True)
class RltechApRebootRecord:
    """Outcome of the last AP reboot request sent through the AC."""

    mac: str
    requested_at: datetime
    # submitted / rejected_offline / rejected_not_listed / rejected_task_pending
    # / rejected_slow
    # / unconfirmed / busy / locked / auth_failed / unreachable / error
    result: str
    reboot_status: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RltechAcRebootRecord:
    """Outcome of the last AC reboot request (mag-reset.asp, stage 6b)."""

    requested_at: datetime
    # submitted: a clean 200 answer. submitted_unconfirmed: the form may be
    # on the wire but the answer was not clean (3xx, login page, other
    # status, timeout, dropped connection; review S1); never retried.
    # Otherwise nothing was sent: busy / locked / auth_failed / unreachable
    # (connect failed) / error.
    result: str
    # Error class name, or why the answer was not clean (redirect_302,
    # http_500, login_page).
    error: str | None = None
    # mag-reset.asp answer, for diagnostics (review S1).
    http_status: int | None = None
    response_head: str | None = None


@dataclass(frozen=True)
class RltechStation:
    """Wi-Fi station row."""

    mac: str
    reported_online: bool
    home: bool
    last_seen: datetime | None
    first_seen: datetime | None = None
    id: str | None = None
    ip: str | None = None
    hostname: str | None = None
    vendor: str | None = None
    ssid: str | None = None
    ap_mac: str | None = None
    ap_alias: str | None = None
    rssi: int | None = None
    rx_rate: float | None = None
    tx_rate: float | None = None
    rx_nego_rate: float | None = None
    tx_nego_rate: float | None = None
    uptime: int | None = None
    channel: int | None = None
    band: str | None = None
    bandwidth: str | None = None
    vlan: int | None = None
    total_count: str | None = None
    update_time: int | None = None


@dataclass(frozen=True)
class RltechOltStatus:
    """Optional OLT/controller status parsed from sta-device.asp."""

    pon_link_state: int | None = None
    fec_state: bool | None = None
    pon_tx_frames: int | None = None
    pon_rx_frames: int | None = None
    pon_up_since: str | None = None
    current_time: str | None = None
    system_uptime: str | None = None
    # AC uptime in seconds when this page was read (stage 6c); None unless
    # the page gave a positive value.
    uptime_seconds: int | None = None
    wan_link_uptime: str | None = None
    last_boot: datetime | None = None
    wan_link_up_since: datetime | None = None
    device_type: str | None = None
    gateway_type: str | None = None
    cpu_temperature: float | None = None
    cpu_usage: float | None = None
    memory_usage: float | None = None
    flash_usage: float | None = None
    manufacturer: str | None = None
    serial_number: str | None = None
    hardware_version: str | None = None
    software_version: str | None = None
    # Raw "<OUI>-<serial>" device identifier shown by sta-device.asp.
    device_identifier: str | None = None
    # Controller LAN (br0) identity from sta-user.asp.
    lan_ip: str | None = None
    lan_mac: str | None = None


@dataclass(frozen=True)
class RltechLanPort:
    """LAN Ethernet port status parsed from sta-user.asp."""

    port: int
    label: str | None = None
    path: str | None = None
    status: str | None = None
    connected: bool | None = None
    rate: str | None = None
    mode: str | None = None
    tx_bytes: int | None = None
    tx_packets: int | None = None
    tx_errors: int | None = None
    tx_drops: int | None = None
    rx_bytes: int | None = None
    rx_packets: int | None = None
    rx_errors: int | None = None
    rx_drops: int | None = None


@dataclass(frozen=True)
class RltechLanPonPort:
    """LAN-PON port status parsed from sta-user.asp."""

    ponid: int
    status: str | None = None
    active: str | None = None
    fec: str | None = None
    autoregister: str | None = None
    tx_power: float | None = None
    rx_power: float | None = None
    temperature: float | None = None
    voltage: float | None = None
    current: float | None = None


@dataclass(frozen=True)
class RltechUplinkPon:
    """AC uplink PON link and optical module (sta-network.asp, 8080).

    Only the PON link section; WAN addressing is deliberately not read.
    Optical values are None whenever the page has no real reading (no fibre,
    no module), never the 0 V / -40 dBm the page JS would print.
    """

    # "up" / "down" from phyStatus, LinkSta and trafficstate (not PonState,
    # which the page overwrites unconditionally).
    link_state: str | None = None
    # "authenticated" / "unauthenticated" / "unregistered" (phy + traffic,
    # the page's registration row, sta-network.asp:1428-1433).
    registration_state: str | None = None
    # Page loidStatus: "up" / "error" / "init".
    loid_status: str | None = None
    pon_type: str | None = None
    link_sta: str | None = None
    traffic_state: str | None = None
    phy_status: str | None = None
    fec_state: str | None = None
    pon_mode: str | None = None
    tx_power: float | None = None  # dBm
    rx_power: float | None = None  # dBm
    temperature: float | None = None  # degC
    voltage: float | None = None  # V
    bias_current: float | None = None  # mA
    # PonInfo.PonState as the page JS leaves it (stage 6b); on ActiveEtherWan
    # builds that is WanInfo_Common.EthernetState (sta-network.asp:420).
    pon_state: str | None = None
    # Page "PON link status" line: online / connecting / offline.
    online_status: str | None = None
    # get_pontype() as an ENUM option (gpon, ..., ge_10ge); None when the page
    # lacks phyStatus, PonMode or the rendered get_pontype body.
    link_type: str | None = None
    # What the page JS itself would print (always computed, for attributes).
    page_link_type: str | None = None
    uplink_sfp_choose: str | None = None
    active_ether_wan: str | None = None
    ae_wan_speed: str | None = None

    @property
    def optics_state(self) -> str | None:
        """Module summary: ok / no_signal / no_readings."""
        if self.voltage is None:
            return "no_readings"
        if self.link_state != "up" or self.rx_power is None:
            return "no_signal"
        return "ok"


@dataclass(frozen=True)
class RltechLegacyOltSource:
    """One legacy port-80 OLT hardware/status source."""

    host: str
    base_url: str
    olt_status: RltechOltStatus | None = None
    lan_ports: dict[int, RltechLanPort] = field(default_factory=dict)
    lanpon_ports: dict[int, RltechLanPonPort] = field(default_factory=dict)
    last_success: datetime | None = None


@dataclass(frozen=True)
class RltechData:
    """Complete normalized coordinator snapshot."""

    aps: dict[str, RltechAp] = field(default_factory=dict)
    ap_details: dict[str, RltechApDetail] = field(default_factory=dict)
    stations: dict[str, RltechStation] = field(default_factory=dict)
    olt_status: RltechOltStatus | None = None
    lan_ports: dict[int, RltechLanPort] = field(default_factory=dict)
    lanpon_ports: dict[int, RltechLanPonPort] = field(default_factory=dict)
    legacy_sources: dict[str, RltechLegacyOltSource] = field(default_factory=dict)
    last_success: datetime | None = None
    last_success_8080: datetime | None = None
    last_success_80: datetime | None = None
    poll_duration_ms: int | None = None
    # Controller status read over 8080 (sta-device.asp / sta-user.asp), kept
    # separately so the slower refresh can be merged with port-80 values.
    web_status: RltechOltStatus | None = None
    web_lan_ports: dict[int, RltechLanPort] = field(default_factory=dict)
    web_lanpon_ports: dict[int, RltechLanPonPort] = field(default_factory=dict)
    web_status_update: datetime | None = None
    # When sta-device/sta-user last gave a status (stage 7). web_status_update
    # is the last *attempt*; once this is older than
    # WEB_STATUS_MAX_STATUS_CYCLES the status readings are dropped.
    web_status_parsed: datetime | None = None
    # Uplink PON link/optics from sta-network.asp (8080, status interval).
    uplink_pon: RltechUplinkPon | None = None
    # When sta-network.asp last parsed; uplink_pon is dropped (None, entities
    # unavailable) once this is older than UPLINK_PON_MAX_STATUS_CYCLES.
    uplink_pon_update: datetime | None = None
    # AP task rows (reboot/upgrade status) by AP MAC, from the AP list page.
    ap_tasks: dict[str, RltechApTask] = field(default_factory=dict)
    # Per-source state names ("web", "legacy", "legacy:<host>", "mqtt"),
    # see sources.py; drives entity availability.
    sources: dict[str, str] = field(default_factory=dict)

    @cached_property
    def aps_by_sn(self) -> dict[str, RltechAp]:
        """AP rows keyed by SN, the AP identity used by devices and entities."""
        return {ap.sn: ap for ap in self.aps.values() if ap.sn}
