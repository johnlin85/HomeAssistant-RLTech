"""AC reboot button and expected-offline window (stage 6b), real HA core."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging

import pytest
from homeassistant.components.button import ButtonDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from rltech_ha_helpers import DOMAIN, make_entry

from custom_components.rltech_fttr import api
from custom_components.rltech_fttr.const import AC_REBOOT_EXPECTED_OFFLINE
from custom_components.rltech_fttr.models import RltechAcRebootRecord
from custom_components.rltech_fttr.sources import CircuitBreaker

BUTTON = "button.rltech_olt_198_51_100_29_reboot"
SWITCH = "switch.rltech_olt_198_51_100_29_web_polling"
AP_COUNT = "sensor.rltech_olt_198_51_100_29_ap_count"


async def _setup(hass: HomeAssistant):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data


def _fake_reboot(result: str = "submitted"):
    presses: list[str] = []

    async def reboot_ac(self, session, *, now=None, on_submitted=None):
        presses.append(result)
        record = RltechAcRebootRecord(requested_at=datetime.now(UTC), result=result)
        self.last_ac_reboot = record
        if on_submitted is not None:
            on_submitted()
        return record

    return presses, reboot_ac


async def _press(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": BUTTON}, blocking=True
    )
    await hass.async_block_till_done()


def _rltech_errors(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith(("custom_components.rltech_fttr", "homeassistant.helpers.update_coordinator"))
    ]


async def test_ac_reboot_button_is_a_config_restart_button(
    hass: HomeAssistant, fake_ac
) -> None:
    entry, _coordinator = await _setup(hass)
    entity = er.async_get(hass).async_get(BUTTON)
    assert entity is not None
    assert entity.unique_id == f"{entry.entry_id}_ac_reboot"
    assert entity.entity_category == EntityCategory.CONFIG
    assert entity.original_device_class == ButtonDeviceClass.RESTART
    state = hass.states.get(BUTTON)
    assert state.state != "unavailable"
    assert state.attributes["last_reboot_result"] is None


async def test_press_opens_the_expected_offline_window_quietly(
    hass: HomeAssistant, fake_ac, caplog, monkeypatch
) -> None:
    entry, coordinator = await _setup(hass)
    presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    breaker = coordinator.client.legacy_breakers.setdefault("198.51.100.29", CircuitBreaker())

    await _press(hass)
    assert presses == ["submitted"]
    assert coordinator.expected_offline()
    # Unavailable in the window: no second press.
    assert hass.states.get(BUTTON).state == "unavailable"
    button = hass.data["button"].get_entity(BUTTON)
    attrs = button.extra_state_attributes
    assert attrs["last_reboot_result"] == "submitted"
    assert attrs["expected_offline_until"] is not None
    # A direct press in the window (HA skips unavailable entities) refuses.
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    assert presses == ["submitted"]

    # The AC goes away: entities turn unavailable, no error log, no issue,
    # even once the failure is older than the unreachable-issue delay.
    caplog.clear()
    fake_ac.error = TimeoutError()
    breaker.record_failure(dt_util.utcnow())
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(AP_COUNT).state == "unavailable"
    coordinator.web_state.failing_since = dt_util.utcnow() - timedelta(hours=2)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not ir.async_get(hass).issues
    assert _rltech_errors(caplog) == []
    assert coordinator.expected_offline()

    # Back again: the window ends early, the breaker is closed again.
    fake_ac.error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.expected_offline()
    assert breaker.failures == 0 and breaker.open_until is None
    assert hass.states.get(AP_COUNT).state != "unavailable"
    state = hass.states.get(BUTTON)
    assert state.state != "unavailable"
    assert state.attributes["last_reboot_result"] == "submitted"
    assert state.attributes["expected_offline_until"] is None
    assert _rltech_errors(caplog) == []


async def test_a_success_before_the_ac_goes_down_keeps_the_window(
    hass: HomeAssistant, fake_ac, monkeypatch
) -> None:
    _entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    await _press(hass)
    # The first poll can still reach the AC before it shuts down.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.expected_offline()
    assert hass.states.get(BUTTON).state == "unavailable"


async def test_window_end_warns_once_when_the_ac_stays_away(
    hass: HomeAssistant, fake_ac, caplog, monkeypatch
) -> None:
    _entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot("submitted_unconfirmed")
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    await _press(hass)
    button = hass.data["button"].get_entity(BUTTON)
    assert button.extra_state_attributes["last_reboot_result"] == (
        "submitted_unconfirmed"
    )
    fake_ac.error = TimeoutError()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    caplog.clear()
    async_fire_time_changed(
        hass, dt_util.utcnow() + AC_REBOOT_EXPECTED_OFFLINE + timedelta(seconds=1)
    )
    await hass.async_block_till_done()
    assert not coordinator.expected_offline()
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "after the reboot request" in r.getMessage()
    ]
    assert len(warnings) == 1


async def test_failed_request_opens_no_window(
    hass: HomeAssistant, fake_ac, monkeypatch
) -> None:
    _entry, coordinator = await _setup(hass)

    async def reboot_ac(self, session, *, now=None, on_submitted=None):
        raise api.AccountBusyError("busy")

    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    with pytest.raises(HomeAssistantError):
        await _press(hass)
    assert not coordinator.expected_offline()
    assert hass.states.get(BUTTON).state != "unavailable"


async def test_button_stays_usable_while_polling_is_paused(
    hass: HomeAssistant, fake_ac
) -> None:
    _entry, coordinator = await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(AP_COUNT).state == "unavailable"
    assert hass.states.get(BUTTON).state != "unavailable"
    # But not when the AC Web UI failed outside a pause.
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    fake_ac.error = TimeoutError()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(BUTTON).state == "unavailable"


async def test_auth_failure_in_the_window_starts_no_reauth(
    hass: HomeAssistant, fake_ac, monkeypatch
) -> None:
    entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    await _press(hass)
    # A booting AC can answer the login as a credential error (review N1).
    fake_ac.error = api.AuthenticationError("invalid username/password")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert hass.states.get(AP_COUNT).state == "unavailable"
    assert not ir.async_get(hass).issues
    # Outside the window the same answer still asks for new credentials.
    fake_ac.error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.expected_offline()
    fake_ac.error = api.AuthenticationError("invalid username/password")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_window_end_reports_a_reboot_that_was_not_seen(
    hass: HomeAssistant, fake_ac, caplog, monkeypatch
) -> None:
    _entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    await _press(hass)
    # Every poll succeeds and the boot time is the old one (review N3).
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    caplog.clear()
    async_fire_time_changed(
        hass, dt_util.utcnow() + AC_REBOOT_EXPECTED_OFFLINE + timedelta(seconds=1)
    )
    await hass.async_block_till_done()
    assert coordinator.ac_reboot_observed is False
    notes = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "may not have happened" in r.getMessage()
    ]
    assert len(notes) == 1


async def test_window_end_counts_a_newer_boot_time_as_a_reboot(
    hass: HomeAssistant, fake_ac, caplog, monkeypatch
) -> None:
    from dataclasses import replace

    _entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    await _press(hass)
    # Rebooted between two polls: no failed poll, but a newer boot time.
    booted = datetime.now(UTC) + timedelta(seconds=20)
    fake_ac.data = replace(
        fake_ac.data, olt_status=replace(fake_ac.data.olt_status, last_boot=booted)
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    caplog.clear()
    async_fire_time_changed(
        hass, dt_util.utcnow() + AC_REBOOT_EXPECTED_OFFLINE + timedelta(seconds=1)
    )
    await hass.async_block_till_done()
    assert coordinator.ac_reboot_observed is True
    assert not [r for r in caplog.records if "may not have happened" in r.getMessage()]


async def test_window_end_without_any_poll_leaves_observed_unknown(
    hass: HomeAssistant, fake_ac, caplog, monkeypatch
) -> None:
    _entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    # Web polling paused for the whole window: no 8080 poll answers (N-R2-2).
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await _press(hass)
    caplog.clear()
    async_fire_time_changed(
        hass, dt_util.utcnow() + AC_REBOOT_EXPECTED_OFFLINE + timedelta(seconds=1)
    )
    await hass.async_block_till_done()
    assert coordinator.ac_reboot_observed is None
    assert not [r for r in caplog.records if "may not have happened" in r.getMessage()]


async def test_early_end_marks_the_reboot_observed(
    hass: HomeAssistant, fake_ac, monkeypatch
) -> None:
    _entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    await _press(hass)
    assert coordinator.ac_reboot_observed is None
    fake_ac.error = TimeoutError()
    await coordinator.async_refresh()
    fake_ac.error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.ac_reboot_observed is True



UPTIME = "sensor.rltech_olt_198_51_100_29_system_uptime"
LAST_BOOT = "sensor.rltech_olt_198_51_100_29_last_boot"


async def test_old_uptime_is_not_shown_after_an_ac_reboot(
    hass: HomeAssistant, fake_ac, monkeypatch
) -> None:
    from dataclasses import replace

    _entry, coordinator = await _setup(hass)
    _presses, reboot_ac = _fake_reboot()
    monkeypatch.setattr(api.RltechClient, "reboot_ac", reboot_ac)
    assert float(hass.states.get(UPTIME).state) > 300
    assert hass.states.get(LAST_BOOT).state not in ("unknown", "unavailable")

    await _press(hass)
    # Cleared at once: unknown, not the uptime from before the reboot.
    assert hass.states.get(UPTIME).state == "unknown"
    assert hass.states.get(LAST_BOOT).state == "unknown"
    assert hass.states.get(AP_COUNT).state == "2"
    assert coordinator.data.olt_status.serial_number == "RL0000000000003"
    assert coordinator.data.web_status_update is None

    # A poll still reaches the AC before it goes down and reads its status.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert float(hass.states.get(UPTIME).state) > 300
    fake_ac.error = TimeoutError()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(UPTIME).state == "unavailable"

    # Back, but the status pages were not read in this poll (not due):
    # the readings are from before the reboot, so unknown.
    fake_ac.error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.expected_offline()
    assert hass.states.get(UPTIME).state == "unknown"
    assert hass.states.get(AP_COUNT).state == "2"
    assert coordinator.data.web_status_update is None

    # The next poll reads sta-device again (due): the new uptime shows.
    now = datetime.now(UTC)
    fake_ac.data = replace(
        fake_ac.data,
        olt_status=replace(fake_ac.data.olt_status, uptime_seconds=120),
        web_status_update=now,
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert abs(float(hass.states.get(UPTIME).state) - 120 / 3600) < 1e-6
