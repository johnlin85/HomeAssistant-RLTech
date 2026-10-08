"""Runtime smoke tests for stage 1-3 behaviour (coordinator, switch, devices)."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from rltech_ha_helpers import AP_A_SN, AP_B_SN, DOMAIN, make_entry, real_snapshot

from custom_components.rltech_fttr import async_remove_config_entry_device
from custom_components.rltech_fttr.api import AccountBusyError
from custom_components.rltech_fttr.sources import CircuitBreaker

AP_COUNT = "sensor.rltech_olt_198_51_100_29_ap_count"
ASSOC = f"sensor.rltech_ap_{AP_B_SN.lower()}_assoc_count"
SWITCH = "switch.rltech_olt_198_51_100_29_web_polling"


async def _setup(hass: HomeAssistant, **kwargs):
    entry = make_entry(**kwargs)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, entry.runtime_data


async def test_busy_grace_then_unavailable_then_recovery(
    hass: HomeAssistant, fake_ac
) -> None:
    _entry, coordinator = await _setup(hass)
    fake_ac.error = AccountBusyError("account is already logged into the Web UI")

    states = []
    for _ in range(4):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        states.append(hass.states.get(AP_COUNT).state)

    # Grace of 3 polls keeps the last data, the 4th makes entities unavailable.
    assert states == ["2", "2", "2", "unavailable"]
    assert hass.states.get(ASSOC).state == "unavailable"
    assert coordinator.web_state.busy_polls == 4

    fake_ac.error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(AP_COUNT).state == "2"
    assert coordinator.web_state.state == "ok"


async def test_pause_switch_stops_and_resumes_polling(
    hass: HomeAssistant, fake_ac
) -> None:
    _entry, coordinator = await _setup(hass)
    assert hass.states.get(SWITCH).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(SWITCH).attributes["paused_until"] is not None
    calls = fake_ac.calls
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert fake_ac.calls == calls  # no login while paused

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "on"
    assert fake_ac.calls > calls


async def test_devices_are_registered_and_renames_follow(
    hass: HomeAssistant, fake_ac
) -> None:
    entry, coordinator = await _setup(hass)
    registry = dr.async_get(hass)

    controller = registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    ap_b = registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_ap_{AP_B_SN}"), entry.entry_id
    )
    assert controller is not None and ap_b is not None
    assert ap_b.via_device_id == controller.id
    assert ap_b.name == "RLTech AP 820"
    assert ap_b.serial_number == AP_B_SN
    assert ("mac", "e0:21:fe:b0:b0:00") in ap_b.connections
    assert controller.serial_number == "RL0000000000003"

    # Alias changed on the AC: the device name follows, entity_id does not.
    aps = {
        mac: replace(ap, alias="Kitchen") if ap.sn == AP_B_SN else ap
        for mac, ap in fake_ac.data.aps.items()
    }
    fake_ac.data = replace(fake_ac.data, aps=aps)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    ap_b = registry.async_get(ap_b.id)
    assert ap_b.name == "RLTech AP Kitchen"
    assert hass.states.get(ASSOC) is not None


async def test_remove_device_only_when_the_ap_is_gone(
    hass: HomeAssistant, fake_ac
) -> None:
    entry, coordinator = await _setup(hass)
    registry = dr.async_get(hass)
    ap_a = registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_ap_{AP_A_SN}"), entry.entry_id
    )
    controller = registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )

    assert not await async_remove_config_entry_device(hass, entry, ap_a)
    assert not await async_remove_config_entry_device(hass, entry, controller)

    # The AC no longer lists AP-A.
    fake_ac.data = replace(
        fake_ac.data,
        aps={mac: ap for mac, ap in fake_ac.data.aps.items() if ap.sn != AP_A_SN},
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert await async_remove_config_entry_device(hass, entry, ap_a)

    registry.async_remove_device(ap_a.id)
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    assert not [
        e
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if AP_A_SN in e.unique_id
    ]

    # The AP comes back: device and entities are recreated.
    fake_ac.data = real_snapshot()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_ap_{AP_A_SN}"), entry.entry_id
    )
    assert hass.states.get(f"sensor.rltech_ap_{AP_A_SN.lower()}_assoc_count")


async def test_f1_removes_old_port80_only_entities(
    hass: HomeAssistant, fake_ac
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    old = {
        "sensor.rltech_olt_198_51_100_29_cpu_usage": f"{entry.entry_id}_olt_cpu_usage",
        "sensor.rltech_olt_198_51_100_29_memory_usage": f"{entry.entry_id}_olt_memory_usage",
        f"sensor.rltech_ap_{AP_B_SN.lower()}_source_host": (
            f"{entry.entry_id}_ap_{AP_B_SN}_source_host"
        ),
    }
    for entity_id, unique_id in old.items():
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            suggested_object_id=entity_id.split(".", 1)[1],
            config_entry=entry,
        )
    keep = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_olt_cpu_temperature",
        suggested_object_id="rltech_olt_198_51_100_29_cpu_temperature",
        config_entry=entry,
    )

    def trip_breaker(client) -> None:
        client.legacy_breakers["198.51.100.29"] = CircuitBreaker(trips=1)

    fake_ac.hook = trip_breaker
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Not before the endpoint probe confirmed a WAN deployment (S5).
    for entity_id in old:
        assert ent_reg.async_get(entity_id) is not None, entity_id
    coordinator = entry.runtime_data
    await coordinator.async_resolve_endpoints()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    for entity_id in old:
        assert ent_reg.async_get(entity_id) is None, entity_id
    assert ent_reg.async_get(keep.entity_id) is not None


async def test_resolver_starts_mqtt_on_the_lan_address(
    hass: HomeAssistant, fake_ac, endpoint_probe, no_mqtt_connections
) -> None:
    endpoint_probe.reachable = {("192.0.2.1", 8883)}
    entry, coordinator = await _setup(hass, mqtt=True)
    await coordinator.async_resolve_endpoints()
    await hass.async_block_till_done()

    # LAN IP from sta-user (sanitized 192.0.2.1) wins over the WAN host.
    assert coordinator.mqtt_host_in_use == "192.0.2.1"
    assert coordinator._mqtt_manager.host == "192.0.2.1"
    assert no_mqtt_connections.called
    assert coordinator.endpoint_probe.reachable["192.0.2.1:8883"] is True


async def test_resolver_on_wan_stays_quiet(
    hass: HomeAssistant, fake_ac, endpoint_probe, no_mqtt_connections, caplog
) -> None:
    entry, coordinator = await _setup(hass, mqtt=True)
    await coordinator.async_resolve_endpoints()
    await hass.async_block_till_done()

    assert coordinator._mqtt_manager is None
    assert coordinator.mqtt_stats.last_error_kind == "unreachable"
    assert not no_mqtt_connections.called
    assert not [
        r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}
        and "rltech" in r.name
    ]


async def test_pause_survives_a_reload_without_logging_in(
    hass: HomeAssistant, fake_ac
) -> None:
    entry, coordinator = await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    paused_until = coordinator.web_polling_paused_until
    calls = fake_ac.calls

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    assert fake_ac.calls == calls  # the reload did not log in to the AC
    assert coordinator.web_polling_paused_until == paused_until
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(AP_COUNT).state == "unavailable"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake_ac.calls == calls + 1
    assert hass.states.get(AP_COUNT).state == "2"
    assert hass.states.get(ASSOC).state == "4"
    assert coordinator.device_manager.paused_until is None


async def test_failing_sync_step_does_not_block_entities(
    hass: HomeAssistant, fake_ac, caplog
) -> None:
    from unittest.mock import patch

    with patch(
        "custom_components.rltech_fttr.devices.RltechDeviceManager._sync_controller",
        side_effect=RuntimeError("boom"),
    ):
        _entry, coordinator = await _setup(hass)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(ASSOC).state == "4"
    warnings = [
        r for r in caplog.records if "device sync step controller" in r.getMessage()
    ]
    assert [r.levelname for r in warnings] == ["WARNING", "DEBUG"][: len(warnings)]
    assert warnings and warnings[0].levelname == "WARNING"


async def test_diagnostics_hide_addresses_macs_and_secrets(
    hass: HomeAssistant, fake_ac, endpoint_probe
) -> None:
    import json

    from custom_components.rltech_fttr.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry, coordinator = await _setup(hass, mqtt=True)
    await coordinator.async_resolve_endpoints()
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(result, default=str)

    for secret in (
        "198.51.100.29",
        "192.0.2.1",
        "E0:21:FE",
        "e0:21:fe",
        "0011223344556677",
        "fake-password",
        "fake-mqtt-password",
        "RL0000000000003",
    ):
        assert secret not in text, secret
    summary = result["entry"]
    assert (summary["version"], summary["minor_version"]) == (1, 2)
    assert summary["unique_id_kind"] == "mac"
    assert summary["mqtt_host_mode"] == "auto"
    assert "object_id_host" in summary["data_keys"]
    assert set(summary["endpoint_probe"]["reachable"]) >= {"host:80", "lan_ip:8883"}


async def test_auth_failure_starts_reauth_but_lock_does_not(
    hass: HomeAssistant, fake_ac
) -> None:
    from custom_components.rltech_fttr.api import (
        AccountLockedError,
        AuthenticationError,
    )

    entry, coordinator = await _setup(hass)

    fake_ac.error = AccountLockedError("Web login is locked")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert coordinator.web_state.last_error_kind == "locked"
    assert hass.states.get(AP_COUNT).state == "unavailable"

    fake_ac.error = AuthenticationError("invalid username/password")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    # Translated like every other user-facing error (exception-translations).
    err = coordinator.last_exception
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "web_ui_auth_failed"
    assert err.translation_placeholders == {"host": "198.51.100.29"}


async def test_busy_at_startup_retries_setup(hass: HomeAssistant, fake_ac) -> None:
    from homeassistant.config_entries import ConfigEntryState

    fake_ac.error = AccountBusyError("account is already logged into the Web UI")
    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_stop_event_logs_out_under_the_session_lock(
    hass: HomeAssistant, fake_ac
) -> None:
    from unittest.mock import AsyncMock, patch

    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    _entry, coordinator = await _setup(hass)
    with patch.object(coordinator.client, "async_close", AsyncMock()) as close:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    close.assert_awaited_once()


async def test_pause_shows_unavailable_and_resume_fetches_all_details(
    hass: HomeAssistant, fake_ac
) -> None:
    _entry, coordinator = await _setup(hass)
    assert fake_ac.kwargs["force_all_details"] is False

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    # Frozen data is not presented as current (review S1).
    assert hass.states.get(AP_COUNT).state == "unavailable"
    assert hass.states.get(ASSOC).state == "unavailable"
    assert hass.states.get(SWITCH).state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake_ac.kwargs["force_all_details"] is True
    assert hass.states.get(AP_COUNT).state == "2"

    await coordinator.async_refresh()
    assert fake_ac.kwargs["force_all_details"] is False


async def test_port80_after_grace_keeps_the_mqtt_overlay(
    hass: HomeAssistant, fake_ac
) -> None:
    from datetime import UTC, datetime, timedelta

    from custom_components.rltech_fttr.models import RltechStation

    _entry, coordinator = await _setup(hass)
    fake_ac.error = AccountBusyError("account is already logged into the Web UI")
    for _ in range(3):
        await coordinator.async_refresh()
    assert coordinator.web_state.busy_polls == 3

    # Grace is over; port 80 still refreshes, and MQTT reports a new client
    # while this poll runs (review S4).
    fake_ac.port80_answers = True
    now = datetime.now(UTC)
    mqtt_station = RltechStation(
        mac="02:00:00:00:00:99", reported_online=True, home=True, last_seen=now
    )

    def mqtt_update_during_poll(_client) -> None:
        current = coordinator._last_data
        coordinator._last_data = replace(
            current, stations={**current.stations, mqtt_station.mac: mqtt_station}
        )

    stale = replace(
        fake_ac.data.stations["02:00:00:00:00:01"],
        mac="02:00:00:00:00:98",
        last_seen=now - timedelta(days=2),
        reported_online=False,
        home=False,
    )
    fake_ac.data = replace(
        fake_ac.data, stations={**fake_ac.data.stations, stale.mac: stale}
    )
    fake_ac.hook = mqtt_update_during_poll
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(AP_COUNT).state == "unavailable"
    kept = coordinator._last_data
    assert mqtt_station.mac in kept.stations  # MQTT overlay preserved
    assert stale.mac not in kept.stations  # client ageing ran
