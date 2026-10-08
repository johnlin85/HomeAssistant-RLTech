"""AP reboot button through the AC (stage 6) against a real Home Assistant."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from rltech_ha_helpers import AP_B_SN, DOMAIN, fixture_text, make_entry

from custom_components.rltech_fttr import api
from custom_components.rltech_fttr.models import RltechApRebootRecord

BUTTON = f"button.rltech_ap_{AP_B_SN.lower()}_reboot"
SWITCH = "switch.rltech_olt_198_51_100_29_web_polling"
AP_B_MAC = "E0:21:FE:B0:B0:00"


async def _setup(hass: HomeAssistant):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data


def _set_ap_b_online(fake_ac, online: bool) -> None:
    ap = fake_ac.data.aps[AP_B_MAC]
    fake_ac.data = replace(
        fake_ac.data, aps={**fake_ac.data.aps, AP_B_MAC: replace(ap, online=online)}
    )


async def test_button_follows_ap_online_and_stays_usable_while_paused(
    hass: HomeAssistant, fake_ac
) -> None:
    _entry, coordinator = await _setup(hass)
    assert hass.states.get(BUTTON).state != "unavailable"
    assert "ap_username" not in str(hass.states.get(BUTTON).attributes)

    _set_ap_b_online(fake_ac, False)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(BUTTON).state == "unavailable"

    _set_ap_b_online(fake_ac, True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(BUTTON).state != "unavailable"

    # Pause: sensors go unavailable (review S1) but the reboot button stays
    # usable on the last known state (review S3).
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.rltech_olt_198_51_100_29_ap_count").state == (
        "unavailable"
    )
    assert hass.states.get(BUTTON).state != "unavailable"


async def test_press_reboots_through_the_ac_and_shows_status(
    hass: HomeAssistant, fake_ac
) -> None:
    _entry, coordinator = await _setup(hass)
    pressed: list[str] = []

    async def reboot(self, session, ap, *, now=None):
        pressed.append(ap.mac)
        record = RltechApRebootRecord(
            mac=ap.mac,
            requested_at=datetime.now(UTC),
            result="submitted",
            reboot_status="0",
        )
        self.reboot_records[ap.mac] = record
        self.last_reboot = record
        return record

    with patch.object(api.RltechClient, "reboot_ap_via_ac", reboot):
        await hass.services.async_call(
            "button", "press", {"entity_id": BUTTON}, blocking=True
        )
    assert pressed == [AP_B_MAC]
    attrs = hass.states.get(BUTTON).attributes
    assert attrs["last_reboot_result"] == "submitted"
    assert attrs["reboot_status"] == "0"
    assert attrs["reboot_state"] == "queued"

    # The next poll reads the task list: RebootStatus 3 (AP rebooting).
    fake_ac.data = replace(
        fake_ac.data,
        ap_tasks=api.parse_ap_task_list(
            fixture_text("real_ap_online_list_rebooting.html")
        ),
        last_success_8080=datetime.now(UTC),
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    attrs = hass.states.get(BUTTON).attributes
    assert attrs["reboot_status"] == "3"
    assert attrs["reboot_state"] == "rebooting"

    diagnostics = await _diagnostics(hass, coordinator)
    assert diagnostics["ap_reboot"]["result"] == "submitted"
    assert diagnostics["ap_reboot"]["reboot_status"] == "3"
    assert AP_B_MAC not in str(diagnostics["ap_reboot"])


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (api.AccountBusyError("busy"), "web_ui_busy"),
        (api.ApRebootRejected("offline"), "ap_reboot_offline"),
        (api.ApRebootRejected("task_pending"), "ap_reboot_task_pending"),
        (api.ApRebootRejected("slow"), "ap_reboot_slow"),
        (TimeoutError(), "web_ui_unreachable"),
        (api.UnexpectedResponse("x"), "ap_reboot_failed"),
    ],
)
async def test_press_errors_are_translated(
    hass: HomeAssistant, fake_ac, error, key
) -> None:
    await _setup(hass)

    async def reboot(self, session, ap, *, now=None):
        raise error

    with (
        patch.object(api.RltechClient, "reboot_ap_via_ac", reboot),
        pytest.raises(HomeAssistantError) as info,
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": BUTTON}, blocking=True
        )
    assert info.value.translation_domain == DOMAIN
    assert info.value.translation_key == key
    # The message comes from strings.json (no raw exception text).
    assert str(info.value)


async def _diagnostics(hass: HomeAssistant, coordinator):
    from custom_components.rltech_fttr.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    return await async_get_config_entry_diagnostics(hass, coordinator.config_entry)
