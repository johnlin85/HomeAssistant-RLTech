"""Coordinator for RLTech FTTR."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AccountLockedError,
    AuthenticationError,
    RltechClient,
    clear_status_readings,
    error_kind,
)
from .const import (
    AC_REBOOT_EXPECTED_OFFLINE,
    CONF_BASE_URL,
    CONF_ENABLE_AP_POLLING,
    CONF_ENABLE_MQTT,
    CONF_LEGACY_HOSTS,
    CONF_LEGACY_PASSWORD,
    CONF_LEGACY_USERNAME,
    CONF_MQTT_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_PSK,
    CONF_MQTT_PSK_IDENTITY,
    CONF_MQTT_USERNAME,
    CONF_SCAN_INTERVAL,
    CONF_ENABLE_STATION_POLLING,
    DEFAULT_AC_STATUS_INTERVAL,
    DEFAULT_AP_DETAIL_INTERVAL,
    DEFAULT_ENABLE_HARDWARE_STATUS,
    DEFAULT_ENABLE_AP_POLLING,
    DEFAULT_DHCP_HOSTNAME_REFRESH_INTERVAL,
    DEFAULT_ENABLE_MQTT,
    DEFAULT_MQTT_PASSWORD,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_PSK_IDENTITY,
    DEFAULT_MQTT_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_ENABLE_STATION_POLLING,
    DEFAULT_STATION_RETENTION,
    DEFAULT_STATION_STALE_AFTER,
    DOMAIN,
    SIGNAL_STATIONS_CHANGED,
    CONF_STATION_RETENTION,
    CONF_STATION_STALE_AFTER,
    CONF_WEB_BUSY_GRACE_POLLS,
    CONF_AC_UTC_OFFSET,
    CONF_MQTT_USE_FACTORY_DEFAULTS,
    FACTORY_MQTT_PSK,
    WEB_BURST_BUDGET,
    WEB_POLLING_PAUSE,
)
from .issues import async_sync_web_issue
from .hostname_enrichment import enrich_from_home_assistant_dhcp
from .models import RltechData
from .mqtt import (
    MqttApHealthUpdate,
    MqttApStatusUpdate,
    MqttStationUpdate,
    RltechMqttManager,
    RltechMqttStats,
    merge_ap_health_update,
    merge_ap_status_update,
    merge_station_updates,
    preserve_live_overlay,
)
from .oui_enrichment import enrich_station_vendors
from .probe import LEGACY_PORT, MQTT_PORT, async_tcp_probe
from .settings import (
    ac_timezone,
    entry_host,
    entry_value,
    hardware_status_enabled,
)
from .sources import (
    DEFAULT_WEB_BUSY_GRACE_POLLS,
    ISSUE_WEB_UI_BUSY,
    SOURCE_LEGACY,
    SOURCE_MQTT,
    SOURCE_WEB,
    STATE_AUTH_FAILED,
    STATE_BUSY,
    STATE_DISABLED,
    STATE_OK,
    STATE_PAUSED,
    STATE_NOT_CONFIGURED,
    STATE_UNKNOWN,
    SourceState,
    choose_endpoint,
    legacy_master_candidates,
    mqtt_host_candidates,
    remaining_pause,
    decide_web_failure,
    desired_web_issue,
)
from .station_freshness import age_station_data, effective_station_retention

_LOGGER = logging.getLogger(__name__)
STATION_AGING_INTERVAL = timedelta(seconds=60)


@dataclass
class EndpointProbe:
    """Last optional-endpoint probe (for diagnostics and F1)."""

    checked_at: datetime
    reachable: dict[str, bool]
    lan_ip: str | None


class RltechCoordinator(DataUpdateCoordinator[RltechData]):
    """Data update coordinator for RLTech FTTR."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: RltechClient,
    ) -> None:
        self.config_entry = entry
        self.client = client
        self._last_data: RltechData | None = None
        self._mqtt_manager: RltechMqttManager | None = None
        self._station_aging_unsub: CALLBACK_TYPE | None = None
        self.station_http_polling_active = True
        self.station_http_polling_reason = "mqtt_disabled"
        self.mqtt_stats = RltechMqttStats(
            enabled=entry_value(entry, CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT)
        )
        self._last_mqtt_hostname_enrichment: datetime | None = None
        # Set by __init__.py; see devices.py (typed loosely to avoid a cycle).
        self.device_manager: Any = None
        self.web_state = SourceState(SOURCE_WEB)
        self.endpoint_probe: EndpointProbe | None = None
        self.mqtt_host_in_use: str | None = None
        self.web_issue: str | None = None
        self.web_polling_paused_until: datetime | None = None
        # False until one real 8080 snapshot exists (placeholder while a
        # restored pause is still running).
        self.has_web_baseline = False
        # Set on resume: the next poll fetches every AP detail (review S1).
        self._force_all_details = False
        self._resume_unsub: CALLBACK_TYPE | None = None
        # Expected-offline window after an AC reboot was submitted (6b).
        self.expected_offline_until: datetime | None = None
        self._expected_offline_unsub: CALLBACK_TYPE | None = None
        self._expected_offline_failed = False
        # Whether an 8080 poll answered inside the window (paused = none).
        self._expected_offline_polled = False
        # Whether the last AC reboot was seen happening (review N3).
        self.ac_reboot_observed: bool | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry_value(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )

    async def _async_refresh(self, log_failures: bool = True, *args, **kwargs) -> None:
        # DataUpdateCoordinator logs the first failed update at ERROR; while
        # the AC reboots on request that failure is expected (6b).
        if self.expected_offline():
            log_failures = False
        await super()._async_refresh(log_failures, *args, **kwargs)

    def expected_offline(self, now: datetime | None = None) -> bool:
        """Return whether the post-reboot expected-offline window is open."""
        until = self.expected_offline_until
        return until is not None and (now or dt_util.now()) < until

    @callback
    def async_start_expected_offline(
        self, duration: timedelta = AC_REBOOT_EXPECTED_OFFLINE
    ) -> None:
        """Open the expected-offline window after an AC reboot was sent.

        Until it ends, failed polls create no Repairs issue, are not logged
        at error level and are retried every scan interval (no back-off).
        A successful poll after at least one failure ends it early; at the
        end the port-80 circuit breakers are closed again.
        """
        self._cancel_expected_offline_timer()
        self.expected_offline_until = dt_util.now() + duration
        self._expected_offline_failed = False
        self._expected_offline_polled = False
        self.ac_reboot_observed = None
        self._expected_offline_unsub = async_call_later(
            self.hass, duration, self._async_expected_offline_elapsed
        )
        # The old uptime / boot time / CPU readings are wrong from now on;
        # show them as unknown until the status pages are read again
        # (stage 7, review 6c N2).
        self._clear_status_readings()
        _LOGGER.info(
            "RLTech AC reboot requested; expecting the AC offline for up to %s s",
            int(duration.total_seconds()),
        )
        self.async_update_listeners()

    @callback
    def _clear_status_readings(self) -> None:
        """Drop the AC status readings and make the status pages due."""
        last = self._last_data
        if last is not None:
            self._last_data = clear_status_readings(last)
        if self.data is not None:
            self.data = (
                self._last_data
                if self.data is last
                else clear_status_readings(self.data)
            )

    def _boot_after_reboot_request(self) -> bool:
        """Return whether the AC boot time is after the last reboot request."""
        record = self.client.last_ac_reboot
        data = self.data
        status = data.olt_status if data is not None else None
        last_boot = status.last_boot if status is not None else None
        if record is None or last_boot is None:
            return False
        # last_boot is derived from the uptime; allow for rounding.
        return last_boot >= record.requested_at - timedelta(minutes=1)

    @callback
    def _cancel_expected_offline_timer(self) -> None:
        if self._expected_offline_unsub is not None:
            self._expected_offline_unsub()
            self._expected_offline_unsub = None

    @callback
    def _end_expected_offline(self) -> None:
        self._cancel_expected_offline_timer()
        self.expected_offline_until = None
        self._expected_offline_failed = False
        self._expected_offline_polled = False
        # Failures during the reboot must not keep port 80 paused (6b).
        self.client.reset_legacy_breakers()

    @callback
    def _async_expected_offline_elapsed(self, _now: datetime) -> None:
        self._expected_offline_unsub = None
        if self.expected_offline_until is None:
            return
        if not self.last_update_success:
            # The error log of the first failure was suppressed in the window.
            _LOGGER.warning(
                "RLTech AC Web UI at %s is still not answering %s s after the "
                "reboot request",
                entry_host(self.config_entry),
                int(AC_REBOOT_EXPECTED_OFFLINE.total_seconds()),
            )
        elif not self._expected_offline_polled:
            # Web polling was paused for the whole window: nothing to judge.
            _LOGGER.debug(
                "No RLTech AC poll ran during the reboot window; "
                "cannot tell whether the reboot happened"
            )
        elif not self._expected_offline_failed:
            self.ac_reboot_observed = self._boot_after_reboot_request()
            if not self.ac_reboot_observed:
                # Never went away and no newer boot time (review N3).
                _LOGGER.info(
                    "RLTech AC at %s answered every poll for %s s after the "
                    "reboot request and its last boot time did not change "
                    "(read every 5 minutes); the reboot may not have happened",
                    entry_host(self.config_entry),
                    int(AC_REBOOT_EXPECTED_OFFLINE.total_seconds()),
                )
        self._end_expected_offline()
        self.async_update_listeners()

    async def _async_update_data(self) -> RltechData:
        """Fetch FTTR data from the OLT."""
        session = async_get_clientsession(self.hass)
        previous = self._last_data
        now = datetime.now().astimezone()
        if self.web_polling_paused(now):
            # Paused by the switch so an administrator can use the AC Web UI
            # without the shared-login conflict; serve the last data.
            self.web_state.set_state(STATE_PAUSED)
            if previous is None:
                # Still paused after a restart/reload: no login at all, and
                # no baseline yet, so entities stay unavailable (plan 5.2).
                data = self._with_sources(RltechData())
            else:
                data = self._with_sources(self._age_stations(previous, now))
            self._last_data = data
            return data
        station_http_active, station_http_reason = self._station_http_polling_decision(
            now
        )
        self.station_http_polling_active = station_http_active
        self.station_http_polling_reason = station_http_reason
        data: RltechData | None = None
        fetch_error: Exception | None = None
        try:
            data = await self.client.fetch_snapshot(
                session,
                previous=previous,
                station_retention=self._station_retention(),
                include_ap_inventory=self._opt(
                    CONF_ENABLE_AP_POLLING, DEFAULT_ENABLE_AP_POLLING
                ),
                include_station_inventory=self._opt(
                    CONF_ENABLE_STATION_POLLING, DEFAULT_ENABLE_STATION_POLLING
                )
                and station_http_active,
                include_hardware_status=hardware_status_enabled(
                    self.config_entry, DEFAULT_ENABLE_HARDWARE_STATUS
                ),
                scan_interval=self._scan_interval(),
                # AC page timestamps (PON up/down times) use the configured
                # AC offset; "ha" follows Home Assistant (plan 5.5).
                local_timezone=ac_timezone(
                    self._opt(CONF_AC_UTC_OFFSET, "ha"),
                    dt_util.get_default_time_zone(),
                ),
                detail_interval=DEFAULT_AP_DETAIL_INTERVAL,
                force_all_details=self._force_all_details,
                burst_budget=WEB_BURST_BUDGET,
                status_interval=DEFAULT_AC_STATUS_INTERVAL,
            )
        except AccountLockedError as err:
            # Locked by failed logins elsewhere: back off, do not ask for a
            # new password (Q12).
            fetch_error = err
        except AuthenticationError as err:
            if self.expected_offline(now):
                # A booting AC may reject the login before its accounts are
                # ready: no reauth flow for a reboot the user asked for
                # (review N1). Handled like any other failure below.
                fetch_error = err
            else:
                self.web_state.record_failure(
                    STATE_AUTH_FAILED, now, type(err).__name__
                )
                self._sync_web_issue(now)
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="web_ui_auth_failed",
                    translation_placeholders={"host": entry_host(self.config_entry)},
                ) from err
        except Exception as err:  # noqa: BLE001 - classified below
            fetch_error = err

        web_error = (
            self.client.last_web_error if self.client.last_web_attempted else None
        )
        if data is None:
            web_error = web_error or fetch_error
        if isinstance(fetch_error, AuthenticationError):
            # fetch_snapshot re-raises login rejections before it records
            # last_web_error, which may still hold an older error.
            web_error = fetch_error
        if web_error is not None:
            return self._handle_web_failure(web_error, data, previous, now)
        if self.client.last_web_attempted:
            # HA logs recovery only after a failed update; a busy streak that
            # stayed within the grace period is announced here instead.
            busy_within_grace = self.web_state.last_error_kind == STATE_BUSY and (
                self.web_state.busy_polls <= self._web_busy_grace_polls()
            )
            if self.web_state.record_success(now) and busy_within_grace:
                _LOGGER.info("RLTech Web UI session is available again")
        self._sync_web_issue(now)
        data = self._finish_update(data, previous, now)
        # A resume forced every AP detail due in this poll (review S1).
        self._force_all_details = False
        if self.expected_offline(now) and self.client.last_web_attempted:
            self._expected_offline_polled = True
        if (
            self.expected_offline(now)
            and self.client.last_web_attempted
            and self._expected_offline_failed
        ):
            # Down and back again: the reboot is over (6b).
            _LOGGER.info("RLTech AC answers again after the reboot")
            self.ac_reboot_observed = True
            self._end_expected_offline()
            if previous is not None and (
                data.web_status_update == previous.web_status_update
            ):
                # Status pages not read in this poll: the readings are from
                # before the reboot. Unknown until the next poll reads them.
                data = clear_status_readings(data)
                self._last_data = data
        return data

    def _finish_update(
        self, data: RltechData, previous: RltechData | None, now: datetime
    ) -> RltechData:
        """Enrich, merge the live overlay and age a snapshot.

        ``previous`` is the data the poll started from; anything MQTT changed
        in the meantime (``self._last_data``) is kept by the overlay.
        """
        try:
            data = enrich_from_home_assistant_dhcp(self.hass, data)
        except Exception as err:  # pragma: no cover - defensive around HA internals
            _LOGGER.debug("Unable to enrich station hostnames from DHCP cache: %s", err)
        data = enrich_station_vendors(data)
        current = self._last_data
        if current is not None and current is not previous:
            data = preserve_live_overlay(current, data, previous)
            data = enrich_station_vendors(data)
        data = self._with_sources(self._age_stations(data, now))
        self._last_data = data
        self.has_web_baseline = True
        return data

    def _handle_web_failure(
        self,
        err: Exception,
        data: RltechData | None,
        previous: RltechData | None,
        now: datetime,
    ) -> RltechData:
        """Apply the busy grace period or fail the update with back-off."""
        kind = error_kind(err)
        self.web_state.record_failure(kind, now, type(err).__name__)
        busy_started = kind == STATE_BUSY and self.web_state.busy_polls == 1
        decision = decide_web_failure(
            self.web_state,
            grace_polls=self._web_busy_grace_polls(),
            scan_interval=self._scan_interval(),
        )
        expected_offline = self.expected_offline(now)
        retry_after = decision.retry_after
        if expected_offline:
            self._expected_offline_failed = True
            if retry_after is not None:
                # Notice the AC coming back quickly: no back-off (6b).
                retry_after = min(retry_after, self._scan_interval())
        self._sync_web_issue(now)
        if decision.serve_previous:
            if busy_started:
                log = _LOGGER.debug if expected_offline else _LOGGER.info
                log(
                    "RLTech Web UI login is held by another session (admin and "
                    "user share one login); keeping the last data for up to %s "
                    "polls",
                    self._web_busy_grace_polls(),
                )
            base = data or self._last_data
            if base is not None:
                if data is not None:
                    # Port 80 refreshed while 8080 was busy.
                    return self._finish_update(data, previous, now)
                aged = self._with_sources(self._age_stations(base, now))
                self._last_data = aged
                return aged
        elif data is not None:
            # Keep port-80 values for when 8080 comes back, merged with the
            # MQTT overlay and aged like any other snapshot (review S4).
            self._finish_update(data, previous, now)
        # Failures are logged once by DataUpdateCoordinator (ERROR on the first
        # failure, INFO on recovery); keep our own log at debug level.
        _LOGGER.debug("RLTech 8080 poll failed (%s): %s", kind, type(err).__name__)
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key=decision.translation_key,
            translation_placeholders={"host": entry_host(self.config_entry)},
            retry_after=retry_after,
        ) from err

    def _with_sources(self, data: RltechData) -> RltechData:
        """Attach the current per-source states to a snapshot."""
        sources = {SOURCE_WEB: self.web_state.state}
        if not hardware_status_enabled(
            self.config_entry, DEFAULT_ENABLE_HARDWARE_STATUS
        ):
            sources[SOURCE_LEGACY] = STATE_DISABLED
        else:
            for index, base_url in enumerate(self.client.legacy_base_urls):
                host = urlsplit(base_url).hostname or base_url
                state = self.client.legacy_states.get(host)
                value = state.state if state is not None else STATE_UNKNOWN
                sources[f"{SOURCE_LEGACY}:{host}"] = value
                if index == 0:
                    sources[SOURCE_LEGACY] = value
        if not self.mqtt_stats.enabled:
            sources[SOURCE_MQTT] = STATE_DISABLED
        elif self.mqtt_stats.connected:
            sources[SOURCE_MQTT] = STATE_OK
        else:
            sources[SOURCE_MQTT] = self.mqtt_stats.last_error_kind or STATE_UNKNOWN
        if data.sources == sources:
            return data
        return replace(data, sources=sources)

    def _sync_web_issue(self, now: datetime) -> None:
        """Create or clear the repairs issue matching the 8080 state."""
        desired = desired_web_issue(self.web_state, now)
        if desired == self.web_issue:
            return
        if desired is not None and self.expected_offline(now):
            # The AC is rebooting on request: no busy/unreachable issue (6b).
            return
        since = (
            self.web_state.busy_since
            if desired == ISSUE_WEB_UI_BUSY
            else self.web_state.failing_since
        )
        async_sync_web_issue(
            self.hass,
            self.config_entry,
            desired,
            {
                "host": entry_host(self.config_entry),
                "since": dt_util.as_local(since).strftime("%H:%M") if since else "",
            },
        )
        self.web_issue = desired

    def _opt(self, key: str, default: Any = None) -> Any:
        """Return an entry setting: options first, then v1 data."""
        return entry_value(self.config_entry, key, default)

    def _scan_interval(self) -> int:
        return int(
            self._opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )

    def _web_busy_grace_polls(self) -> int:
        """Return the busy grace period (options, then v1 data)."""
        value = entry_value(
            self.config_entry, CONF_WEB_BUSY_GRACE_POLLS, DEFAULT_WEB_BUSY_GRACE_POLLS
        )
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return DEFAULT_WEB_BUSY_GRACE_POLLS

    def web_polling_paused(self, now: datetime | None = None) -> bool:
        """Return whether 8080 polling is paused by the switch."""
        until = self.web_polling_paused_until
        return until is not None and (now or dt_util.now()) < until

    @callback
    def async_pause_web_polling(self, duration: timedelta = WEB_POLLING_PAUSE) -> None:
        """Pause 8080 polling; it resumes automatically after ``duration``."""
        self.web_polling_paused_until = dt_util.now() + duration
        if self._resume_unsub is not None:
            self._resume_unsub()
        self._resume_unsub = async_call_later(
            self.hass, duration, self._async_auto_resume_web_polling
        )
        # A deliberate pause is not a failure: drop any busy streak so the
        # grace period and repairs timers start fresh after resuming.
        self.web_state.reset(STATE_PAUSED)
        self._sync_web_issue(dt_util.now())
        _LOGGER.info(
            "RLTech 8080 polling paused until %s", self.web_polling_paused_until
        )
        self._persist_pause()
        self.async_update_listeners()

    async def async_resume_web_polling(self) -> None:
        """Resume 8080 polling now and refresh."""
        if self._resume_unsub is not None:
            self._resume_unsub()
            self._resume_unsub = None
        was_paused = self.web_polling_paused_until is not None
        self.web_polling_paused_until = None
        if was_paused:
            # Details aged during the pause: fetch all of them now instead of
            # the usual ceil(N/5) per poll (review S1).
            self._force_all_details = True
        if was_paused:
            _LOGGER.info("RLTech 8080 polling resumed")
        self._persist_pause()
        self.async_update_listeners()
        await self.async_request_refresh()

    @callback
    def _persist_pause(self) -> None:
        if self.device_manager is not None:
            self.device_manager.async_set_paused_until(self.web_polling_paused_until)

    @callback
    def async_restore_pause(self, until: datetime | None) -> None:
        """Re-apply a pause persisted before a restart/reload (review S2)."""
        remaining = remaining_pause(until, dt_util.now())
        if remaining is None:
            if until is not None:
                self._persist_pause()  # clear the expired value
            return
        self.web_polling_paused_until = until
        self._resume_unsub = async_call_later(
            self.hass, remaining, self._async_auto_resume_web_polling
        )
        self.web_state.reset(STATE_PAUSED)
        _LOGGER.info("RLTech 8080 polling still paused until %s", until)

    async def _async_auto_resume_web_polling(self, _now: datetime) -> None:
        self._resume_unsub = None
        await self.async_resume_web_polling()

    @callback
    def async_cancel_timers(self) -> None:
        """Cancel the auto-resume and expected-offline timers on unload."""
        if self._resume_unsub is not None:
            self._resume_unsub()
            self._resume_unsub = None
        self._cancel_expected_offline_timer()

    async def async_close_web_session(self) -> None:
        """Log out any open 8080 session after an in-flight burst (N1)."""
        await self.client.async_close(async_get_clientsession(self.hass))

    async def async_start_station_aging(self) -> None:
        """Start periodic station aging independent from poll source success."""
        if self._station_aging_unsub is not None:
            return
        self._station_aging_unsub = async_track_time_interval(
            self.hass, self._async_age_station_rows, STATION_AGING_INTERVAL
        )

    async def async_stop_station_aging(self) -> None:
        """Stop periodic station aging."""
        if self._station_aging_unsub is None:
            return
        self._station_aging_unsub()
        self._station_aging_unsub = None

    def _mqtt_credentials(self) -> tuple[str, bool]:
        """Return (psk_hex, use_factory_defaults) for MQTT."""
        psk = str(self._opt(CONF_MQTT_PSK) or "").strip()
        factory = bool(self._opt(CONF_MQTT_USE_FACTORY_DEFAULTS, False))
        return psk, factory

    async def async_start_mqtt(self) -> None:
        """Record whether MQTT is wanted; the resolver starts it (plan 5.1)."""
        self.mqtt_stats.enabled = bool(self._opt(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT))
        if not self.mqtt_stats.enabled:
            return
        psk, factory = self._mqtt_credentials()
        if not psk and not factory:
            self.mqtt_stats.last_error = "MQTT PSK is not configured"
            self.mqtt_stats.last_error_kind = STATE_NOT_CONFIGURED

    async def async_resolve_endpoints(self) -> None:
        """Pick the reachable MQTT and port-80 hosts (every 30 minutes).

        MQTT: explicit host, else the AC LAN IP (from sta-user), else the
        configured host. Port 80 master: the configured host, else the LAN IP.
        Unreachable targets are only logged at debug level; nothing here uses
        the 8080 session lock.
        """
        data = self.data
        status = data.olt_status if data is not None else None
        lan_ip = status.lan_ip if status is not None else None
        host = entry_host(self.config_entry)
        mqtt_candidates: list[str] = []
        if self.mqtt_stats.enabled:
            psk, factory = self._mqtt_credentials()
            manager = self._mqtt_manager
            connected = manager is not None and self.mqtt_stats.connected
            if (psk or factory) and not connected:
                mqtt_candidates = mqtt_host_candidates(
                    str(self._opt(CONF_MQTT_HOST, "") or "").strip(), lan_ip, host
                )
        legacy_candidates = (
            legacy_master_candidates(host, lan_ip)
            if self.client.legacy_base_urls
            and hardware_status_enabled(
                self.config_entry, DEFAULT_ENABLE_HARDWARE_STATUS
            )
            else []
        )
        targets = [(item, MQTT_PORT) for item in mqtt_candidates] + [
            (item, LEGACY_PORT) for item in legacy_candidates
        ]
        reachable = await async_tcp_probe(targets) if targets else {}
        self.endpoint_probe = EndpointProbe(
            checked_at=dt_util.utcnow(),
            reachable={f"{h}:{p}": ok for (h, p), ok in reachable.items()},
            lan_ip=lan_ip,
        )
        if mqtt_candidates:
            chosen = choose_endpoint(mqtt_candidates, reachable, MQTT_PORT)
            if chosen is None:
                self.mqtt_stats.last_error_kind = "unreachable"
                _LOGGER.debug("RLTech MQTT not reachable on %s", mqtt_candidates)
            elif self._mqtt_manager is None or self._mqtt_manager.host != chosen:
                await self.async_stop_mqtt()
                self._mqtt_manager = build_mqtt_manager(
                    self.config_entry, self, chosen
                )
                self._mqtt_manager.start()
                self.mqtt_host_in_use = chosen
        if legacy_candidates:
            chosen80 = choose_endpoint(legacy_candidates, reachable, LEGACY_PORT)
            current = urlsplit(self.client.legacy_base_urls[0]).hostname
            if chosen80 is not None and chosen80 != current:
                _LOGGER.debug("RLTech port 80 master now %s", chosen80)
                urls = [f"http://{chosen80}"] + [
                    url
                    for url in self.client.legacy_base_urls[1:]
                    if urlsplit(url).hostname != chosen80
                ]
                self.client.legacy_base_urls = urls

    @callback
    def async_schedule_endpoint_resolution(self, _now: datetime | None = None) -> None:
        """Run the resolver as an entry background task."""
        self.config_entry.async_create_background_task(
            self.hass,
            self.async_resolve_endpoints(),
            "rltech_fttr endpoint probe",
        )

    async def async_stop_mqtt(self) -> None:
        """Stop optional MQTT live overlay."""
        if self._mqtt_manager is not None:
            await self._mqtt_manager.stop()
            self._mqtt_manager = None

    @callback
    def _async_age_station_rows(self, now: datetime) -> None:
        """Age station rows on a timer even when all sources are quiet."""
        if self._last_data is None:
            return
        data = self._age_stations(self._last_data, now)
        if data is self._last_data:
            return
        self._async_set_live_overlay_data(data)
        async_dispatcher_send(
            self.hass,
            f"{SIGNAL_STATIONS_CHANGED}_{self.config_entry.entry_id}",
            now,
        )

    def async_apply_mqtt_update(
        self,
        cmd: str,
        update: list[MqttStationUpdate]
        | MqttApHealthUpdate
        | MqttApStatusUpdate
        | None,
        now: datetime,
    ) -> None:
        """Merge one parsed MQTT update into the in-memory coordinator data."""
        data = self._last_data
        if data is None or update is None:
            return
        if cmd == "XReport_StaList" and isinstance(update, list):
            data = merge_station_updates(data, update, now=now)
            data = self._maybe_enrich_mqtt_station_hostnames(data, now)
            data = enrich_station_vendors(data)
            data = self._age_stations(data, now)
        elif cmd == "XReport_ExtendInfo" and isinstance(update, MqttApHealthUpdate):
            data = merge_ap_health_update(data, update, now=now)
        elif cmd in {"APOnline", "APOffline"} and isinstance(
            update, MqttApStatusUpdate
        ):
            data = merge_ap_status_update(data, update)
        else:
            return
        if data is self._last_data:
            return
        self._async_set_live_overlay_data(data)
        if cmd == "XReport_StaList":
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_STATIONS_CHANGED}_{self.config_entry.entry_id}",
                now,
            )

    def _async_set_live_overlay_data(self, data: RltechData) -> None:
        """Publish MQTT overlay data without delaying the next HTTP poll."""
        self._last_data = data
        self.data = data
        self.async_update_listeners()

    def _maybe_enrich_mqtt_station_hostnames(
        self, data: RltechData, now: datetime
    ) -> RltechData:
        """Occasionally retry DHCP hostname enrichment for MQTT-only updates."""
        if self._last_mqtt_hostname_enrichment is not None and (
            now - self._last_mqtt_hostname_enrichment
        ) < timedelta(seconds=DEFAULT_DHCP_HOSTNAME_REFRESH_INTERVAL):
            return data
        self._last_mqtt_hostname_enrichment = now
        try:
            return enrich_from_home_assistant_dhcp(self.hass, data)
        except Exception as err:  # pragma: no cover - defensive around HA internals
            _LOGGER.debug(
                "Unable to enrich MQTT station hostnames from DHCP cache: %s", err
            )
            return data

    def _age_stations(self, data: RltechData, now: datetime) -> RltechData:
        """Apply configured station stale/removal rules."""
        stale_after = self._opt(CONF_STATION_STALE_AFTER, DEFAULT_STATION_STALE_AFTER)
        return age_station_data(
            data,
            now=now,
            stale_after=stale_after,
            retention=self._station_retention(),
        )

    def _station_retention(self) -> int:
        """Return effective station retention for runtime use."""
        return effective_station_retention(
            self._opt(CONF_STATION_RETENTION, DEFAULT_STATION_RETENTION)
        )

    def _station_http_polling_decision(self, now: datetime) -> tuple[bool, str]:
        """Return whether HTTP station polling should run this cycle."""
        if not self._opt(CONF_ENABLE_STATION_POLLING, DEFAULT_ENABLE_STATION_POLLING):
            return False, "disabled_by_config"
        if not self._opt(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT):
            return True, "mqtt_disabled"
        if self._last_data is None or not self._last_data.stations:
            return True, "bootstrap"
        if not self.mqtt_stats.connected:
            return True, "mqtt_stale"
        if self.mqtt_stats.last_station_message is None:
            return True, "bootstrap"
        stale_threshold = timedelta(
            seconds=max(
                180,
                int(
                    self._opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                )
                * 2,
            )
        )
        if now - self.mqtt_stats.last_station_message >= stale_threshold:
            return True, "mqtt_stale"
        return False, "disabled_by_mqtt"


