"""MQTT live overlay for RLTech FTTR."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import json
import logging
import re
import ssl
import struct
from typing import Any

from .models import RltechAp, RltechApDetail, RltechData, RltechStation

_LOGGER = logging.getLogger(__name__)

TOPIC_AP_NOTIFY = "/homeGatewayProxy/v2/AP2AC/messsage/notify"
TOPIC_AP_ONLINE = "/homeGatewayProxy/v2/AP/messsage/online"
TOPIC_AP_OFFLINE = "/homeGatewayProxy/v2/AP/messsage/offline"
MQTT_TOPICS = [TOPIC_AP_NOTIFY, TOPIC_AP_ONLINE, TOPIC_AP_OFFLINE]
MQTT_KEEPALIVE = 60
MQTT_PING_INTERVAL = 30
LAST_BOOT_STABILITY = timedelta(seconds=60)
BANDWIDTH_MAP = {
    1: "20 MHz",
    2: "40 MHz",
    3: "20/40 MHz",
    4: "20/40/80 MHz",
    5: "20/40/80/160 MHz",
}


class RltechMqttError(RuntimeError):
    """Raised when the MQTT transport or protocol fails."""


@dataclass(frozen=True, slots=True)
class MqttStationUpdate:
    """Normalized station update from XReport_StaList."""

    mac: str
    ap_mac: str | None = None
    ip: str | None = None
    hostname: str | None = None
    ssid: str | None = None
    rssi: int | None = None
    band: str | None = None
    channel: int | None = None
    bandwidth: str | None = None
    vlan: int | None = None
    rx_rate: float | None = None
    tx_rate: float | None = None
    rx_nego_rate: float | None = None
    tx_nego_rate: float | None = None
    uptime: int | None = None
    reported_online: bool = True


@dataclass(frozen=True, slots=True)
class MqttApHealthUpdate:
    """Normalized known-AP health update from XReport_ExtendInfo."""

    mac: str
    pon_sn: str | None = None
    assoc_count: int | None = None
    cpu_usage: float | None = None
    cpu_temperature: float | None = None
    memory_usage: float | None = None
    flash_usage: float | None = None
    uptime: int | None = None
    optical_tx_power: float | None = None
    optical_rx_power: float | None = None
    last_down_cause: str | None = None


@dataclass(frozen=True, slots=True)
class MqttApStatusUpdate:
    """Normalized known-AP online/offline update."""

    online: bool
    mac: str | None = None
    sn: str | None = None


@dataclass(slots=True)
class RltechMqttStats:
    """Runtime MQTT diagnostics."""

    enabled: bool = False
    connected: bool = False
    last_connect: datetime | None = None
    last_message: datetime | None = None
    last_station_message: datetime | None = None
    reconnect_count: int = 0
    message_counts: Counter[str] = field(default_factory=Counter)
    tls_version: str | None = None
    tls_cipher: str | None = None
    last_error: str | None = None
    last_error_kind: str | None = None
    next_retry: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a diagnostics-safe dictionary."""
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "last_connect": (
                self.last_connect.isoformat() if self.last_connect else None
            ),
            "last_message": (
                self.last_message.isoformat() if self.last_message else None
            ),
            "last_station_message": (
                self.last_station_message.isoformat()
                if self.last_station_message
                else None
            ),
            "reconnect_count": self.reconnect_count,
            "message_counts": dict(sorted(self.message_counts.items())),
            "tls_version": self.tls_version,
            "tls_cipher": self.tls_cipher,
            "last_error": self.last_error,
            "last_error_kind": self.last_error_kind,
            "next_retry": self.next_retry.isoformat() if self.next_retry else None,
        }


MQTT_INITIAL_BACKOFF = 5
MQTT_MAX_BACKOFF = 300


