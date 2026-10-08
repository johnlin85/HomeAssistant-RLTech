"""Data source health tracking for RLTech FTTR.

Pure Python (no Home Assistant imports) so the state machine, circuit breaker,
back-off and repairs decisions can be unit tested directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

STATE_UNKNOWN = "unknown"
STATE_OK = "ok"
STATE_BUSY = "busy"
STATE_UNREACHABLE = "unreachable"
STATE_ERROR = "error"
STATE_AUTH_FAILED = "auth_failed"
# Web login locked by failed attempts elsewhere: an error, not a reauth.
STATE_LOCKED = "locked"
STATE_DISABLED = "disabled"
STATE_PAUSED = "paused"

FAILURE_STATES = frozenset(
    {STATE_BUSY, STATE_UNREACHABLE, STATE_ERROR, STATE_AUTH_FAILED, STATE_LOCKED}
)

SOURCE_WEB = "web"
SOURCE_LEGACY = "legacy"
SOURCE_MQTT = "mqtt"

ISSUE_WEB_UI_BUSY = "web_ui_busy"
ISSUE_WEB_UI_UNREACHABLE = "web_ui_unreachable"
# Plan 3.2.4: busy for 10 minutes, unreachable for 30 minutes.
WEB_UI_BUSY_ISSUE_AFTER = timedelta(minutes=10)
WEB_UI_UNREACHABLE_ISSUE_AFTER = timedelta(minutes=30)

DEFAULT_WEB_BUSY_GRACE_POLLS = 3
MAX_RETRY_AFTER = 300


@dataclass
class SourceState:
    """Health of one data source (8080 Web UI, port 80, MQTT)."""

    name: str
    state: str = STATE_UNKNOWN
    last_success: datetime | None = None
    last_failure: datetime | None = None
    failing_since: datetime | None = None
    consecutive_failures: int = 0
    last_error_kind: str | None = None
    last_error: str | None = None
    # Busy polls since the last success. Other errors in between neither
    # reset nor advance it, so a busy / unreachable / busy sequence does not
    # earn a fresh grace period and flip entities back to available.
    busy_polls: int = 0
    busy_since: datetime | None = None

    @property
    def failing(self) -> bool:
        """Return whether the source is in a failure streak."""
        return self.consecutive_failures > 0

    def record_success(self, now: datetime) -> bool:
        """Record a successful poll; return True when a failure streak ends."""
        recovered = self.consecutive_failures > 0
        self.state = STATE_OK
        self.last_success = now
        self.consecutive_failures = 0
        self.failing_since = None
        self.last_error_kind = None
        self.last_error = None
        self.busy_polls = 0
        self.busy_since = None
        return recovered

    def record_failure(
        self, kind: str, now: datetime, error: str | None = None
    ) -> bool:
        """Record a failure; return True when a streak starts or its kind changes.

        A new kind (for example busy -> unreachable) restarts the streak so
        grace periods and repairs timers apply to the current problem only.
        """
        changed = self.consecutive_failures == 0 or self.last_error_kind != kind
        if changed:
            self.consecutive_failures = 0
            self.failing_since = now
        self.consecutive_failures += 1
        if kind == STATE_BUSY:
            if self.busy_polls == 0:
                self.busy_since = now
            self.busy_polls += 1
        self.state = kind
        self.last_failure = now
        self.last_error_kind = kind
        self.last_error = error
        return changed

    def reset(self, state: str) -> None:
        """End any failure streak without recording a success."""
        self.state = state
        self.consecutive_failures = 0
        self.failing_since = None
        self.busy_polls = 0
        self.busy_since = None

    def set_state(self, state: str) -> None:
        """Set a non-failure state such as disabled or paused."""
        self.state = state

    def as_dict(self) -> dict[str, Any]:
        """Return a diagnostics-safe dictionary."""
        return {
            "state": self.state,
            "last_success": _iso(self.last_success),
            "last_failure": _iso(self.last_failure),
            "failing_since": _iso(self.failing_since),
            "consecutive_failures": self.consecutive_failures,
            "last_error_kind": self.last_error_kind,
            "last_error": self.last_error,
            "busy_polls": self.busy_polls,
            "busy_since": _iso(self.busy_since),
        }


@dataclass
class CircuitBreaker:
    """Stop calling an optional source after repeated failures.

    Opens after ``threshold`` consecutive failures and stays open for
    ``cooldown``. After the cooldown one trial request is allowed; if it
    fails the breaker opens again immediately.
    """

    threshold: int = 3
    cooldown: timedelta = timedelta(minutes=30)
    failures: int = 0
    open_until: datetime | None = None
    trips: int = 0

    def allow(self, now: datetime) -> bool:
        """Return whether a request may be sent now."""
        return self.open_until is None or now >= self.open_until

    def is_open(self, now: datetime) -> bool:
        """Return whether requests are currently suppressed."""
        return not self.allow(now)

    def record_success(self) -> None:
        """Close the breaker."""
        self.failures = 0
        self.open_until = None

    def record_failure(self, now: datetime) -> bool:
        """Record a failure; return True when the breaker (re)opens."""
        half_open = self.open_until is not None
        self.failures += 1
        if half_open or self.failures >= self.threshold:
            self.open_until = now + self.cooldown
            self.failures = 0
            self.trips += 1
            return True
        return False

    def as_dict(self) -> dict[str, Any]:
        """Return a diagnostics-safe dictionary."""
        return {
            "failures": self.failures,
            "open_until": _iso(self.open_until),
            "trips": self.trips,
        }


def network_retry_after(consecutive_failures: int, base_interval: int) -> int:
    """Return the back-off delay for 8080 network errors (60/120/240/300 s)."""
    exponent = max(0, consecutive_failures - 1)
    return int(min(MAX_RETRY_AFTER, max(1, base_interval) * (2**exponent)))


def busy_within_grace(state: SourceState, grace_polls: int) -> bool:
    """Return whether a busy streak may still serve the previous data.

    Rule: the grace period covers the first ``grace_polls`` busy polls since
    the last successful 8080 poll. Only a success resets it.
    """
    return state.last_error_kind == STATE_BUSY and state.busy_polls <= max(
        0, grace_polls
    )


@dataclass(frozen=True)
class WebFailureDecision:
    """What the coordinator does after a failed 8080 poll."""

    serve_previous: bool
    translation_key: str
    retry_after: int | None = None


def decide_web_failure(
    state: SourceState, *, grace_polls: int, scan_interval: int
) -> WebFailureDecision:
    """Decide between serving stale data and failing the update.

    ``state`` must already include the current failure. Busy (another Web UI
    session holds the shared login) is tolerated for ``grace_polls`` polls and
    retried every poll without back-off: retrying never counts towards the
    login lock-out (ctc_auth_api.c:373-396). Network and unexpected errors
    fail at once and back off 60/120/240/300 s.
    """
    if state.last_error_kind == STATE_BUSY:
        return WebFailureDecision(
            serve_previous=busy_within_grace(state, grace_polls),
            translation_key=ISSUE_WEB_UI_BUSY,
        )
    return WebFailureDecision(
        serve_previous=False,
        translation_key=(
            ISSUE_WEB_UI_UNREACHABLE
            if state.last_error_kind == STATE_UNREACHABLE
            else "web_ui_error"
        ),
        retry_after=network_retry_after(state.consecutive_failures, scan_interval),
    )


def detail_is_fresh(
    last_update: datetime | None, now: datetime, max_age: timedelta
) -> bool:
    """Return whether a slow-poll value is recent enough to show."""
    return last_update is not None and now - last_update <= max_age


def ap_entity_available(ap: Any, *, requires_online: bool) -> bool:
    """AP row entities: the AP must still be listed; some need it online."""
    if ap is None:
        return False
    return not requires_online or getattr(ap, "online", None) is not False


def ap_detail_entity_available(
    ap: Any,
    detail: Any,
    *,
    now: datetime,
    max_age: timedelta,
    fallback_value: Any = None,
) -> bool:
    """AP detail entities: listed, not offline, and a recent value exists.

    ``fallback_value`` is the every-poll AP list value (optical power), which
    keeps the entity available while the rotating detail is still pending.
    """
    if not ap_entity_available(ap, requires_online=True):
        return False
    if fallback_value is not None:
        return True
    return detail is not None and detail_is_fresh(
        getattr(detail, "last_update", None), now, max_age
    )


def source_available(state: str | None) -> bool:
    """Optional-source entities (port 80) need that source to be ok."""
    return state == STATE_OK


def desired_web_issue(state: SourceState, now: datetime) -> str | None:
    """Return the repairs issue that should exist for the 8080 source.

    While the latest failure is a network error only the unreachable issue
    applies (after 30 minutes). Otherwise the busy issue applies once the
    first busy poll since the last success is 10 minutes old, so an
    interleaved unexpected response does not delete and recreate it.
    """
    if not state.failing:
        return None
    if state.last_error_kind == STATE_UNREACHABLE:
        if (
            state.failing_since is not None
            and now - state.failing_since >= WEB_UI_UNREACHABLE_ISSUE_AFTER
        ):
            return ISSUE_WEB_UI_UNREACHABLE
        return None
    if (
        state.busy_since is not None
        and now - state.busy_since >= WEB_UI_BUSY_ISSUE_AFTER
    ):
        return ISSUE_WEB_UI_BUSY
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


STATE_NOT_CONFIGURED = "not_configured"


def _dedupe(values):
    return list(dict.fromkeys(value for value in values if value))


def mqtt_host_candidates(
    explicit: str | None, lan_ip: str | None, host: str
) -> list[str]:
    """Return MQTT hosts to try: the explicit one, else LAN IP then host."""
    if explicit:
        return [explicit]
    return _dedupe([lan_ip, host])


def legacy_master_candidates(host: str, lan_ip: str | None) -> list[str]:
    """Return port-80 master hosts to try: the configured host, then LAN IP."""
    return _dedupe([host, lan_ip])


def choose_endpoint(
    candidates: list[str], reachable: Mapping[tuple[str, int], bool], port: int
) -> str | None:
    """Return the first candidate whose port answered, else None."""
    for candidate in candidates:
        if reachable.get((candidate, port)):
            return candidate
    return None


def remaining_pause(until: datetime | None, now: datetime) -> timedelta | None:
    """Return how long a persisted pause still lasts (None when over)."""
    if until is None or until <= now:
        return None
    return until - now
