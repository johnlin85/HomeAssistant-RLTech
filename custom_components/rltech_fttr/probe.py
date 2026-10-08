"""Read the AC identity in one short session, and probe optional ports.

Pure asyncio (no Home Assistant imports), used by the config flow and by the
coordinator's endpoint resolver (plan 3.1).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

from .api import (
    AP_DETAIL_REQUEST_TIMEOUT,
    RltechClient,
    SessionExpired,
    parse_ac_lan_identity,
    parse_olt_status,
)
from .mqtt import AsyncPskMqttClient, mqtt_error_kind

_LOGGER = logging.getLogger(__name__)

LEGACY_PORT = 80
MQTT_PORT = 8883


@dataclass(frozen=True)
class AcIdentity:
    """What sta-device.asp / sta-user.asp say about the AC itself."""

    lan_ip: str | None = None
    lan_mac: str | None = None
    serial_number: str | None = None
    device_identifier: str | None = None
    software_version: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    """Identity plus TCP reachability of the optional ports."""

    identity: AcIdentity = field(default_factory=AcIdentity)
    ports: dict[tuple[str, int], bool] = field(default_factory=dict)


async def async_read_ac_identity(session: Any, client: RltechClient) -> AcIdentity:
    """Log in, read sta-device and sta-user, log out.

    Same session rules as the polling burst: every request is capped at
    min(timeout, 8 s); a timeout or an expired session stops at once and only
    the logout follows (stage 2 M1). Page parse failures leave fields None;
    only login errors (auth, locked, busy, network) are raised.
    """
    client.token = None
    await client.login(session)
    status = None
    lan_ip = lan_mac = None
    timeout = min(client.timeout, AP_DETAIL_REQUEST_TIMEOUT)
    try:
        for page in ("device", "user"):
            try:
                if page == "device":
                    status = parse_olt_status(
                        await client.fetch_device_html(session, timeout=timeout)
                    )
                else:
                    lan_ip, lan_mac = parse_ac_lan_identity(
                        await client.fetch_user_html(session, timeout=timeout)
                    )
            except (TimeoutError, SessionExpired) as err:
                _LOGGER.debug("RLTech identity read stopped at %s page: %r", page, err)
                break
            except Exception as err:  # noqa: BLE001 - identity is best effort
                _LOGGER.debug(
                    "Unable to read RLTech %s page: %s", page, type(err).__name__
                )
    finally:
        try:
            await asyncio.shield(client.logout(session))
        except Exception as err:  # noqa: BLE001 - token already dropped
            _LOGGER.debug("RLTech identity logout failed: %s", type(err).__name__)
    return AcIdentity(
        lan_ip=lan_ip,
        lan_mac=lan_mac,
        serial_number=status.serial_number if status else None,
        device_identifier=status.device_identifier if status else None,
        software_version=status.software_version if status else None,
    )


def probe_targets(host: str, lan_ip: str | None) -> list[tuple[str, int]]:
    """Return (host, port) pairs to probe: 80 and 8883 on host and LAN IP."""
    hosts = [host]
    if lan_ip and lan_ip != host:
        hosts.append(lan_ip)
    return [(item, port) for port in (LEGACY_PORT, MQTT_PORT) for item in hosts]


async def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
    except Exception as err:  # noqa: BLE001 - unreachable is a normal result
        _LOGGER.debug("RLTech probe %s:%s unreachable: %s", host, port, err)
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    return True


async def async_tcp_probe(
    targets: Iterable[tuple[str, int]], timeout: float = 2.0
) -> dict[tuple[str, int], bool]:
    """Return TCP reachability for each target (concurrent, short timeout)."""
    items = list(dict.fromkeys(targets))
    results = await asyncio.gather(
        *(_tcp_reachable(host, port, timeout) for host, port in items)
    )
    return dict(zip(items, results, strict=True))


async def async_mqtt_handshake(
    host: str,
    port: int,
    username: str,
    password: str,
    psk_identity: str,
    psk_hex: str,
    *,
    timeout: float = 5.0,
) -> str | None:
    """Try TLS-PSK + MQTT CONNECT once; return an error kind or None."""
    client = AsyncPskMqttClient(
        host,
        port,
        username,
        password,
        psk_identity=psk_identity,
        psk_hex=psk_hex,
        client_id="ha-rltech-fttr-probe",
    )
    try:
        await asyncio.wait_for(client.connect(), timeout)
    except Exception as err:  # noqa: BLE001 - classified for the form
        return mqtt_error_kind(err)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return None