def mqtt_error_kind(err: BaseException) -> str:
    """Classify an MQTT connection failure (str(TimeoutError()) is empty)."""
    if isinstance(err, TimeoutError):
        return "timeout"
    if isinstance(err, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(err, ssl.SSLError):
        return "tls_handshake"
    if isinstance(err, RltechMqttError):
        return "connack_refused" if "CONNACK refused" in str(err) else "protocol"
    if isinstance(err, (asyncio.IncompleteReadError, ConnectionError)):
        return "disconnected"
    if isinstance(err, OSError):
        return "unreachable"
    return "error"


def mqtt_next_backoff(current: int) -> int:
    """Return the next reconnect delay (doubling, capped at 5 minutes)."""
    return min(max(current, MQTT_INITIAL_BACKOFF) * 2, MQTT_MAX_BACKOFF)


def build_psk_context(psk_identity: str, psk_hex: str) -> ssl.SSLContext:
    """Return a native TLS-PSK SSL context for the RLTech broker."""
    if not hasattr(ssl.SSLContext, "set_psk_client_callback"):
        raise RltechMqttError("Python ssl lacks TLS-PSK support; use Python 3.14+")
    try:
        psk = bytes.fromhex(psk_hex)
    except ValueError as err:
        raise RltechMqttError("MQTT PSK must be hex") from err
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.MAXIMUM_SUPPORTED
    context.set_psk_client_callback(lambda _hint: (psk_identity, psk))
    return context


def parse_mqtt_payload(
    payload: str,
    topic: str = "",
) -> tuple[
    str, list[MqttStationUpdate] | MqttApHealthUpdate | MqttApStatusUpdate | None
]:
    """Parse one MQTT payload into a supported normalized update."""
    if topic in {TOPIC_AP_ONLINE, TOPIC_AP_OFFLINE}:
        online = topic == TOPIC_AP_ONLINE
        return (
            "APOnline" if online else "APOffline",
            parse_ap_status(payload, online=online),
        )
    try:
        message = json.loads(payload)
    except json.JSONDecodeError as err:
        raise RltechMqttError("invalid JSON MQTT payload") from err
    if not isinstance(message, dict):
        raise RltechMqttError("unsupported MQTT payload shape")
    cmd = str(message.get("Cmd") or "")
    if cmd == "XReport_StaList":
        return cmd, parse_station_list(message)
    if cmd == "XReport_ExtendInfo":
        return cmd, parse_ap_health(message)
    return cmd or "unknown", None


def parse_station_list(message: dict[str, Any]) -> list[MqttStationUpdate]:
    """Parse XReport_StaList into normalized station rows."""
    data = message.get("Data")
    if not isinstance(data, dict):
        return []
    rows = data.get("List")
    if not isinstance(rows, list):
        return []
    stations: list[MqttStationUpdate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mac = _normalize_mac(row.get("Mac"))
        if not mac:
            continue
        status = _int(row.get("Status"))
        channel = _int(row.get("Channel"))
        stations.append(
            MqttStationUpdate(
                mac=mac,
                ap_mac=_normalize_mac(row.get("APMac")),
                ip=_none_if_empty(row.get("IP")),
                hostname=_none_if_empty(row.get("HostName")),
                ssid=_none_if_empty(row.get("SSID")),
                rssi=_int(row.get("RSSI")),
                band=_channel_band(channel),
                channel=channel,
                bandwidth=_bandwidth_label(row.get("Bandwidth")),
                vlan=_int(row.get("Vlan")),
                rx_rate=_float(row.get("RxRate")),
                tx_rate=_float(row.get("TxRate")),
                rx_nego_rate=_float(row.get("RxNegoRate")),
                tx_nego_rate=_float(row.get("TxNegoRate")),
                uptime=_int(row.get("UpTime")),
                reported_online=status != 0,
            )
        )
    return stations


def parse_ap_health(message: dict[str, Any]) -> MqttApHealthUpdate | None:
    """Parse XReport_ExtendInfo into a compact known-AP health update."""
    data = message.get("Data")
    if not isinstance(data, dict):
        return None
    mac = _normalize_mac(message.get("Send") or data.get("Send") or data.get("Mac"))
    if not mac:
        return None
    return MqttApHealthUpdate(
        mac=mac,
        pon_sn=_none_if_empty(data.get("PONSN")),
        assoc_count=_int(data.get("Assoc")),
        cpu_usage=_float(data.get("CPUUsage")),
        cpu_temperature=_float(data.get("CPUTemp")),
        memory_usage=_float(data.get("MEMUsage")),
        flash_usage=_float(data.get("FlashUsage")),
        uptime=_int(data.get("SysDuration")),
        # XReport_ExtendInfo also carries the AP's own optics and last offline
        # reason (slaveMgr_v2 client/object/obj_client_info.c:45-68).
        optical_tx_power=_dbm(data.get("OptTxPower")),
        optical_rx_power=_dbm(data.get("OptRxPower")),
        last_down_cause=_none_if_null(data.get("LastOfflineReason")),
    )


def parse_ap_status(payload: str, *, online: bool) -> MqttApStatusUpdate | None:
    """Parse an AP lifecycle topic payload into a known-AP status update."""
    stripped = payload.strip()
    try:
        message: Any = json.loads(stripped) if stripped else {}
    except json.JSONDecodeError:
        message = stripped
    mac, sn = _extract_ap_identity(message)
    if mac is None and sn is None:
        return None
    return MqttApStatusUpdate(online=online, mac=mac, sn=sn)


def merge_station_updates(
    data: RltechData,
    updates: list[MqttStationUpdate],
    *,
    now: datetime,
) -> RltechData:
    """Merge MQTT station updates into an existing coordinator snapshot."""
    if not updates:
        return data
    stations = dict(data.stations)
    for update in updates:
        ap_alias = data.aps.get(update.ap_mac or "").alias if update.ap_mac else None
        previous = stations.get(update.mac)
        hostname = update.hostname or (previous.hostname if previous else None)
        stations[update.mac] = RltechStation(
            mac=update.mac,
            reported_online=update.reported_online,
            home=update.reported_online,
            first_seen=previous.first_seen if previous else now,
            last_seen=(
                now
                if update.reported_online
                else previous.last_seen
                if previous
                else None
            ),
            id=previous.id if previous else None,
            ip=update.ip or (previous.ip if previous else None),
            hostname=hostname,
            vendor=previous.vendor if previous else None,
            ssid=update.ssid or (previous.ssid if previous else None),
            ap_mac=update.ap_mac or (previous.ap_mac if previous else None),
            ap_alias=ap_alias or (previous.ap_alias if previous else None),
            rssi=_new_or_previous(update.rssi, previous.rssi if previous else None),
            rx_rate=_new_or_previous(
                update.rx_rate, previous.rx_rate if previous else None
            ),
            tx_rate=_new_or_previous(
                update.tx_rate, previous.tx_rate if previous else None
            ),
            rx_nego_rate=_new_or_previous(
                update.rx_nego_rate, previous.rx_nego_rate if previous else None
            ),
            tx_nego_rate=_new_or_previous(
                update.tx_nego_rate, previous.tx_nego_rate if previous else None
            ),
            uptime=_new_or_previous(
                update.uptime, previous.uptime if previous else None
            ),
            channel=_new_or_previous(
                update.channel, previous.channel if previous else None
            ),
            band=update.band or (previous.band if previous else None),
            bandwidth=update.bandwidth or (previous.bandwidth if previous else None),
            vlan=_new_or_previous(update.vlan, previous.vlan if previous else None),
            total_count=previous.total_count if previous else None,
            update_time=previous.update_time if previous else None,
        )
    return replace(data, stations=stations)


def merge_ap_health_update(
    data: RltechData,
    update: MqttApHealthUpdate | None,
    *,
    now: datetime,
) -> RltechData:
    """Merge one MQTT AP health update for an AP already known from HTTP."""
    if update is None or update.mac not in data.aps:
        return data
    aps = dict(data.aps)
    ap = aps[update.mac]
    if update.assoc_count is not None:
        aps[update.mac] = replace(ap, assoc_count=update.assoc_count)

    details = dict(data.ap_details)
    previous = details.get(update.mac)
    computed_last_boot = (
        now - timedelta(seconds=update.uptime) if update.uptime is not None else None
    )
    details[update.mac] = replace(
        previous or RltechApDetail(mac=update.mac),
        last_update=now,
        mac=update.mac,
        pon_sn=update.pon_sn or (previous.pon_sn if previous else None),
        sys_duration=(
            update.uptime
            if update.uptime is not None
            else previous.sys_duration
            if previous
            else None
        ),
        last_boot=_stable_datetime(
            computed_last_boot, previous.last_boot if previous else None
        ),
        cpu_usage=(
            update.cpu_usage
            if update.cpu_usage is not None
            else previous.cpu_usage
            if previous
            else None
        ),
        cpu_temperature=(
            update.cpu_temperature
            if update.cpu_temperature is not None
            else previous.cpu_temperature
            if previous
            else None
        ),
        memory_usage=(
            update.memory_usage
            if update.memory_usage is not None
            else previous.memory_usage
            if previous
            else None
        ),
        flash_usage=(
            update.flash_usage
            if update.flash_usage is not None
            else previous.flash_usage
            if previous
            else None
        ),
        optical_tx_power=_new_or_previous(
            update.optical_tx_power,
            previous.optical_tx_power if previous else None,
        ),
        optical_rx_power=_new_or_previous(
            update.optical_rx_power,
            previous.optical_rx_power if previous else None,
        ),
        last_down_cause=_new_or_previous(
            update.last_down_cause,
            previous.last_down_cause if previous else None,
        ),
        detail_source="mqtt",
    )
    return replace(data, aps=aps, ap_details=details)


def merge_ap_status_update(
    data: RltechData,
    update: MqttApStatusUpdate | None,
) -> RltechData:
    """Merge one MQTT AP online/offline update for an AP already known from HTTP."""
    if update is None:
        return data
    mac = update.mac if update.mac in data.aps else None
    if mac is None and update.sn is not None:
        mac = next(
            (
                ap_mac
                for ap_mac, ap in data.aps.items()
                if _normalize_sn(ap.sn) == update.sn
            ),
            None,
        )
    if mac is None:
        return data

    ap = data.aps[mac]
    if ap.online is update.online:
        return data
    aps = dict(data.aps)
    aps[mac] = replace(ap, online=update.online)
    return replace(data, aps=aps)


def preserve_live_overlay(
    current: RltechData, fresh: RltechData, previous: RltechData | None = None
) -> RltechData:
    """Keep MQTT-fed live fields when an HTTP poll returns an older snapshot."""
    aps = _preserve_live_ap_fields(current, fresh, previous)
    stations = _preserve_live_station_fields(current, fresh, previous)
    ap_details = _preserve_live_ap_detail_fields(current, fresh)
    return replace(fresh, aps=aps, stations=stations, ap_details=ap_details)


def _preserve_live_ap_fields(
    current: RltechData, fresh: RltechData, previous: RltechData | None
) -> dict[str, RltechAp]:
    """Return AP rows with MQTT-fresher values kept for known APs."""
    aps = dict(fresh.aps)
    for mac, current_ap in current.aps.items():
        if mac not in aps:
            continue
        if previous is not None and current_ap == previous.aps.get(mac):
            continue
        updates: dict[str, Any] = {}
        if current_ap.assoc_count is not None:
            updates["assoc_count"] = current_ap.assoc_count
        if current_ap.online is not None:
            updates["online"] = current_ap.online
        if updates:
            aps[mac] = replace(aps[mac], **updates)
    return aps


def _preserve_live_station_fields(
    current: RltechData, fresh: RltechData, previous: RltechData | None
) -> dict[str, RltechStation]:
    """Return station rows with the newest live station records kept."""
    stations = dict(fresh.stations)
    for mac, current_station in current.stations.items():
        fresh_station = stations.get(mac)
        if fresh_station is None:
            if previous is None or current_station != previous.stations.get(mac):
                stations[mac] = current_station
            continue
        if _station_is_newer(current_station, fresh_station):
            stations[mac] = current_station
            continue
        preserved = {}
        if not fresh_station.hostname and current_station.hostname:
            preserved["hostname"] = current_station.hostname
        if not fresh_station.vendor and current_station.vendor:
            preserved["vendor"] = current_station.vendor
        if preserved:
            stations[mac] = replace(fresh_station, **preserved)
    return stations


_MQTT_AP_HEALTH_FIELDS = (
    "sys_duration",
    "last_boot",
    "cpu_usage",
    "cpu_temperature",
    "memory_usage",
    "flash_usage",
    "optical_tx_power",
    "optical_rx_power",
    "last_down_cause",
)


def _preserve_live_ap_detail_fields(
    current: RltechData, fresh: RltechData
) -> dict[str, RltechApDetail]:
    """Return AP details with MQTT AP health fields retained.

    A health field from the live (MQTT-fed) record is kept when the fresh
    HTTP record has no value, or when the live record is newer than the
    fresh one. A fresh 8080 detail fetched during this poll therefore wins
    over the older values the poll started from.
    """
    details = dict(fresh.ap_details)
    for mac, current_detail in current.ap_details.items():
        if mac not in fresh.aps:
            continue
        fresh_detail = details.get(mac)
        if fresh_detail is None:
            details[mac] = current_detail
            continue
        current_is_newer = current_detail.last_update is not None and (
            fresh_detail.last_update is None
            or current_detail.last_update > fresh_detail.last_update
        )
        updates: dict[str, Any] = {}
        for name in _MQTT_AP_HEALTH_FIELDS:
            current_value = getattr(current_detail, name)
            if current_value is None:
                continue
            if current_is_newer or getattr(fresh_detail, name) is None:
                updates[name] = current_value
        if updates:
            details[mac] = replace(fresh_detail, **updates)
    return details


def _station_is_newer(current: RltechStation, fresh: RltechStation) -> bool:
    """Return whether the current station record is newer than the fresh one."""
    if current.last_seen is None:
        return False
    if fresh.last_seen is None:
        return True
    return current.last_seen > fresh.last_seen


class AsyncPskMqttClient:
    """Small asyncio MQTT 3.1.1 client for the RLTech local broker."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        psk_identity: str,
        psk_hex: str,
        client_id: str,
        keepalive: int = MQTT_KEEPALIVE,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.psk_identity = psk_identity
        self.psk_hex = psk_hex
        self.client_id = client_id
        self.keepalive = keepalive
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.tls_version: str | None = None
        self.tls_cipher: str | None = None
        self._packet_id = 0

    async def connect(self) -> None:
        """Open TLS-PSK transport and send MQTT CONNECT."""
        context = build_psk_context(self.psk_identity, self.psk_hex)
        self.reader, self.writer = await asyncio.open_connection(
            self.host,
            self.port,
            ssl=context,
            server_hostname=None,
        )
        ssl_object = self.writer.get_extra_info("ssl_object")
        if ssl_object:
            self.tls_version = ssl_object.version()
            cipher = ssl_object.cipher()
            self.tls_cipher = cipher[0] if cipher else None
        flags = 0x02
        payload = _pack_string(self.client_id)
        if self.username:
            flags |= 0x80
            payload += _pack_string(self.username)
        if self.password:
            flags |= 0x40
            payload += _pack_string(self.password)
        variable = (
            _pack_string("MQTT")
            + bytes([4, flags, self.keepalive >> 8, self.keepalive & 0xFF])
        )
        await self._write(_packet(0x10, variable + payload))
        packet_type, body = await self._read_packet()
        if packet_type != 0x20 or len(body) != 2:
            raise RltechMqttError(f"expected CONNACK, got 0x{packet_type:02x}")
        if body[1] != 0:
            raise RltechMqttError(f"CONNACK refused: {body[1]}")

    async def subscribe(self, topics: list[str]) -> None:
        """Subscribe to topic filters at QoS 0."""
        self._packet_id += 1
        payload = struct.pack("!H", self._packet_id)
        for topic in topics:
            payload += _pack_string(topic) + b"\x00"
        await self._write(_packet(0x82, payload))
        packet_type, body = await self._read_packet()
        if packet_type != 0x90 or len(body) < 3:
            raise RltechMqttError(f"expected SUBACK, got 0x{packet_type:02x}")
        if struct.unpack("!H", body[:2])[0] != self._packet_id:
            raise RltechMqttError("SUBACK packet id mismatch")
        if any(code == 0x80 for code in body[2:]):
            raise RltechMqttError("subscription rejected")

    async def read_message(self) -> tuple[str, str] | None:
        """Read one publish payload, handling ping/disconnect packets."""
        packet_type, body = await self._read_packet()
        kind = packet_type & 0xF0
        if kind == 0x30:
            topic, offset = _unpack_string(body, 0)
            qos = (packet_type >> 1) & 0x03
            if qos:
                packet_id = struct.unpack("!H", body[offset : offset + 2])[0]
                offset += 2
                if qos == 1:
                    await self._write(_packet(0x40, struct.pack("!H", packet_id)))
            return topic, body[offset:].decode(errors="replace")
        if kind == 0xD0:
            return None
        if kind == 0xE0:
            raise RltechMqttError("broker disconnected")
        return None

    async def ping(self) -> None:
        """Send MQTT PINGREQ."""
        await self._write(_packet(0xC0, b""))

    async def disconnect(self) -> None:
        """Send MQTT DISCONNECT and close the transport."""
        if self.writer:
            try:
                try:
                    await self._write(_packet(0xE0, b""))
                except (ConnectionError, OSError):
                    pass
            finally:
                self.writer.close()
                try:
                    await self.writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def _write(self, packet: bytes) -> None:
        if not self.writer:
            raise RltechMqttError("not connected")
        self.writer.write(packet)
        await self.writer.drain()

    async def _read_packet(self) -> tuple[int, bytes]:
        if not self.reader:
            raise RltechMqttError("not connected")
        first = (await self.reader.readexactly(1))[0]
        multiplier = 1
        remaining = 0
        while True:
            byte = (await self.reader.readexactly(1))[0]
            remaining += (byte & 127) * multiplier
            if not (byte & 128):
                break
            multiplier *= 128
            if multiplier > 128 * 128 * 128:
                raise RltechMqttError("malformed remaining length")
        return first, await self.reader.readexactly(remaining)


class RltechMqttManager:
    """Background MQTT loop that feeds parsed messages into the coordinator."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        psk_identity: str,
        psk_hex: str,
        client_id: str,
        apply_update: Callable[
            [
                str,
                list[MqttStationUpdate]
                | MqttApHealthUpdate
                | MqttApStatusUpdate
                | None,
                datetime,
            ],
            None,
        ],
        stats: RltechMqttStats,
        task_factory: Callable[[Coroutine[Any, Any, None], str], asyncio.Task]
        | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.psk_identity = psk_identity
        self.psk_hex = psk_hex
        self.client_id = client_id
        self._apply_update = apply_update
        self.stats = stats
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._client: AsyncPskMqttClient | None = None
        # HA passes entry.async_create_background_task (L5); plain asyncio
        # otherwise, so this module stays usable without Home Assistant.
        self._task_factory = task_factory or (
            lambda coro, name: asyncio.create_task(coro, name=name)
        )

    def start(self) -> None:
        """Start the background MQTT loop."""
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = self._task_factory(
                self._run(), f"rltech-fttr-mqtt-{self.host}"
            )

    async def stop(self) -> None:
        """Stop the background MQTT loop."""
        self._stopped.set()
        if self._client is not None:
            await self._client.disconnect()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        # MQTT is an optional LAN-only enhancement (mosquitto binds br0,
        # other_mgr.c:3770): reconnect with back-off and log at debug only.
        backoff = MQTT_INITIAL_BACKOFF
        while not self._stopped.is_set():
            client = AsyncPskMqttClient(
                self.host,
                self.port,
                self.username,
                self.password,
                psk_identity=self.psk_identity,
                psk_hex=self.psk_hex,
                client_id=self.client_id,
            )
            self._client = client
            try:
                await asyncio.wait_for(client.connect(), timeout=10)
                await asyncio.wait_for(client.subscribe(MQTT_TOPICS), timeout=10)
                self.stats.connected = True
                self.stats.last_connect = datetime.now().astimezone()
                self.stats.tls_version = client.tls_version
                self.stats.tls_cipher = client.tls_cipher
                self.stats.last_error = None
                self.stats.last_error_kind = None
                self.stats.next_retry = None
                backoff = MQTT_INITIAL_BACKOFF
                await self._read_loop(client)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.stats.connected = False
                kind = mqtt_error_kind(err)
                self.stats.last_error = str(err) or type(err).__name__
                self.stats.last_error_kind = kind
                self.stats.reconnect_count += 1
                self.stats.next_retry = datetime.now().astimezone() + timedelta(
                    seconds=backoff
                )
                _LOGGER.debug(
                    "RLTech MQTT feed failed (%s): %s; retrying in %s s",
                    kind,
                    self.stats.last_error,
                    backoff,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = mqtt_next_backoff(backoff)
            finally:
                if self._client is client:
                    self._client = None
        self.stats.connected = False

    async def _read_loop(self, client: AsyncPskMqttClient) -> None:
        while not self._stopped.is_set():
            try:
                message = await asyncio.wait_for(
                    client.read_message(), timeout=MQTT_PING_INTERVAL
                )
            except TimeoutError:
                await client.ping()
                continue
            if message is None:
                continue
            topic, payload = message
            try:
                cmd, update = parse_mqtt_payload(payload, topic)
            except RltechMqttError as err:
                self.stats.message_counts["parse_error"] += 1
                self.stats.last_error = str(err)
                continue
            now = datetime.now().astimezone()
            self.stats.last_message = now
            if cmd == "XReport_StaList":
                self.stats.last_station_message = now
            self.stats.message_counts[cmd] += 1
            if update is not None:
                self._apply_update(cmd, update, now)


def _encode_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        out.append(digit)
        if not length:
            return bytes(out)


def _pack_string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("!H", len(raw)) + raw


def _packet(packet_type_flags: int, payload: bytes) -> bytes:
    return bytes([packet_type_flags]) + _encode_remaining_length(len(payload)) + payload


def _unpack_string(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise RltechMqttError("truncated MQTT string length")
    size = struct.unpack("!H", data[offset : offset + 2])[0]
    offset += 2
    if offset + size > len(data):
        raise RltechMqttError("truncated MQTT string data")
    return data[offset : offset + size].decode(errors="replace"), offset + size


def _normalize_mac(value: Any) -> str | None:
    text = str(value or "").strip().replace(":", "").replace("-", "").upper()
    if len(text) != 12:
        return None
    return ":".join(text[index : index + 2] for index in range(0, 12, 2))


def _new_or_previous(new: Any, previous: Any) -> Any:
    """Return a new value when reported, otherwise keep the previous value."""
    return new if new is not None else previous


def _normalize_sn(value: Any) -> str | None:
    text = (
        str(value or "")
        .strip()
        .upper()
        .replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .replace(" ", "")
    )
    if not text.startswith("RLGM") or len(text) < 8:
        return None
    return text


def _extract_ap_identity(value: Any) -> tuple[str | None, str | None]:
    """Return AP MAC/SN identity from common RLTech lifecycle payload shapes."""
    if isinstance(value, dict):
        mac = _first_normalized(value, _normalize_mac, ("Mac", "MAC", "mac", "APMac"))
        sn = _first_normalized(
            value,
            _normalize_sn,
            ("SN", "sn", "PONSN", "PONSn", "PonSn", "pon_sn", "ponSn"),
        )
        if mac or sn:
            return mac, sn
        for nested_key in ("Data", "data", "Payload", "payload", "Body", "body"):
            nested = value.get(nested_key)
            if isinstance(nested, dict | str):
                mac, sn = _extract_ap_identity(nested)
                if mac or sn:
                    return mac, sn
        for nested in value.values():
            if isinstance(nested, dict):
                mac, sn = _extract_ap_identity(nested)
                if mac or sn:
                    return mac, sn
        return None, None
    if isinstance(value, str):
        mac_match = re.search(
            r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}|(?<![0-9A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Fa-f])",
            value,
        )
        mac = _normalize_mac(mac_match.group(0)) if mac_match else None
        sn_match = re.search(r"RLGM[-_: ]?[0-9A-Fa-f]{8}", value, re.IGNORECASE)
        sn = _normalize_sn(sn_match.group(0)) if sn_match else None
        return mac, sn
    return None, None


def _first_normalized(
    data: dict[str, Any],
    normalizer: Callable[[Any], str | None],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        normalized = normalizer(data.get(key))
        if normalized:
            return normalized
    return None


def _none_if_empty(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _none_if_null(value: Any) -> str | None:
    """Return text, treating AC placeholders (SQL NULL, N/A) as missing."""
    text = _none_if_empty(value)
    if text is None or text.upper() in {"NULL", "N/A", "NONE"}:
        return None
    return text


def _dbm(value: Any) -> float | None:
    """Parse OptTx/RxPower text leniently (exact AP format unverified)."""
    text = _none_if_null(value)
    if text is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _channel_band(channel: int | None) -> str | None:
    if channel is None or channel <= 0:
        return None
    if channel <= 14:
        return "2.4 GHz"
    return "5 GHz"


def _bandwidth_label(value: Any) -> str | None:
    return BANDWIDTH_MAP.get(_int(value))


def _stable_datetime(
    computed: datetime | None, previous: datetime | None
) -> datetime | None:
    if computed is None:
        return previous
    if previous is not None and abs(previous - computed) < LAST_BOOT_STABILITY:
        return previous
    return computed