# entry.runtime_data holds the coordinator (quality scale: runtime-data).
type RltechConfigEntry = ConfigEntry[RltechCoordinator]


def build_client(entry: ConfigEntry) -> RltechClient:
    """Build an API client from a config entry."""
    base_url = entry.data[CONF_BASE_URL]
    legacy_hosts = _legacy_hosts_from_entry(entry)
    return RltechClient(
        base_url,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        legacy_base_urls=legacy_hosts,
        legacy_username=entry_value(entry, CONF_LEGACY_USERNAME, "admin"),
        legacy_password=entry_value(entry, CONF_LEGACY_PASSWORD, "admin"),
    )


def build_mqtt_manager(
    entry: ConfigEntry, coordinator: RltechCoordinator, host: str
) -> RltechMqttManager:
    """Build an MQTT manager for the host chosen by the resolver."""
    psk = str(entry_value(entry, CONF_MQTT_PSK, "") or "")
    if not psk and entry_value(entry, CONF_MQTT_USE_FACTORY_DEFAULTS, False):
        psk = FACTORY_MQTT_PSK
    hass = coordinator.hass
    return RltechMqttManager(
        host=host,
        port=entry_value(entry, CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
        username=entry_value(entry, CONF_MQTT_USERNAME, DEFAULT_MQTT_USERNAME),
        password=entry_value(entry, CONF_MQTT_PASSWORD, DEFAULT_MQTT_PASSWORD),
        psk_identity=entry_value(
            entry, CONF_MQTT_PSK_IDENTITY, DEFAULT_MQTT_PSK_IDENTITY
        ),
        psk_hex=psk,
        client_id=f"ha-rltech-fttr-{entry.entry_id[:8]}",
        apply_update=coordinator.async_apply_mqtt_update,
        stats=coordinator.mqtt_stats,
        task_factory=lambda coro, name: entry.async_create_background_task(
            hass, coro, name
        ),
    )


def _legacy_hosts_from_entry(entry: ConfigEntry) -> list[str]:
    """Return legacy port-80 URLs for the master plus optional slave hosts."""
    hosts = [entry_host(entry)]
    extra_hosts = entry_value(entry, CONF_LEGACY_HOSTS, "")
    for item in re.split(r"[\s,]+", str(extra_hosts)):
        item = item.strip()
        if not item:
            continue
        if "://" in item:
            item = urlsplit(item).hostname or item
        elif ":" in item and item.rsplit(":", 1)[1].isdigit():
            item = item.rsplit(":", 1)[0]
        if item not in hosts:
            hosts.append(item)
    return [f"http://{item}" for item in hosts]

