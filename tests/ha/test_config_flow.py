"""Config flow, reauth and reconfigure against a real Home Assistant."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from rltech_ha_helpers import DOMAIN, HOST, make_entry

from custom_components.rltech_fttr.api import (
    AccountBusyError,
    AccountLockedError,
    AuthenticationError,
    UnexpectedResponse,
)
from custom_components.rltech_fttr.probe import AcIdentity

FLOW = "custom_components.rltech_fttr.config_flow"
LAN_IP = "192.0.2.1"
MAC = "E0:21:FE:C0:C0:00"
UID = "e0:21:fe:c0:c0:00"
IDENTITY = AcIdentity(
    lan_ip=LAN_IP, lan_mac=MAC, serial_number="RL0000000000003", software_version="V"
)
LOGIN = {"base_url": HOST, "username": "useradmin", "password": "secret"}
WAN_ONLY = {(HOST, 80): False, (LAN_IP, 80): False, (HOST, 8883): False, (LAN_IP, 8883): False}


@pytest.fixture
def identity():
    with patch(f"{FLOW}.async_read_ac_identity", AsyncMock(return_value=IDENTITY)) as m:
        yield m


@pytest.fixture
def ports():
    with patch(f"{FLOW}.async_tcp_probe", AsyncMock(return_value=dict(WAN_ONLY))) as m:
        yield m


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.rltech_fttr.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_wan_creates_entry_with_three_fields(
    hass: HomeAssistant, identity, ports
) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert set(result["data_schema"].schema) == {"base_url", "username", "password"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**LOGIN, "base_url": f"http://{HOST}:8080/"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == f"RLTech FTTR {HOST}"
    assert entry.unique_id == UID
    assert (entry.version, entry.minor_version) == (1, 2)
    assert dict(entry.data) == {
        "base_url": f"http://{HOST}:8080",
        "username": "useradmin",
        "password": "secret",
        "object_id_host": HOST,
    }
    assert entry.options["enable_mqtt"] is False
    assert entry.options["mqtt_host"] == ""
    assert entry.options["scan_interval"] == 60
    ports.assert_awaited_once()
    assert ports.await_args.args[0] == [
        (HOST, 80),
        (LAN_IP, 80),
        (HOST, 8883),
        (LAN_IP, 8883),
    ]


async def test_user_without_identity_still_creates_entry(
    hass: HomeAssistant, ports
) -> None:
    with patch(f"{FLOW}.async_read_ac_identity", AsyncMock(return_value=AcIdentity())):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], LOGIN
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id is None


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (AuthenticationError("bad"), "invalid_auth"),
        (AccountLockedError("locked"), "account_locked"),
        (AccountBusyError("busy"), "account_busy"),
        (aiohttp.ClientConnectionError("down"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (UnexpectedResponse("html"), "not_rltech"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_errors(hass: HomeAssistant, ports, error, key) -> None:
    with patch(f"{FLOW}.async_read_ac_identity", AsyncMock(side_effect=error)):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], LOGIN
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}


async def test_user_invalid_host(hass: HomeAssistant, identity, ports) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**LOGIN, "base_url": f"{HOST}:8081"}
    )
    assert result["errors"] == {"base_url": "invalid_host"}
    identity.assert_not_awaited()


async def test_user_aborts_for_same_address(hass: HomeAssistant, identity, ports) -> None:
    make_entry().add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    identity.assert_not_awaited()


async def test_user_aborts_for_same_ac_on_the_lan_address(
    hass: HomeAssistant, identity, ports
) -> None:
    make_entry(unique_id=UID).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**LOGIN, "base_url": LAN_IP}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_mqtt_step_when_8883_answers(hass: HomeAssistant, identity) -> None:
    lan = {**WAN_ONLY, (LAN_IP, 8883): True}
    with (
        patch(f"{FLOW}.async_tcp_probe", AsyncMock(return_value=lan)),
        patch(f"{FLOW}.async_mqtt_handshake", AsyncMock(return_value=None)) as shake,
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], LOGIN
        )
        assert result["step_id"] == "mqtt"
        assert result["description_placeholders"]["lan_ip"] == LAN_IP

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enable_mqtt": True}
        )
        assert result["errors"] == {"base": "mqtt_credentials_required"}
        shake.assert_not_awaited()

        shake.return_value = "tls_handshake"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enable_mqtt": True, "mqtt_psk": "wrong"}
        )
        assert result["errors"] == {"base": "mqtt_tls_failed"}

        shake.return_value = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enable_mqtt": True, "mqtt_psk": "text-psk!"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["result"].options
    assert options["enable_mqtt"] is True
    assert options["mqtt_host"] == ""
    assert options["mqtt_psk"] == "746578742d70736b21"
    # Automatic host: the handshake went to the detected LAN address.
    assert shake.await_args.args[0] == LAN_IP


async def test_mqtt_step_can_be_skipped(hass: HomeAssistant, identity) -> None:
    lan = {**WAN_ONLY, (LAN_IP, 8883): True}
    with patch(f"{FLOW}.async_tcp_probe", AsyncMock(return_value=lan)):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], LOGIN
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enable_mqtt": False}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["enable_mqtt"] is False


async def test_reauth_success_updates_password(hass: HomeAssistant, identity) -> None:
    entry = make_entry(unique_id=UID, minor_version=2)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "administrator", "password": "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
    assert entry.data["username"] == "administrator"


async def test_reauth_invalid_auth_and_wrong_device(hass: HomeAssistant) -> None:
    entry = make_entry(unique_id=UID, minor_version=2)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(
        f"{FLOW}.async_read_ac_identity", AsyncMock(side_effect=AuthenticationError())
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["errors"] == {"base": "invalid_auth"}
    other = AcIdentity(lan_mac="E0:21:FE:99:99:00")
    with patch(f"{FLOW}.async_read_ac_identity", AsyncMock(return_value=other)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data["password"] == "fake-password"


async def _reconfigure(hass, entry, user_input):
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_reconfigure_moves_to_lan_keeping_ids(hass: HomeAssistant, identity) -> None:
    entry = make_entry(
        unique_id=UID,
        minor_version=2,
        data={
            "base_url": f"http://{HOST}:8080",
            "username": "useradmin",
            "password": "fake-password",
            "object_id_host": HOST,
        },
    )
    entry.add_to_hass(hass)
    result = await _reconfigure(
        hass, entry, {"base_url": LAN_IP, "username": "useradmin"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["base_url"] == f"http://{LAN_IP}:8080"
    assert entry.data["password"] == "fake-password"  # blank keeps it
    assert entry.data["object_id_host"] == HOST  # entity_ids stay
    assert entry.unique_id == UID
    assert entry.title == f"RLTech FTTR {LAN_IP}"


async def test_reconfigure_wrong_device_and_cannot_identify(hass: HomeAssistant) -> None:
    entry = make_entry(unique_id=UID, minor_version=2)
    entry.add_to_hass(hass)
    with patch(f"{FLOW}.async_read_ac_identity", AsyncMock(return_value=AcIdentity())):
        result = await _reconfigure(hass, entry, {"base_url": LAN_IP, "username": "u"})
    assert result["errors"] == {"base": "cannot_identify"}
    other = AcIdentity(lan_mac="E0:21:FE:99:99:00")
    with patch(f"{FLOW}.async_read_ac_identity", AsyncMock(return_value=other)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"base_url": LAN_IP, "username": "u"}
        )
    assert result["reason"] == "wrong_device"
    assert entry.data["base_url"] == f"http://{HOST}:8080"


async def test_reconfigure_upgrades_legacy_unique_id(
    hass: HomeAssistant, identity
) -> None:
    entry = make_entry(minor_version=2, title="My AC")
    entry.add_to_hass(hass)
    result = await _reconfigure(hass, entry, {"base_url": HOST, "username": "useradmin"})
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == UID
    assert entry.title == "My AC"  # user-edited title untouched


async def test_reconfigure_host_in_use(hass: HomeAssistant, identity) -> None:
    make_entry(
        data={"base_url": f"http://{LAN_IP}:8080", "username": "u", "password": "p"},
        unique_id="other",
    ).add_to_hass(hass)
    entry = make_entry(unique_id=UID, minor_version=2)
    entry.add_to_hass(hass)
    result = await _reconfigure(hass, entry, {"base_url": LAN_IP, "username": "u"})
    assert result["errors"] == {"base_url": "host_in_use"}
    identity.assert_not_awaited()
