"""Tests for probe.py (identity session rules, TCP probe) and Locked mapping."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from test_settings import load_module

for _name in ("models", "sources", "settings", "api", "mqtt"):
    load_module(_name)
api = load_module("api")
probe = load_module("probe")

FIXTURES = Path(__file__).parent / "fixtures"
LOGIN_OK = json.dumps({"Logged": "0", "Privilege": "1", "Active": "1", "ecntToken": "t"})


class Response:
    def __init__(self, status=200, body="", error=None):
        self.status, self.body, self.error, self.headers = status, body, error, {}

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return None

    async def text(self, **_kwargs):
        return self.body

    async def read(self):
        return self.body.encode()


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def _next(self, method, url, kwargs):
        self.calls.append(url.rsplit("/", 1)[1])
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def read(responses):
    client = api.RltechClient("http://198.51.100.29:8080", "u", "p")
    session = Session(responses)
    identity = asyncio.run(probe.async_read_ac_identity(session, client))
    return identity, session.calls, client


def test_identity_from_real_pages() -> None:
    identity, calls, client = read(
        [
            Response(200, LOGIN_OK),
            Response(200, fixture("real_sta_device.html")),
            Response(200, fixture("real_sta_user.html")),
            Response(200, ""),
        ]
    )
    assert calls == ["check_auth.json", "sta-device.asp", "sta-user.asp", "logout.cgi"]
    assert identity.lan_ip == "192.0.2.1"
    assert identity.lan_mac == "E0:21:FE:C0:C0:00"
    assert identity.serial_number == "RL0000000000003"
    assert identity.device_identifier == "E021FE-RL0000000000003"
    assert identity.software_version == "V0.0.30"
    assert client.token is None


def test_device_page_timeout_stops_and_logs_out() -> None:
    identity, calls, _ = read(
        [Response(200, LOGIN_OK), Response(error=TimeoutError()), Response(200, "")]
    )
    assert calls == ["check_auth.json", "sta-device.asp", "logout.cgi"]
    assert identity == probe.AcIdentity()


def test_user_page_session_expired_stops_and_logs_out() -> None:
    identity, calls, _ = read(
        [
            Response(200, LOGIN_OK),
            Response(200, fixture("real_sta_device.html")),
            Response(302, ""),
            Response(200, ""),
        ]
    )
    assert calls == ["check_auth.json", "sta-device.asp", "sta-user.asp", "logout.cgi"]
    assert identity.serial_number == "RL0000000000003"
    assert identity.lan_mac is None


def test_busy_and_locked_raise_without_logout() -> None:
    for body, error in (
        ({"Logged": "1"}, api.AccountBusyError),
        ({"Locked": "1"}, api.AccountLockedError),
    ):
        client = api.RltechClient("http://198.51.100.29:8080", "u", "p")
        session = Session([Response(200, json.dumps(body))])
        try:
            asyncio.run(probe.async_read_ac_identity(session, client))
        except error:
            pass
        else:  # pragma: no cover
            raise AssertionError(error)
        assert session.calls == ["check_auth.json"]
    # Locked is still an AuthenticationError for old callers, but its own kind.
    assert issubclass(api.AccountLockedError, api.AuthenticationError)
    assert api.error_kind(api.AccountLockedError("x")) == "locked"
    assert api.error_kind(api.AuthenticationError("x")) == "auth_failed"


def test_probe_targets() -> None:
    assert probe.probe_targets("198.51.100.29", "192.0.2.1") == [
        ("198.51.100.29", 80),
        ("192.0.2.1", 80),
        ("198.51.100.29", 8883),
        ("192.0.2.1", 8883),
    ]
    assert probe.probe_targets("192.0.2.1", "192.0.2.1") == [
        ("192.0.2.1", 80),
        ("192.0.2.1", 8883),
    ]
    assert probe.probe_targets("198.51.100.29", None) == [
        ("198.51.100.29", 80),
        ("198.51.100.29", 8883),
    ]


def test_tcp_probe_reports_unreachable(monkeypatch) -> None:
    async def fake_open(host, port):
        if port == 80:
            raise OSError("refused")

        class Writer:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        return object(), Writer()

    monkeypatch.setattr(probe.asyncio, "open_connection", fake_open)
    result = asyncio.run(probe.async_tcp_probe([("h", 80), ("h", 8883), ("h", 80)]))
    assert result == {("h", 80): False, ("h", 8883): True}


def test_client_exclusive_holds_the_session_lock() -> None:
    async def run() -> None:
        client = api.RltechClient("http://198.51.100.29:8080", "u", "p")
        async with client.exclusive():
            assert client._lock.locked()
        assert not client._lock.locked()

    asyncio.run(run())
