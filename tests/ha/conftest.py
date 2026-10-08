"""Home Assistant runtime test fixtures (tests/ha/).

Only collected when pytest-homeassistant-custom-component is installed
(tests/conftest.py ignores this directory otherwise). The AC is never
contacted: ``RltechClient.fetch_snapshot`` is replaced by a scripted fake.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from rltech_ha_helpers import FakeAc

from custom_components.rltech_fttr import api


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/rltech_fttr from the repository."""
    return


@pytest.fixture
def fake_ac():
    """Replace the AC with scripted snapshots."""
    fake = FakeAc()

    async def fetch_snapshot(self, session, **kwargs):
        return await fake.fetch_snapshot(self, session, **kwargs)

    with patch.object(api.RltechClient, "fetch_snapshot", fetch_snapshot):
        yield fake


@pytest.fixture(autouse=True)
def no_mqtt_connections():
    """Never open MQTT sockets; tests assert on the manager instead."""
    with patch(
        "custom_components.rltech_fttr.mqtt.RltechMqttManager.start"
    ) as start:
        yield start


@pytest.fixture(autouse=True)
def endpoint_probe():
    """TCP probes of 80/8883: nothing reachable unless a test says so."""

    async def probe(targets, timeout=2.0):
        return {target: target in probe.reachable for target in targets}

    probe.reachable = set()
    with patch("custom_components.rltech_fttr.coordinator.async_tcp_probe", probe):
        yield probe
