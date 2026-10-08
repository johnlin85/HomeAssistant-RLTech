"""RLTech OLT Web UI client and parsers."""

from __future__ import annotations

import asyncio
import ast
from collections.abc import AsyncIterator, Iterable, Sequence
import contextlib
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, tzinfo
import html
import json
import logging
import math
import re
import time
from typing import Any
from urllib.parse import urlencode, urlsplit
import zlib

try:
    import aiohttp
except ModuleNotFoundError:
    aiohttp = None  # type: ignore[assignment]

from .sources import (
    STATE_ERROR,
    STATE_LOCKED,
    STATE_UNREACHABLE,
    CircuitBreaker,
    SourceState,
)
from .models import (
    RltechAcRebootRecord,
    RltechAp,
    RltechApDetail,
    RltechApRebootRecord,
    RltechApTask,
    RltechData,
    RltechLegacyOltSource,
    RltechLanPonPort,
    RltechLanPort,
    RltechOltStatus,
    RltechStation,
    RltechUplinkPon,
)

_LOGGER = logging.getLogger(__name__)

STA_RE = re.compile(r"var\s+STA_manage\s*=\s*'((?:\\'|[^'])*)'", re.S)
AP_RE = re.compile(r"var\s+AP_manage\s*=\s*'((?:\\'|[^'])*)'", re.S)
JS_SINGLE_QUOTED_RE = r"((?:\\'|[^'])*)"

BANDWIDTH_MAP = {
    1: "20 MHz",
    2: "40 MHz",
    3: "20/40 MHz",
    4: "20/40/80 MHz",
    5: "20/40/80/160 MHz",
}
_BOOT_TIME_DRIFT_GRACE = timedelta(minutes=2)
# Port 80 is an optional LAN-only enhancement: fail fast (plan 3.5).
LEGACY_REQUEST_TIMEOUT = 5


# Per-request cap for AP detail pages: min(DEFAULT_TIMEOUT, 8). boa expires a
# session idle for more than 10 s (REQ_SHORT_TIMEOUT), so stay below it.
AP_DETAIL_REQUEST_TIMEOUT = 8

# ap_online_list.asp also carries the XDel_AP hook (ap_online_list.asp:19);
# "0" makes it return without deleting. Every form this client posts to the
# page uses this value, never anything else.
AP_LIST_DELETE_NONE = "0"
# XAdd_Task upgrade_action for a reboot (op_reboot in ap_online_list.asp).
AP_REBOOT_ACTION = "2"
# Reboot burst (review S1): each request is capped like the detail pages
# (AP_DETAIL_REQUEST_TIMEOUT, 8 s), and the submit is only sent if it
# follows the list request within this many seconds of the list request
# being sent. boa expires a session idle for more than 10 s and then drops
# every token (a person in the Web UI too), so a slow AC gets a logout and
# a readable error instead of a late write.
AP_REBOOT_SUBMIT_GUARD = 7.5
# AC reboot (stage 6b) through mag-reset.asp, the Web UI "Device reboot"
# form (ResetForm, Reboot() at mag-reset.asp:435). The server block at
# mag-reset.asp:12-15 sets System_Entry.reboot_type from restoreFlag when
# rebootflag is 1 (1 = plain reboot). The same page also runs the factory
# reset forms (defaultflag/restoreflag2, defaultflag2/restoreflag22), USB
# backup (backupflg), SN (setsnflag), Restartflag and timed reboot
# (timereboot) blocks, so the request body is a whitelist: exactly these
# three fields (in form order, as jQuery serializeArray sends them; the
# button input is not serialized), checked again right before sending.
# The session token travels in the cookie, not in the body.
AC_REBOOT_FIELDS: tuple[tuple[str, str], ...] = (
    ("rebootflag", "1"),
    ("restoreFlag", "1"),
    ("isCUCSupport", "0"),
)
AC_REBOOT_ALLOWED_FIELDS = frozenset(name for name, _ in AC_REBOOT_FIELDS)
AC_REBOOT_RESTORE_FLAG = "1"
# The AC may be rebooting already, so the logout after the reboot form is
# expected to fail; do not wait the full request timeout for it.
AC_REBOOT_LOGOUT_TIMEOUT = 5
# Uplink PON values are kept across failed or unparsable sta-network reads
# for at most this many status intervals (2 x 300 s), then dropped so the
# entities turn unavailable instead of showing stale values (review S3).
UPLINK_PON_MAX_STATUS_CYCLES = 2
# Same limit for the sta-device/sta-user readings (CPU temperature, uptime,
# boot time...): after 2 x 300 s without a good read they turn unknown
# (review 6c N2). The AC identity (serial, LAN IP/MAC, versions) is kept.
WEB_STATUS_MAX_STATUS_CYCLES = 2
WEB_STATUS_IDENTITY_FIELDS = frozenset(
    {
        "manufacturer",
        "serial_number",
        "hardware_version",
        "software_version",
        "device_identifier",
        "device_type",
        "gateway_type",
        "lan_ip",
        "lan_mac",
    }
)
# AC reboot results after which the form may have reached the AC.
AC_REBOOT_SENT_RESULTS = frozenset({"submitted", "submitted_unconfirmed"})
# For this long after an AC reboot request (the expected-offline window,
# const.AC_REBOOT_EXPECTED_OFFLINE) a status read whose boot time is older
# than the request is a read of the AC before it went down: its readings are
# ignored and the status pages stay due (review 7 finding 2). Boot times are
# derived from the uptime, hence the slack.
AC_REBOOT_STALE_READ_WINDOW = 300
AC_REBOOT_BOOT_SLACK = timedelta(minutes=1)


def _client_timeout(seconds: int) -> Any:
    if aiohttp is None:
        return None
    return aiohttp.ClientTimeout(total=seconds)


class RltechError(Exception):
    """Base integration API error."""


class AuthenticationError(RltechError):
    """The device rejected credentials or locked login."""


class AccountLockedError(AuthenticationError):
    """The Web UI login is locked (usually wrong passwords typed in a browser).

    A subclass of AuthenticationError for compatibility, but it must not
    trigger reauth: typing the password again cannot unlock it (Q12).
    """


class AccountBusyError(RltechError):
    """The single Web UI account/session is already in use."""


class SessionExpired(RltechError):
    """Authenticated page access failed."""


class UnexpectedResponse(RltechError):
    """The device returned an unexpected response."""


class LegacyAuthenticationError(RltechError):
    """The legacy port-80 UI rejected credentials."""


class ApRebootRejected(RltechError):
    """The AC state does not allow a reboot now (nothing was submitted)."""

    def __init__(self, reason: str) -> None:
        # not_listed / offline / task_pending / slow (nothing submitted)
        super().__init__(f"AP reboot rejected: {reason}")
        self.reason = reason


def error_kind(err: BaseException) -> str:
    """Classify a poll failure for source state and back-off decisions."""
    if isinstance(err, AccountBusyError):
        return "busy"
    if isinstance(err, AccountLockedError):
        return STATE_LOCKED
    if isinstance(err, AuthenticationError):
        return "auth_failed"
    if isinstance(err, (TimeoutError, OSError)) or (
        aiohttp is not None and isinstance(err, aiohttp.ClientError)
    ):
        return STATE_UNREACHABLE
    return STATE_ERROR


def eboo_value(fields: Sequence[tuple[str, str]]) -> str:
    """Return the RLTech EBOOVALUE CRC for decoded ordered form fields."""
    cleartext = "".join(name + value for name, value in fields)
    return format(zlib.crc32(cleartext.encode("utf-8")) & 0xFFFFFFFF, "x")


def check_ac_reboot_fields(fields: Sequence[tuple[str, str]]) -> None:
    """Refuse any mag-reset.asp body other than the plain reboot whitelist.

    Raises AssertionError (explicitly, so ``python -O`` cannot strip it)
    unless the field names are exactly AC_REBOOT_ALLOWED_FIELDS, each once,
    with rebootflag "1" and restoreFlag AC_REBOOT_RESTORE_FLAG. A factory
    reset field can therefore never be sent.
    """
    names = [name for name, _ in fields]
    values = dict(fields)
    if len(names) != len(set(names)) or set(names) != AC_REBOOT_ALLOWED_FIELDS:
        raise AssertionError(f"mag-reset.asp body must be exactly {sorted(AC_REBOOT_ALLOWED_FIELDS)}")
    if values["rebootflag"] != "1" or values["restoreFlag"] != AC_REBOOT_RESTORE_FLAG:
        raise AssertionError("mag-reset.asp body must be a plain reboot")


# Kept from the mag-reset.asp answer for diagnostics (review S1).
AC_REBOOT_RESPONSE_HEAD_BYTES = 300
_HEAD_REDACTIONS = (
    # Form values (the page renders account names into hidden inputs).
    (re.compile(r"(?i)(value\s*=\s*)(\"[^\"]*\"|'[^']*')"), r"\1'***'"),
    (re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"), "***MAC***"),
    (re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}(?![0-9a-f])"), "***MAC***"),
    (re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"), "***IP***"),
    (re.compile(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-f:])"), "***IP***"),
    # Serial numbers, tokens and other long letter/digit runs; "_" does not
    # shield them (token_1a2b...), and letters-only runs of 16+ are tokens too.
    (re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{8,}(?![A-Za-z0-9])"), "***"),
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{16,}(?![A-Za-z0-9])"), "***"),
)


def sanitize_response_head(text: str | None) -> str | None:
    """Return the first AC_REBOOT_RESPONSE_HEAD_BYTES bytes, identifiers removed.

    The whole answer is redacted before it is cut, so an identifier split by
    the cut cannot leave a partial value behind (review N-R2-1).
    """
    if text is None:
        return None
    for pattern, replacement in _HEAD_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text.encode("utf-8")[:AC_REBOOT_RESPONSE_HEAD_BYTES].decode(
        "utf-8", errors="ignore"
    )


def _ac_reboot_not_sent(err: BaseException) -> bool:
    """Return whether a mag-reset.asp POST failed before sending anything.

    Only a refused/failed connect (aiohttp ClientConnectorError, or a bare
    ConnectionRefusedError) or a connect timeout; everything else may have
    reached the AC (review N2). Errors raised before the request (no token,
    body check) are not request errors and are handled by the caller.
    """
    if isinstance(err, (ConnectionRefusedError, AssertionError, RltechError)):
        return True
    if aiohttp is None:
        return False
    not_sent: tuple[type[BaseException], ...] = (aiohttp.ClientConnectorError,)
    connect_timeout = getattr(aiohttp, "ConnectionTimeoutError", None)
    if connect_timeout is not None:
        not_sent += (connect_timeout,)
    return isinstance(err, not_sent)


def _ac_reboot_answer_problem(status: int, text: str) -> str | None:
    """Return why a mag-reset.asp answer is not a clean success, else None."""
    if 300 <= status < 400:
        return f"redirect_{status}"
    if status != 200:
        return f"http_{status}"
    if "check_auth.json" in text and "username" in text:
        return "login_page"
    return None


def ac_reboot_fields() -> list[tuple[str, str]]:
    """Return the mag-reset.asp ResetForm body for a plain reboot."""
    fields = list(AC_REBOOT_FIELDS)
    check_ac_reboot_fields(fields)
    return fields


def extract_embedded_json(page: str, variable: str) -> dict[str, Any]:
    """Extract and decode a Web UI JSON object embedded in a JS string."""
    regex = AP_RE if variable == "AP_manage" else STA_RE if variable == "STA_manage" else None
    if regex is None:
        regex = re.compile(rf"var\s+{re.escape(variable)}\s*=\s*'((?:\\'|[^'])*)'", re.S)
    match = regex.search(page)
    if match is None:
        raise SessionExpired(f"{variable} missing from response")
    payload = html.unescape(match.group(1)).replace("\\'", "'")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UnexpectedResponse(f"{variable} contains invalid JSON") from exc


def _extract_js_string(page: str, variable: str) -> str:
    """Extract a JavaScript single-quoted string assigned by var/let/const."""
    match = re.search(
        rf"(?:var|let|const)\s+{re.escape(variable)}\s*=\s*'{JS_SINGLE_QUOTED_RE}'",
        page,
        re.S,
    )
    if match is None:
        raise SessionExpired(f"{variable} missing from response")
    return html.unescape(match.group(1)).replace("\\'", "'")


def _extract_json_from_js_string(page: str, variable: str) -> dict[str, Any]:
    """Extract JSON from a JavaScript single-quoted string assignment."""
    payload = _extract_js_string(page, variable)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UnexpectedResponse(f"{variable} contains invalid JSON") from exc


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _none_if_na(value: Any) -> str | None:
    text = _text(value)
    if text is None or text.upper() == "N/A":
        return None
    return text


def _int(value: Any) -> int | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _bool_status(value: Any) -> bool | None:
    text = _text(value)
    if text is None:
        return None
    if text == "1":
        return True
    if text == "0":
        return False
    return None


_NULL_TEXT = {"NULL", "N/A", "NONE"}
_DBM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FLAT_JSON_PAIR_RE = re.compile(
    r'"([A-Za-z0-9_]+)"\s*:\s*(?:"((?:\\.|[^"\\])*)"|(-?\d+(?:\.\d+)?))'
)
_AC_DATETIME_RE = re.compile(
    r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})"
)
# registerinfo reports "1970-01-01 08:00:00" (epoch in AC local time) for
# "never happened" (git_FTTR apps/private/ap_server/ap_support.c:563-564).
_AC_DATETIME_MIN_YEAR = 2000


def _none_if_null(value: Any) -> str | None:
    """Return text, treating AC placeholders (SQL NULL, N/A) as missing."""
    text = _text(value)
    if text is None or text.upper() in _NULL_TEXT:
        return None
    return text


def _dbm(value: Any) -> float | None:
    """Parse an optical power value leniently.

    The AP reports OptTxPower/OptRxPower as "%0.2f" text or the raw
    Info_PonPhy TxPowerDbm/RxPowerDbm value (slaveMgr_v2
    client/api/api_client_info.c:232-259), "" when unknown, and the AC
    database returns "NULL" for rows created before the column existed.
    The exact TxPowerDbm text format is unverified, so accept an optional
    unit suffix such as " dBm" and surrounding padding.
    """
    text = _none_if_null(value)
    if text is None:
        return None
    match = _DBM_RE.search(text)
    return float(match.group(0)) if match else None


def _salvage_flat_json(payload: str) -> dict[str, Any]:
    """Recover "key":"value" pairs from a truncated flat JSON object.

    boa copies XQuery_AP_Info/registerinfo output into a 1024-byte buffer
    (git_FTTR apps/public/boa-asp/src/asp/asp_handler.c:3276, :4688), so a
    long row can be cut mid-object. Every AP column is a JSON string, so the
    complete pairs before the cut are still usable.
    """
    values: dict[str, Any] = {}
    for key, text_value, number in _FLAT_JSON_PAIR_RE.findall(payload):
        if number:
            values[key] = number
            continue
        try:
            values[key] = json.loads(f'"{text_value}"')
        except json.JSONDecodeError:
            values[key] = text_value
    return values


def _parse_ac_datetime(
    value: Any,
    *,
    local_timezone: tzinfo = UTC,
) -> datetime | None:
    """Parse AC-local timestamps such as '2026-09-28 18:24:10'.

    The exact registerinfo last_down_time format is unverified on a live AC;
    fall back to the legacy 'Fri Aug 21 06:12:26 2026' format as well.
    """
    text = _none_if_null(value)
    # "-" is how registerinfo reports "never" on a real AC (phase 0).
    if text is None or text == "-":
        return None
    match = _AC_DATETIME_RE.search(text)
    if match is None:
        parsed = _parse_legacy_datetime(text, local_timezone=local_timezone)
    else:
        try:
            parsed = datetime(
                *(int(part) for part in match.groups()),
                tzinfo=local_timezone,
            )
        except ValueError:
            return None
    if parsed is None or parsed.year < _AC_DATETIME_MIN_YEAR:
        return None
    return parsed


def parse_ap_task_list(text: str) -> dict[str, RltechApTask]:
    """Parse the XQuery_Task_List rows embedded in ap_online_list.asp.

    ``var list = '...'`` holds ``respCode 3`` when no task exists and
    ``respCode 0`` with ``data.list`` rows otherwise (appendix A.1/A.5).
    Raises SessionExpired when the variable is missing.
    """
    payload = _extract_json_from_js_string(text, "list")
    tasks: dict[str, RltechApTask] = {}
    for row in _payload_rows(payload):
        if not isinstance(row, dict):
            continue
        mac = normalize_mac(row.get("Mac"))
        if mac is None:
            continue
        tasks[mac] = RltechApTask(
            mac=mac,
            action=_text(row.get("Action")),
            upgrade_status=_text(row.get("UpgradeStatus")),
            reboot_status=_text(row.get("RebootStatus")),
            restore_status=_text(row.get("RestoreStatus")),
        )
    return tasks


def ap_task_blocks_reboot(task: RltechApTask | None) -> bool:
    """Return whether an unfinished task forbids a new one (Web UI op_reboot).

    Same rules as ap_online_list.asp:517-545: an upgrade (Action 1) blocks
    unless UpgradeStatus is 0/1/4/5; a reboot (2) or restore (3) blocks until
    its status is 1 or 2.
    """
    if task is None:
        return False
    if task.action == "1":
        return task.upgrade_status not in {"0", "1", "4", "5"}
    if task.action == "2":
        return task.reboot_status not in {"1", "2"}
    if task.action == "3":
        return task.restore_status not in {"1", "2"}
    return False


def xadd_task_accepted(text: str) -> bool:
    """Return whether the XAdd_Task hook confirmed the task.

    The hook prints a bare ``1`` (asp_send_format_response("1")) into the
    page script before ``var AP_manage`` (appendix A.5); a plain list
    request leaves those lines empty.
    """
    index = text.find("var AP_manage")
    if index < 0:
        return False
    head = text[:index]
    start = head.lower().rfind("<script")
    if start < 0:
        return False
    close = head.find(">", start)
    script = head[close + 1 :] if close >= 0 else ""
    return any(line.strip() == "1" for line in script.splitlines())


def normalize_mac(value: Any) -> str | None:
    """Normalize a MAC address to colon-separated uppercase text."""
    text = _text(value)
    if text is None:
        return None
    clean = re.sub(r"[^0-9A-Fa-f]", "", text)
    if len(clean) != 12:
        return None
    return ":".join(clean[i : i + 2] for i in range(0, 12, 2)).upper()


def _channel_band(channel: int | None) -> str | None:
    if channel is None or channel <= 0:
        return None
    return "2.4 GHz" if channel <= 14 else "5 GHz"


def _bandwidth(value: Any) -> str | None:
    return BANDWIDTH_MAP.get(_int(value))


def _payload_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    code = str(payload.get("respCode", "0"))
    if code == "3":
        return []
    if code != "0":
        raise UnexpectedResponse(f"unexpected respCode {code}")
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("list")
    return rows if isinstance(rows, list) else []


def _payload_total(payload: dict[str, Any]) -> int:
    return _int(payload.get("total")) or 0


def _normalize_ap_payloads(ap_payloads: Iterable[dict[str, Any]]) -> dict[str, RltechAp]:
    """Normalize AP payload pages into an AP map."""
    aps: dict[str, RltechAp] = {}
    for payload in ap_payloads:
        for row in _payload_rows(payload):
            ap = normalize_ap(row)
            if ap is not None:
                aps[ap.mac] = ap
    return aps


def normalize_ap(row: dict[str, Any]) -> RltechAp | None:
    """Normalize one managed AP row."""
    mac = normalize_mac(row.get("Mac"))
    if mac is None:
        return None
    return RltechAp(
        mac=mac,
        ip=_text(row.get("IP")),
        model=_text(row.get("Model")),
        version=_text(row.get("Version")),
        online=_bool_status(row.get("Status")),
        profile=_text(row.get("Profile")),
        profile_idx=_text(row.get("ProfileIdx")),
        channel_24=_int(row.get("ChannelG24")),
        channel_5=_int(row.get("ChannelG5")),
        bssid_24=normalize_mac(row.get("BssidG24")),
        bssid_5=normalize_mac(row.get("BssidG5")),
        assoc_count=_int(row.get("Assoc")),
        alias=_text(row.get("Alias")),
        uplink=_int(row.get("Uplink")),
        uplink_port=_int(row.get("UplinkPort")),
        sn=_text(row.get("SN")),
        dev_sn=_text(row.get("DevSN")),
        upgrade_flag=_text(row.get("UpgradeFlag")),
        optical_tx_power=_dbm(row.get("OptTxPower")),
        optical_rx_power=_dbm(row.get("OptRxPower")),
    )


def _parse_ap_detail_list_ap(text: str) -> tuple[dict[str, Any], str | None]:
    """Return the list_ap data object and an optional partial-parse marker."""
    payload_text = _extract_js_string(text, "list_ap")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        # Truncated by the 1024-byte boa buffer: keep the complete pairs.
        salvaged = _salvage_flat_json(payload_text)
        if normalize_mac(salvaged.get("Mac")) is None:
            raise UnexpectedResponse("list_ap contains invalid JSON") from None
        return salvaged, "list_ap_truncated"
    if not isinstance(payload, dict):
        raise UnexpectedResponse("list_ap is not a JSON object")
    code = _text(payload.get("respCode"))
    if code not in {None, "0"}:
        raise UnexpectedResponse(f"list_ap respCode {code}")
    ap_data = payload.get("data")
    if not isinstance(ap_data, dict):
        raise UnexpectedResponse("list_ap missing data object")
    return ap_data, None


def _parse_ap_detail_list_pon(text: str) -> tuple[dict[str, Any], str | None]:
    """Return registerinfo PON data, tolerating every observed/likely shape.

    ``list_pon.data`` is a JSON string holding an object per
    ap_support.c:559-568 and the page's own quote-stripping regex
    (ap_online_detail.asp:32), but an embedded object is accepted too.
    ``result`` "9" means "no PON data" (ap_online_detail.asp:288). An empty
    or invalid list_pon must not discard the list_ap values.
    """
    try:
        payload_text = _extract_js_string(text, "list_pon")
    except SessionExpired:
        return {}, None
    if not payload_text.strip():
        return {}, None
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        # Truncated registerinfo: unescape the inner object and keep the
        # complete pairs (the pair right after "data" is lost).
        salvaged = _salvage_flat_json(payload_text.replace('\\"', '"'))
        salvaged.pop("result", None)
        salvaged.pop("data", None)
        return salvaged, "list_pon_invalid"
    if not isinstance(payload, dict):
        return {}, "list_pon_invalid"
    if _text(payload.get("result")) not in {None, "0"}:
        return {}, None
    raw = payload.get("data")
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return _salvage_flat_json(raw), "list_pon_invalid"
        if isinstance(decoded, dict):
            return decoded, None
        return {}, "list_pon_invalid"
    return {}, None


def parse_ap_detail(
    text: str,
    *,
    now: datetime | None = None,
    local_timezone: tzinfo = UTC,
) -> RltechApDetail:
    """Parse ap_online_detail.asp embedded AP and PON detail JSON.

    PON values from registerinfo take precedence over the AP-reported
    list_ap copies, mirroring ap_online_detail.asp:336-360.
    """
    now = now or datetime.now(UTC)
    ap_data, ap_error = _parse_ap_detail_list_ap(text)
    pon_data, pon_error = _parse_ap_detail_list_pon(text)

    online = _bool_status(ap_data.get("Status"))
    sys_duration = _int(ap_data.get("SysDuration"))
    # SysDuration is the AP's last reported uptime; an offline AP's value is
    # frozen, so only derive last_boot for APs the AC reports as online.
    last_boot = (
        now - timedelta(seconds=sys_duration)
        if sys_duration is not None and sys_duration >= 0 and online is not False
        else None
    )
    last_down_at = _parse_ac_datetime(
        pon_data.get("last_down_time"), local_timezone=local_timezone
    )
    # A real AC reported last_down_cause "FiberBroken" with last_down_time "-"
    # (never down): the cause is a stale default then, so do not show it.
    pon_down_cause = (
        None
        if _text(pon_data.get("last_down_time")) == "-"
        else _none_if_null(pon_data.get("last_down_cause"))
    )
    last_down_cause = pon_down_cause or _none_if_null(
        ap_data.get("LastOfflineReason")
    )
    optical_tx_power = _dbm(pon_data.get("opt_tx_power"))
    if optical_tx_power is None:
        optical_tx_power = _dbm(ap_data.get("OptTxPower"))
    optical_rx_power = _dbm(pon_data.get("opt_rx_power"))
    if optical_rx_power is None:
        optical_rx_power = _dbm(ap_data.get("OptRxPower"))

    return RltechApDetail(
        mac=normalize_mac(ap_data.get("Mac")),
        ip=_text(ap_data.get("IP")),
        model=_text(ap_data.get("Model")),
        version=_text(ap_data.get("Version")),
        online=online,
        profile=_text(ap_data.get("Profile")),
        alias=_text(ap_data.get("Alias")),
        sn=_text(ap_data.get("SN")),
        dev_sn=_text(ap_data.get("DevSN")),
        uplink=_int(ap_data.get("Uplink")),
        uplink_port=_int(ap_data.get("UplinkPort")),
        assoc_count=_int(ap_data.get("Assoc")),
        channel_24=_int(ap_data.get("ChannelG24")),
        channel_5=_int(ap_data.get("ChannelG5")),
        bssid_24=normalize_mac(ap_data.get("BssidG24")),
        bssid_5=normalize_mac(ap_data.get("BssidG5")),
        hostname=_none_if_null(ap_data.get("DevName")),
        sys_duration=sys_duration,
        last_boot=last_boot,
        ram_size=_int(ap_data.get("RamSize")),
        flash_size=_int(ap_data.get("FlashSize")),
        cpu_usage=_float(ap_data.get("CPUUsage")),
        cpu_temperature=_float(ap_data.get("CPUTemp")),
        memory_usage=_float(ap_data.get("MEMUsage")),
        flash_usage=_float(ap_data.get("FlashUsage")),
        pon_id=_int(pon_data.get("pon_id")),
        onu_id=_int(pon_data.get("onu_id")),
        pon_sn=_text(pon_data.get("pon_sn")),
        onu_status=_text(pon_data.get("onu_status")),
        ont_distance=_int(pon_data.get("ont_distance")),
        optical_temperature=_float(pon_data.get("opt_temperature")),
        optical_current=_float(pon_data.get("opt_current")),
        optical_voltage=_float(pon_data.get("opt_voltage")),
        optical_tx_power=optical_tx_power,
        optical_rx_power=optical_rx_power,
        downstream_optical_rx_power=_dbm(pon_data.get("dnopt_rx_power")),
        optical_error_status=_int(pon_data.get("opt_err_status")),
        active=_bool_status(pon_data.get("active")),
        last_up_time=_text(pon_data.get("last_up_time")),
        last_down_time=_text(pon_data.get("last_down_time")),
        last_down_at=last_down_at,
        reg_off_time=last_down_at,
        last_dying_gasp_time=_text(pon_data.get("last_dying_gasptime")),
        last_down_cause=last_down_cause,
        identify_vendor=_text(pon_data.get("identify_vendor")),
        equipment_id=_text(pon_data.get("equipment_id")),
        sn_address=_text(pon_data.get("sn_address")),
        hardware_version=_none_if_na(pon_data.get("hardware_version")),
        software_version=_none_if_na(pon_data.get("software_version")),
        firmware_version=_none_if_na(pon_data.get("fireware_version")),
        detail_source="8080",
        detail_error=ap_error or pon_error,
    )


def normalize_station(
    row: dict[str, Any],
    *,
    now: datetime,
    aps: dict[str, RltechAp],
) -> RltechStation | None:
    """Normalize one station row."""
    mac = normalize_mac(row.get("Mac"))
    if mac is None:
        return None
    channel = _int(row.get("Channel"))
    ap_mac = normalize_mac(row.get("APMac"))
    ap = aps.get(ap_mac or "")
    reported_online = _bool_status(row.get("Status")) is not False
    return RltechStation(
        mac=mac,
        id=_text(row.get("ID")),
        reported_online=reported_online,
        home=reported_online,
        last_seen=now if reported_online else None,
        first_seen=now,
        ip=_text(row.get("IP")),
        hostname=_text(row.get("HostName")),
        ssid=_text(row.get("SSID")),
        ap_mac=ap_mac,
        ap_alias=ap.alias if ap else None,
        rssi=_int(row.get("RSSI")),
        rx_rate=_float(row.get("RxRate")),
        tx_rate=_float(row.get("TxRate")),
        rx_nego_rate=_float(row.get("RxNegoRate")),
        tx_nego_rate=_float(row.get("TxNegoRate")),
        uptime=_int(row.get("UpTime")),
        channel=channel,
        band=_channel_band(channel),
        bandwidth=_bandwidth(row.get("Bandwidth")),
        vlan=_int(row.get("Vlan")),
        total_count=_text(row.get("ToTalcnt")),
        update_time=_int(row.get("UpDateTime")),
    )


def _js_string(text: str, name: str) -> str | None:
    patterns = [
        rf"var\s+{re.escape(name)}\s*=\s*['\"]([^'\"]*)['\"]",
        rf"this\.{re.escape(name)}\s*=\s*['\"]([^'\"]*)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return html.unescape(match.group(1).strip())
    return None


def _hidden_value(text: str, element_id: str) -> str | None:
    match = re.search(
        rf"id=['\"]{re.escape(element_id)}['\"][^>]*value=['\"]([^'\"]*)['\"]",
        text,
        re.I | re.S,
    )
    return html.unescape(match.group(1).strip()) if match else None


def _field_literal(text: str, name: str) -> str | None:
    patterns = [
        rf"{re.escape(name)}\s*[:=]\s*['\"]([^'\"]*)['\"]",
        rf"{re.escape(name)}['\"]?\s*,\s*['\"]([^'\"]*)['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            return html.unescape(match.group(1).strip())
    return None


def _field_number(text: str, name: str) -> float | None:
    literal = _field_literal(text, name)
    if literal is not None:
        return _float(literal)
    match = re.search(rf"{re.escape(name)}\D+(-?\d+(?:\.\d+)?)", text, re.I | re.S)
    return _float(match.group(1)) if match else None


def _table_value(text: str, label: str) -> str | None:
    match = re.search(
        rf"<td[^>]*>\s*{re.escape(label)}\s*[:：]\s*</td>\s*<td[^>]*>(.*?)</td>",
        text,
        re.I | re.S,
    )
    if not match:
        return None
    value = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.S)
    # Only trust a script-rendered cell when it has a single literal write;
    # branches such as the transMode switch for "设备类型" are ambiguous.
    writes = re.findall(r"document\.write\(", value, re.I)
    script_literal = re.search(
        r"document\.write\(\s*['\"]([^'\"]*)['\"]\s*\)", value, re.I | re.S
    )
    if script_literal and len(writes) == 1:
        return _none_if_na(html.unescape(script_literal.group(1).strip()))
    value = re.sub(r"<script\b.*?</script>", "", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _table_number(text: str, label: str) -> float | None:
    value = _table_value(text, label)
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return _float(match.group(0)) if match else None


def _duration(value: str | None) -> timedelta | None:
    """Parse vendor duration text such as '18 Days 20 Hour 37 Min 46 Sec'."""
    if value is None:
        return None
    total = 0
    for number, unit in re.findall(
        r"(\d+)\s*(days?|d|hours?|h|mins?|minutes?|m|secs?|seconds?|s)\b",
        value,
        re.I,
    ):
        amount = int(number)
        unit = unit.lower()
        if unit.startswith("d"):
            total += amount * 86400
        elif unit.startswith("h"):
            total += amount * 3600
        elif unit.startswith("m"):
            total += amount * 60
        elif unit.startswith("s"):
            total += amount
    return timedelta(seconds=total) if total else None


def _duration_text(duration: timedelta) -> str:
    """Format a duration in the same unit names used by the vendor UI."""
    total = max(0, int(duration.total_seconds()))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} Days")
    if hours or parts:
        parts.append(f"{hours} Hour")
    if minutes or parts:
        parts.append(f"{minutes} Min")
    parts.append(f"{seconds} Sec")
    return " ".join(parts)


def _function_var_literal(text: str, function_name: str, var_name: str) -> str | None:
    """Return a JavaScript var literal from a named function body."""
    match = re.search(rf"function\s+{re.escape(function_name)}\s*\(", text)
    if not match:
        return None
    body = text[match.end() : match.end() + 4000]
    var_match = re.search(
        rf"\bvar\s+{re.escape(var_name)}\s*=\s*['\"]([^'\"]*)['\"]",
        body,
    )
    return html.unescape(var_match.group(1).strip()) if var_match else None


def _script_wan_link_uptime(text: str) -> str | None:
    """Parse WAN link uptime rendered by sta-device.asp JavaScript."""
    is_wan_up = _function_var_literal(text, "wanUpTime", "IsWanUp")
    if is_wan_up in {"0", "N/A"}:
        return None
    cur_time = _int(_function_var_literal(text, "wanUpTime", "curTime"))
    wan_up_time = _int(_function_var_literal(text, "wanUpTime", "WanUpTime"))
    if cur_time is None or wan_up_time is None or cur_time < wan_up_time:
        return None
    return _duration_text(timedelta(seconds=cur_time - wan_up_time))


# sta-device.asp labels follow the AC UI language, not the login request
# (phase 0 capture: Chinese labels although selectLanguage=English was sent).
_OLT_LABELS = {
    "manufacturer": ("Manufacturer", "生产厂家", "制造商"),
    "device_type": ("Device Type", "设备类型"),
    "gateway_type": ("Gateway Type", "设备型号"),
    "serial_number": ("Serial Number", "设备标识号", "序列号"),
    "hardware_version": ("Hardware Version", "硬件版本"),
    "software_version": ("Software Version", "软件版本"),
    "run_time": ("Run Time", "运行时间"),
    "cpu_temperature": ("CPU Temperature", "CPU温度", "CPU 温度"),
    "wan_link_uptime": ("WAN Link Up Time", "WAN在线时间"),
}
_DEVICE_IDENTIFIER_RE = re.compile(r"([0-9A-Fa-f]{6})-(\S+)")


def _labeled_value(text: str, key: str) -> str | None:
    for label in _OLT_LABELS[key]:
        value = _table_value(text, label)
        if value is not None:
            return value
    return None


def _labeled_number(text: str, key: str) -> float | None:
    for label in _OLT_LABELS[key]:
        value = _table_number(text, label)
        if value is not None:
            return value
    return None


def _assigned_literal(text: str, name: str) -> str | None:
    """Return a quoted literal assigned with ``name = '...'`` or ``name: '...'``.

    Unlike _field_literal this ignores call arguments, so JS such as
    ``new stDeviceInfo(..., top.ModelName, "19191900AABB818180", ...)`` in
    sta-device.asp is not mistaken for a value.
    """
    match = re.search(
        rf"\b{re.escape(name)}\s*[:=]\s*['\"]([^'\"]*)['\"]", text, re.I | re.S
    )
    return html.unescape(match.group(1).strip()) if match else None


def _split_device_identifier(value: str | None) -> tuple[str | None, str | None]:
    """Split sta-device '<OUI>-<serial>' into (identifier, serial)."""
    if value is None:
        return None, None
    match = _DEVICE_IDENTIFIER_RE.fullmatch(value.strip())
    if match is None:
        return None, value
    return value, match.group(2)


def parse_olt_status(text: str, *, now: datetime | None = None) -> RltechOltStatus:
    """Parse optional OLT/controller status values from sta-device.asp."""
    now = now or datetime.now(UTC)
    link_state = _int(_none_if_na(_js_string(text, "LinkSta")))
    # wanUpTime() carries curTime, the AC uptime in seconds (the page derives
    # "WAN Link Up Time" as curTime - WanUpTime). It is the most precise boot
    # reference and independent of the UI language.
    uptime_seconds = _int(_function_var_literal(text, "wanUpTime", "curTime"))
    system_uptime = (
        _labeled_value(text, "run_time")
        or _field_literal(text, "sysUpTime")
        or (
            _duration_text(timedelta(seconds=uptime_seconds))
            if uptime_seconds is not None
            else None
        )
    )
    wan_table_value = _labeled_value(text, "wan_link_uptime")
    wan_literal_value = _field_literal(text, "WanUpTime")
    wan_link_uptime = (
        wan_table_value
        if _duration(wan_table_value) is not None
        else _script_wan_link_uptime(text)
        or (
            wan_literal_value
            if wan_literal_value is not None and _duration(wan_literal_value) is not None
            else None
        )
    )
    system_duration = (
        timedelta(seconds=uptime_seconds)
        if uptime_seconds is not None and uptime_seconds > 0
        else _duration(system_uptime)
    )
    wan_duration = _duration(wan_link_uptime)
    device_identifier, serial_number = _split_device_identifier(
        _labeled_value(text, "serial_number") or _field_literal(text, "SerialNum")
    )
    return RltechOltStatus(
        pon_link_state=link_state,
        fec_state=(
            _int(_js_string(text, "fecState")) == 1
            if _js_string(text, "fecState") is not None
            else None
        ),
        pon_tx_frames=_int(_js_string(text, "PonSendPkt")),
        pon_rx_frames=_int(_js_string(text, "PonRecvPkt")),
        pon_up_since=_none_if_na(
            _js_string(text, "ponuptime") or _hidden_value(text, "Uptime")
        ),
        current_time=_none_if_na(_js_string(text, "curtime")),
        system_uptime=system_uptime,
        uptime_seconds=(
            int(system_duration.total_seconds())
            if system_duration is not None and system_duration.total_seconds() > 0
            else None
        ),
        wan_link_uptime=wan_link_uptime,
        last_boot=now - system_duration if system_duration is not None else None,
        wan_link_up_since=now - wan_duration if wan_duration is not None else None,
        device_type=_labeled_value(text, "device_type")
        or _assigned_literal(text, "DeviceType"),
        gateway_type=_labeled_value(text, "gateway_type")
        or _assigned_literal(text, "ModelName"),
        cpu_temperature=_labeled_number(text, "cpu_temperature")
        or _field_number(text, "CpuTemp"),
        cpu_usage=_field_number(text, "CpuUsage"),
        memory_usage=_field_number(text, "MemoryUsage"),
        flash_usage=_field_number(text, "FlashUsage"),
        manufacturer=_labeled_value(text, "manufacturer")
        or _assigned_literal(text, "Manufacturer"),
        serial_number=serial_number,
        device_identifier=device_identifier,
        hardware_version=_labeled_value(text, "hardware_version")
        or _field_literal(text, "CustomerHWVersion"),
        software_version=_labeled_value(text, "software_version")
        or _field_literal(text, "CustomerSWVersion"),
    )


def parse_ac_lan_identity(text: str) -> tuple[str | None, str | None]:
    """Return the controller LAN (br0) IP and MAC from sta-user.asp."""
    ip = None
    for label in ("IP地址", "IP Address", "IPv4 Address"):
        value = _table_value(text, label)
        if value and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
            ip = value
            break
    mac = None
    for label in ("MAC地址", "MAC Address"):
        mac = normalize_mac(_table_value(text, label))
        if mac is not None:
            break
    return ip, mac


def _stable_datetime(
    new_value: datetime | None,
    old_value: datetime | None,
    *,
    grace: timedelta = _BOOT_TIME_DRIFT_GRACE,
) -> datetime | None:
    """Keep derived timestamps stable across small uptime/poll timing drift."""
    if new_value is None or old_value is None:
        return new_value
    if abs(new_value - old_value) <= grace:
        return old_value
    return new_value


def _stabilize_olt_status(
    status: RltechOltStatus | None, previous: RltechData | None
) -> RltechOltStatus | None:
    """Preserve prior OLT boot/link timestamps when only parser drift changed."""
    return _stabilize_olt_status_against(
        status,
        previous.olt_status if previous is not None else None,
    )


def _stabilize_olt_status_against(
    status: RltechOltStatus | None,
    previous_status: RltechOltStatus | None,
) -> RltechOltStatus | None:
    """Stabilize derived OLT timestamps against one source's prior status."""
    if status is None or previous_status is None:
        return status
    return replace(
        status,
        last_boot=_stable_datetime(status.last_boot, previous_status.last_boot),
        wan_link_up_since=_stable_datetime(
            status.wan_link_up_since, previous_status.wan_link_up_since
        ),
    )


_U64_WRAP_THRESHOLD = 1 << 63


def _byte_counter(value: Any) -> int | None:
    """Parse a byte counter, rejecting u64 wrap-around of negative values.

    sta-user.asp reported RxBytes 18446744073147154563 (2**64 - 562397053)
    on a real AC, i.e. a negative counter printed as unsigned.
    """
    text = _text(value)
    if text is None or not text.isdigit():
        return None
    number = int(text)
    return number if number < _U64_WRAP_THRESHOLD else None


def parse_lan_ports(text: str) -> dict[int, RltechLanPort]:
    """Parse LAN Ethernet port rows from sta-user.asp."""
    try:
        payload = _extract_json_from_js_string(text, "lancntvalue")
    except SessionExpired:
        payload = None
    if payload is not None:
        rows = payload.get("data")
        ports: dict[int, RltechLanPort] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                port = _int(row.get("Port"))
                if port is None:
                    continue
                connected = _bool_status(row.get("LanState"))
                label = f"LANPON{port - 4}" if port > 4 else f"LAN-{port}"
                mode = _none_if_na(row.get("Mode"))
                if mode == "Full":
                    mode = "Full-Duplex"
                elif mode == "Half":
                    mode = "Half-Duplex"
                ports[port] = RltechLanPort(
                    port=port,
                    label=label,
                    status="connected"
                    if connected
                    else "disconnected"
                    if connected is False
                    else None,
                    connected=connected,
                    rate=_none_if_na(row.get("Negoration")),
                    mode=mode,
                    tx_bytes=_byte_counter(row.get("TxBytes")),
                    rx_bytes=_byte_counter(row.get("RxBytes")),
                )
        return ports

    match = re.search(r"Ethernet\s*=\s*(\[.*?\])\s*(?:;|\n)", text, re.S)
    if not match:
        return {}
    raw = re.sub(r",\s*null\s*(?=\])", "", match.group(1))
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UnexpectedResponse("Ethernet array contains invalid JSON") from exc

    ports: dict[int, RltechLanPort] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, list) or len(row) < 10:
            continue
        path = _text(row[0])
        port = _int(path.rsplit(".", 1)[-1]) if path and "." in path else index
        ports[port or index] = RltechLanPort(
            port=port or index,
            label=f"LAN-{port or index}",
            path=path,
            status=_none_if_na(row[1]),
            connected=(_none_if_na(row[1]) or "").lower() == "up",
            tx_bytes=_int(row[2]),
            tx_packets=_int(row[3]),
            tx_errors=_int(row[4]),
            tx_drops=_int(row[5]),
            rx_bytes=_int(row[6]),
            rx_packets=_int(row[7]),
            rx_errors=_int(row[8]),
            rx_drops=_int(row[9]),
        )
    return ports


def _lanpon_rx_power(value: Any, status: str | None) -> float | None:
    """Return LAN-PON RX power, None when the link is down.

    A down port reports rx_power "0.00" (sta-user.asp on a real AC, LANPON1
    down), which would read as a strong 0 dBm signal. TX power, temperature,
    voltage and bias current are real module readings even without a link,
    so only RX is suppressed.
    """
    power = _float(_strip_units(value))
    if power == 0.0 and (status or "").lower() != "up":
        return None
    return power


def parse_lanpon_ports(text: str) -> dict[int, RltechLanPonPort]:
    """Parse LAN-PON port rows from sta-user.asp."""
    try:
        payload = extract_embedded_json(text, "ponport_info")
    except SessionExpired:
        return {}
    rows = payload.get("list")
    if not isinstance(rows, list):
        return {}

    ports: dict[int, RltechLanPonPort] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ponid = _int(row.get("ponid"))
        if ponid is None:
            continue
        status = _none_if_na(row.get("status"))
        ports[ponid] = RltechLanPonPort(
            ponid=ponid,
            status=status,
            active=_none_if_na(row.get("active")),
            fec=_none_if_na(row.get("fec")),
            autoregister=_none_if_na(row.get("autoregister")),
            tx_power=_float(row.get("tx_power")),
            rx_power=_lanpon_rx_power(row.get("rx_power"), status),
            temperature=_float(row.get("temp")),
            voltage=_float(row.get("voltage")),
            current=_float(row.get("current")),
        )
    return ports


_PON_INFO_RE = re.compile(
    r"function\s+PonInfoClass\s*\(\s*\)\s*\{(.*?)(?:var\s+PonInfo\s*=\s*new\s+PonInfoClass|\Z)",
    re.S,
)
_PON_ASSIGN_RE = re.compile(
    r"(?:\bvar\s+|\bthis\.)([A-Za-z_]\w*)\s*=\s*'((?:\\.|[^'\\])*)'"
)
_PON_NA = {"", "N/A", "NA", "-", "NULL"}


def _pon_raw(values: dict[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    text = value.strip()
    return None if text.upper() in _PON_NA else text


def _pon_number(values: dict[str, str], key: str) -> float | None:
    text = _pon_raw(values, key)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _pon_dbm(raw: float | None) -> float | None:
    """Return 10*log10(raw/10000) rounded like the page (0.1 dB).

    raw is in 0.1 uW. 1 (-40 dBm) is what the PHY reports without light and
    0 has no logarithm: both mean "no reading".
    """
    if raw is None or raw <= 1:
        return None
    return round(10 * math.log10(raw / 10000), 1)


def _pon_temperature(raw: float | None) -> float | None:
    """Return the page's transTemperature (1/256 degC, 16-bit signed)."""
    if raw is None:
        return None
    if raw >= 2**15:
        return round(-(2**16 - raw) / 256, 1)
    return round(raw / 256, 1)


def _pon_phy_up(phy: str | None) -> bool | None:
    """Return the page's phy test: ``phyStatus.indexOf("up") > -1``.

    None when phyStatus is missing or N/A.
    """
    if phy is None:
        return None
    return "up" in phy.lower()


def _pon_link_state(
    link_sta: str | None, traffic: str | None, phy: str | None
) -> str | None:
    """Combine phyStatus, LinkSta and trafficstate into "up"/"down".

    Same rule as the page (sta-network.asp:1405, :1428): the link is only up
    when phyStatus is up (review S2); trafficstate alone never makes it up.
    A known down phy is down whatever the other fields say. With phyStatus
    unknown, LinkSta 0 or trafficstate down still mean down; otherwise the
    state is unknown (None). PonState is ignored: the page sets it to 'up'
    after the if/else whatever the link (ActiveEtherWan branch, :420).
    """
    phy_up = _pon_phy_up(phy)
    if phy_up is True:
        return "up"
    if phy_up is False:
        return "down"
    if link_sta == "0" or traffic == "down":
        return "down"
    return None


_GET_PONTYPE_RE = re.compile(
    r"function\s+get_pontype\s*\(\s*\)\s*\{(.*?)return\s+pon_type", re.S
)
_PON_STATE_ASSIGN_RE = re.compile(r"\bthis\.PonState\s*=\s*'((?:\\.|[^'\\])*)'")
_TRAFFIC_UP_IF_RE = re.compile(
    r"\bif\s*\(\s*'up'\s*==\s*this\.trafficstate\s*\)"
)
# get_pontype() result -> ENUM option (translations carry the page text).
PON_LINK_TYPES = {
    "GPON": "gpon",
    "EPON": "epon",
    "XG-PON": "xg_pon",
    "XGS-PON": "xgs_pon",
    "10G-EPON": "10g_epon",
    "GE": "ge",
    "10GE": "10ge",
    "GE/10GE": "ge_10ge",
}
PON_ONLINE_STATES = ("online", "connecting", "offline")


def page_pon_type(
    phy_status: str | None,
    pon_mode: str | None,
    sfp_choose: str | None,
    active_ether_wan: str | None,
    ae_wan_speed: str | None,
) -> str:
    """Return what sta-network.asp get_pontype() prints (:1054-1123).

    Arguments are the raw page strings; None stands for a value the page
    does not render (JS ``undefined``). Like the JS, a missing PonMode
    (only rendered on isCTJOYME4Supported builds) is "not 0/1/2" and gives
    10G-EPON; the entity guards against that (``parse_uplink_pon``).
    """
    pon_type = "GPON"
    if phy_status in ("gpon_phy_up", "gpon_phy_down"):
        pon_type = "GPON"
    elif phy_status in ("epon_phy_up", "epon_phy_down"):
        pon_type = "EPON"
    if pon_mode not in ("1", "2", "0"):
        if pon_mode == "6":
            pon_type = "XG-PON"
        elif pon_mode == "7":
            pon_type = "XGS-PON"
        elif pon_mode == "5":
            pon_type = "EPON"
        else:
            pon_type = "10G-EPON"
    if phy_status == "down":
        pon_type = {
            "1": "GPON",
            "2": "EPON",
            "3": "10G-EPON",
            "4": "10G-EPON",
            "5": "EPON",
            "6": "XG-PON",
            "7": "XGS-PON",
        }.get(pon_mode or "", pon_type)
    if sfp_choose == "Yes" and active_ether_wan == "Yes" and pon_mode == "3":
        if ae_wan_speed == "1000M":
            pon_type = "GE"
        elif ae_wan_speed == "10000M":
            pon_type = "10GE"
        else:
            pon_type = "GE/10GE"
    return pon_type


def _js_block_end(text: str, start: int) -> int | None:
    """Return the index after the ``{...}`` block starting at/after ``start``.

    Skips whitespace to the opening brace; quoted strings are ignored when
    counting braces. None when there is no block.
    """
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "{":
        return None
    depth = 0
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _page_pon_state(body: str, traffic: str | None) -> str | None:
    """Return PonInfo.PonState as the page JS leaves it.

    ``this.PonState`` starts as 'down', becomes 'up' inside
    ``if ('up' == this.trafficstate) {...}``, and on ActiveEtherWan builds
    is then overwritten after the if/else with WanInfo_Common.EthernetState
    (sta-network.asp:348, :407-421). So: the last assignment after the
    if/else wins, else the if-branch one when trafficstate is up, else the
    initial one. Without that if/else, the last assignment.
    """
    assigns = [(m.start(), m.group(1)) for m in _PON_STATE_ASSIGN_RE.finditer(body)]
    if not assigns:
        return None
    match = _TRAFFIC_UP_IF_RE.search(body)
    if_end = _js_block_end(body, match.end()) if match is not None else None
    if match is None or if_end is None:
        return assigns[-1][1]
    after = if_end
    else_match = re.match(r"\s*else\b", body[if_end:])
    if else_match is not None:
        else_start = if_end + else_match.end()
        else_end = _js_block_end(body, else_start)
        if else_end is None:
            # Single-statement else: up to the end of that statement.
            semicolon = body.find(";", else_start)
            else_end = semicolon + 1 if semicolon >= 0 else else_start
        after = else_end
    before = [value for pos, value in assigns if pos < match.start()]
    state = before[-1] if before else None
    if traffic == "up":
        inside = [value for pos, value in assigns if match.end() <= pos < if_end]
        if inside:
            state = inside[-1]
    tail = [value for pos, value in assigns if pos >= after]
    if tail:
        state = tail[-1]
    return state


def _pon_online_status(pon_state: str | None) -> str | None:
    """Page line "PON link status" (sta-network.asp:1280-1310)."""
    if pon_state is None:
        return None
    if pon_state == "up":
        return "online"
    if pon_state == "connecting":
        return "connecting"
    return "offline"


def parse_uplink_pon(text: str) -> RltechUplinkPon | None:
    """Parse the uplink PON link section of sta-network.asp.

    Reads the literals assigned in ``function PonInfoClass()`` (first
    assignment wins) and applies the page formulas: voltage SupplyVoltage/10
    mV, bias TxBiasCurrent*2/1000 mA, transTemperature, 10*log10(x/10000)
    dBm. Pitfalls handled (stage 6 task): PonState is not used, and with
    LinkSta 'N/A' the page still computes -40 dBm from TxPower '1'; optical
    power is only reported while the link is up. Module readings need a
    non-zero supply voltage. Returns None when the page has no PonInfoClass.
    """
    match = _PON_INFO_RE.search(text)
    if match is None:
        return None
    values: dict[str, str] = {}
    for name, value in _PON_ASSIGN_RE.findall(match.group(1)):
        values.setdefault(name, value)
    if not values:
        return None
    link_sta = _pon_raw(values, "LinkSta")
    traffic = (_pon_raw(values, "trafficstate") or "").lower() or None
    phy = _pon_raw(values, "phyStatus")
    link_state = _pon_link_state(link_sta, traffic, phy)

    # Registration row of the page (sta-network.asp:1428-1433): phy up and
    # traffic up = registered and authenticated, phy up and traffic down =
    # registered only, anything else = not registered.
    if link_state == "up":
        registration = "authenticated" if traffic == "up" else "unauthenticated"
    elif link_state == "down":
        registration = "unregistered"
    else:
        registration = None

    loid_auth = _pon_raw(values, "loidAuth")
    loid_v1 = values.get("loidV1", "")
    if traffic == "up":
        loid_status = "up"
    elif link_sta != "0" and loid_auth == "2" and loid_v1:
        loid_status = "error"
    else:
        loid_status = "init"

    supply = _pon_number(values, "SupplyVoltage")
    module = supply is not None and supply > 0
    bias = _pon_number(values, "TxBiasCurrent") if module else None
    temperature = _pon_number(values, "Temperature") if module else None
    link_up = link_state == "up"
    pon_type = _pon_raw(values, "ponType")

    # Link type (get_pontype) and online status (PonState), as the page.
    pontype_match = _GET_PONTYPE_RE.search(text)
    pontype_vars: dict[str, str] = {}
    if pontype_match is not None:
        for name, value in _PON_ASSIGN_RE.findall(pontype_match.group(1)):
            pontype_vars.setdefault(name, value)
    phy_raw = values.get("phyStatus")
    mode_raw = values.get("PonMode")
    page_link_type = page_pon_type(
        phy_raw,
        mode_raw,
        pontype_vars.get("is_up_link_sfp_choose"),
        pontype_vars.get("is_active_tther_wan"),
        pontype_vars.get("ae_wan_speed"),
    )
    # Guard: without phyStatus, PonMode (non-JOYME4 builds) or the rendered
    # get_pontype body the JS falls back to 10G-EPON or ignores the SFP
    # choice; the entity shows unknown instead of that artefact.
    # An empty or N/A PonMode is the same JS artefact as a missing one
    # (review N4).
    link_type = (
        PON_LINK_TYPES.get(page_link_type)
        if pontype_match is not None
        and phy_raw is not None
        and _pon_raw(values, "PonMode") is not None
        else None
    )
    # The page compares trafficstate case-sensitively (review N4).
    pon_state = _page_pon_state(match.group(1), values.get("trafficstate"))
    return RltechUplinkPon(
        link_state=link_state,
        registration_state=registration,
        loid_status=loid_status,
        pon_type=pon_type if pon_type in {"GPON", "EPON"} else None,
        link_sta=link_sta,
        traffic_state=traffic,
        phy_status=phy,
        fec_state=_pon_raw(values, "fecState"),
        pon_mode=_pon_raw(values, "PonMode"),
        tx_power=_pon_dbm(_pon_number(values, "TxPower")) if link_up else None,
        rx_power=_pon_dbm(_pon_number(values, "RxPower")) if link_up else None,
        temperature=_pon_temperature(temperature),
        voltage=round(supply / 10000, 3) if module and supply is not None else None,
        bias_current=round(bias * 2 / 1000, 3) if bias is not None else None,
        pon_state=pon_state,
        online_status=_pon_online_status(pon_state),
        link_type=link_type,
        page_link_type=page_link_type,
        uplink_sfp_choose=pontype_vars.get("is_up_link_sfp_choose"),
        active_ether_wan=pontype_vars.get("is_active_tther_wan"),
        ae_wan_speed=pontype_vars.get("ae_wan_speed"),
    )


def _age_uplink_pon(
    pon: RltechUplinkPon | None,
    updated: datetime | None,
    *,
    now: datetime,
    status_interval: int,
) -> tuple[RltechUplinkPon | None, datetime | None]:
    """Drop uplink PON values older than UPLINK_PON_MAX_STATUS_CYCLES.

    A fetch failure, a timeout or a page without PonInfoClass (parse None,
    e.g. after a firmware change) keeps the last values only this long.
    Values without a timestamp (older snapshots) are kept until the next
    successful parse sets one; status_interval <= 0 disables the ageing.
    """
    if pon is None or updated is None or status_interval <= 0:
        return pon, updated
    max_age = UPLINK_PON_MAX_STATUS_CYCLES * status_interval
    if (now - updated).total_seconds() > max_age:
        return None, updated
    return pon, updated


def status_without_readings(
    status: RltechOltStatus | None,
) -> RltechOltStatus | None:
    """Return the status with only the AC identity fields left.

    Readings (temperatures, usage, uptime, boot and link times, PON
    counters) become None, so their sensors show unknown; the identity used
    for the device, the unique_id check and the endpoint resolver stays.
    """
    if status is None:
        return None
    cleared = {
        item.name: None
        for item in fields(status)
        if item.name not in WEB_STATUS_IDENTITY_FIELDS
        and getattr(status, item.name) is not None
    }
    return replace(status, **cleared) if cleared else status


def _age_web_status(
    status: RltechOltStatus | None,
    parsed: datetime | None,
    *,
    now: datetime,
    status_interval: int,
) -> RltechOltStatus | None:
    """Drop status readings older than WEB_STATUS_MAX_STATUS_CYCLES.

    Like _age_uplink_pon: failed, timed-out or unparsable sta-device and
    sta-user reads keep the last readings only this long. Without a
    timestamp (older snapshots) nothing is dropped; status_interval <= 0
    disables the ageing.
    """
    if status is None or parsed is None or status_interval <= 0:
        return status
    if (now - parsed).total_seconds() > WEB_STATUS_MAX_STATUS_CYCLES * status_interval:
        return status_without_readings(status)
    return status


def clear_status_readings(data: RltechData) -> RltechData:
    """Forget the AC status readings of a snapshot (after an AC reboot).

    Used when an AC reboot was sent: the old uptime, boot time and CPU
    values must not be shown for the next status interval. The status pages
    also become due, so the first poll that answers reads them again. The
    port-80 sources' own last values are cleared too, so a later port-80
    fallback cannot bring them back (review 7 finding 1).
    """
    return replace(
        data,
        web_status=status_without_readings(data.web_status),
        olt_status=status_without_readings(data.olt_status),
        legacy_sources={
            host: replace(source, olt_status=status_without_readings(source.olt_status))
            for host, source in data.legacy_sources.items()
        },
        web_status_update=None,
    )


def normalize_sn(value: Any) -> str | None:
    """Normalize RLTech AP/ONU serials for joining across Web UIs."""
    text = _text(value)
    if text is None:
        return None
    clean = re.sub(r"[^0-9A-Za-z]", "", text).upper()
    return clean or None


def _parse_js_call_args(text: str, function_name: str) -> list[tuple[Any, ...]]:
    """Parse simple JavaScript function call argument lists."""
    rows: list[tuple[Any, ...]] = []
    for args_text in re.findall(
        rf"(?<!function\s)\b{re.escape(function_name)}\s*\((.*?)\)\s*;",
        text,
        re.S,
    ):
        try:
            parsed = ast.literal_eval("(" + args_text.strip() + ",)")
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed, tuple):
            rows.append(parsed)
    return rows


def parse_legacy_runinfo(text: str, *, now: datetime | None = None) -> RltechOltStatus:
    """Parse OLT hardware status from the legacy runinfo.asp page."""
    now = now or datetime.now(UTC)
    for row in _parse_js_call_args(text, "X"):
        if len(row) != 12 or str(row[0]) == "ProName":
            continue
        system_uptime = _none_if_na(row[8])
        duration = _duration(system_uptime)
        return RltechOltStatus(
            system_uptime=system_uptime,
            last_boot=now - duration if duration is not None else None,
            gateway_type=_none_if_na(row[0]),
            cpu_usage=_float(row[9]),
            memory_usage=_float(row[10]),
            manufacturer="RLTech",
            serial_number=_none_if_na(row[11]),
            software_version=_none_if_na(row[1]),
        )
    raise UnexpectedResponse("runinfo.asp contains no OLT status row")


def parse_legacy_lan_ports(text: str) -> dict[int, RltechLanPort]:
    """Parse LAN link rows from the legacy lan_info.asp page."""
    ports: dict[int, RltechLanPort] = {}
    for row in _parse_js_call_args(text, "showPortInfo"):
        if len(row) != 6:
            continue
        label = _none_if_na(row[0])
        if label is None:
            continue
        port = _legacy_port_number(label)
        connected = str(row[1]).strip() == "1"
        mode = _none_if_na(row[2])
        if mode == "Full":
            mode = "Full-Duplex"
        elif mode == "Half":
            mode = "Half-Duplex"
        ports[port] = RltechLanPort(
            port=port,
            label=label,
            status="connected" if connected else "disconnected",
            connected=connected,
            rate=_none_if_na(row[3]),
            mode=mode,
            tx_bytes=_int(row[4]),
            rx_bytes=_int(row[5]),
        )
    return ports


def parse_legacy_lanpon_ports(text: str) -> dict[int, RltechLanPonPort]:
    """Parse LAN-PON optical rows from the legacy lanpon_info.asp page."""
    ports: dict[int, RltechLanPonPort] = {}
    for row in _parse_js_call_args(text, "showLANPonInfo"):
        if len(row) != 10:
            continue
        label = _none_if_na(row[0])
        if label is None:
            continue
        ponid = _legacy_pon_number(label)
        if ponid is None:
            continue
        status = _none_if_na(row[4])
        ports[ponid] = RltechLanPonPort(
            ponid=ponid,
            status=status,
            active=_none_if_na(row[2]),
            fec=_none_if_na(row[1]),
            autoregister=_none_if_na(row[3]),
            tx_power=_float(_strip_units(row[5])),
            rx_power=_lanpon_rx_power(row[6], status),
            temperature=_float(_strip_units(row[7])),
            voltage=_float(_strip_units(row[8])),
            current=_float(_strip_units(row[9])),
        )
    return ports


_LEGACY_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_legacy_datetime(
    value: Any,
    *,
    local_timezone: tzinfo = UTC,
) -> datetime | None:
    """Parse legacy OLT timestamps such as 'Fri Aug 21 06:12:26 2026'."""
    text = _none_if_na(value)
    if text is None:
        return None
    match = re.fullmatch(
        r"[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+"
        r"(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})",
        text,
    )
    if not match:
        return None
    month = _LEGACY_MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return datetime(
            int(match.group(6)),
            month,
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            tzinfo=local_timezone,
        )
    except ValueError:
        return None


def parse_legacy_onu_mgmt(
    text: str,
    *,
    source_host: str | None = None,
    local_timezone: tzinfo = UTC,
) -> dict[str, RltechApDetail]:
    """Parse AP/ONU optical rows from the legacy onu_mgmt.asp page."""
    rows: dict[str, RltechApDetail] = {}
    for row in _parse_js_call_args(text, "R"):
        if len(row) != 8:
            continue
        sn = normalize_sn(row[1])
        if sn is None:
            continue
        tx_power, rx_power = _legacy_optical_pair(row[3])
        interface = _none_if_na(row[0])
        pon_id = onu_id = None
        if interface:
            parts = [_int(part) for part in interface.split("/")]
            if len(parts) > 1:
                pon_id = parts[1]
            if len(parts) > 2:
                onu_id = parts[2]
        rows[sn] = RltechApDetail(
            sn=sn,
            pon_sn=sn,
            interface=interface,
            pon_id=pon_id,
            onu_id=onu_id,
            optical_tx_power=tx_power,
            optical_rx_power=rx_power,
            onu_status=_none_if_na(row[4]),
            register_status=_none_if_na(row[5]),
            reg_off_time=_parse_legacy_datetime(
                row[2],
                local_timezone=local_timezone,
            ),
            last_down_cause=_none_if_na(row[7]),
            source_host=source_host,
        )
    return rows


def _legacy_port_number(label: str) -> int:
    if match := re.fullmatch(r"LANPON(\d+)", label, re.I):
        return 4 + int(match.group(1))
    if match := re.fullmatch(r"LAN-(\d+)", label, re.I):
        return int(match.group(1))
    return _int(label) or 0


def _legacy_pon_number(label: str) -> int | None:
    if match := re.fullmatch(r"LANPON(\d+)", label, re.I):
        return int(match.group(1))
    return _int(label)


def _strip_units(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return (
        text.replace("dBm", "")
        .replace("℃", "")
        .replace("mA", "")
        .replace("V", "")
        .strip()
    )


def _legacy_optical_pair(value: Any) -> tuple[float | None, float | None]:
    text = _text(value)
    if text is None or "/" not in text or text.upper() == "N/A":
        return None, None
    tx_text, rx_text = text.split("/", 1)
    return _float(_strip_units(tx_text)), _float(_strip_units(rx_text))


def normalize_snapshot(
    ap_payloads: Iterable[dict[str, Any]],
    station_payloads: Iterable[dict[str, Any]],
    *,
    olt_html: str | None,
    user_html: str | None = None,
    previous: RltechData | None = None,
    now: datetime | None = None,
    station_retention: int = 3600,
    poll_duration_ms: int | None = None,
    ap_details: dict[str, RltechApDetail] | None = None,
) -> RltechData:
    """Normalize AP, station, and optional OLT responses into HA-friendly data."""
    now = now or datetime.now(UTC)

    aps = _normalize_ap_payloads(ap_payloads)

    stations: dict[str, RltechStation] = {}
    for payload in station_payloads:
        for row in _payload_rows(payload):
            station = normalize_station(row, now=now, aps=aps)
            if station is not None:
                if previous is not None and station.mac in previous.stations:
                    station = replace(
                        station,
                        first_seen=previous.stations[station.mac].first_seen
                        or station.first_seen,
                    )
                stations[station.mac] = station

    if previous is not None:
        for mac, old in previous.stations.items():
            if mac in stations:
                continue
            if old.last_seen is not None:
                age = (now - old.last_seen).total_seconds()
                if age >= station_retention:
                    continue
                stations[mac] = old

    olt_status = parse_olt_status(olt_html, now=now) if olt_html is not None else None

    if ap_details is None and previous is not None:
        ap_details = {
            mac: detail for mac, detail in previous.ap_details.items() if mac in aps
        }

    return RltechData(
        aps=aps,
        ap_details=ap_details or {},
        stations=stations,
        olt_status=_stabilize_olt_status(olt_status, previous),
        lan_ports=parse_lan_ports(user_html) if user_html is not None else {},
        lanpon_ports=parse_lanpon_ports(user_html) if user_html is not None else {},
        last_success=now,
        last_success_8080=now,
        poll_duration_ms=poll_duration_ms,
    )


def _join_legacy_ap_details(
    aps: dict[str, RltechAp],
    details_by_sn: dict[str, RltechApDetail],
    previous: RltechData | None,
    *,
    now: datetime,
) -> dict[str, RltechApDetail]:
    """Join legacy ONU rows to managed APs by AP serial."""
    previous_details = previous.ap_details if previous is not None else {}
    joined = {
        mac: detail
        for mac, detail in previous_details.items()
        if mac in aps
    }
    for mac, ap in aps.items():
        sn = normalize_sn(ap.sn)
        if sn is None:
            continue
        detail = details_by_sn.get(sn)
        if detail is None:
            continue
        old = previous_details.get(mac)
        joined[mac] = replace(
            detail,
            mac=mac,
            ip=ap.ip,
            model=ap.model,
            version=ap.version,
            online=ap.online,
            profile=ap.profile,
            alias=ap.alias,
            sn=ap.sn,
            dev_sn=ap.dev_sn,
            uplink=ap.uplink,
            uplink_port=ap.uplink_port,
            assoc_count=ap.assoc_count,
            last_update=now,
            sys_duration=old.sys_duration if old else detail.sys_duration,
            last_boot=old.last_boot if old else detail.last_boot,
            cpu_usage=old.cpu_usage if old else detail.cpu_usage,
            cpu_temperature=old.cpu_temperature if old else detail.cpu_temperature,
            memory_usage=old.memory_usage if old else detail.memory_usage,
            flash_usage=old.flash_usage if old else detail.flash_usage,
        )
    return joined


def _merge_olt_status(
    web: RltechOltStatus | None, legacy: RltechOltStatus | None
) -> RltechOltStatus | None:
    """Prefer 8080 controller status; fill gaps (CPU/memory) from port 80."""
    if web is None:
        return legacy
    if legacy is None:
        return web
    updates = {
        item.name: getattr(legacy, item.name)
        for item in fields(legacy)
        if getattr(web, item.name) is None and getattr(legacy, item.name) is not None
    }
    return replace(web, **updates) if updates else web


# Detail bookkeeping fields that always follow the latest 8080 attempt, even
# when the new value is None (for example a cleared detail_error).
_WEB_DETAIL_STATUS_FIELDS = frozenset(
    {"detail_source", "detail_error", "web_detail_update"}
)


def _overlay_ap_detail(
    base: RltechApDetail | None,
    overlay: RltechApDetail,
    *,
    always: frozenset[str] = frozenset(),
) -> RltechApDetail:
    """Return base updated with every non-empty overlay field."""
    if base is None:
        return overlay
    updates = {
        item.name: getattr(overlay, item.name)
        for item in fields(overlay)
        if getattr(overlay, item.name) is not None or item.name in always
    }
    return replace(base, **updates)


def _merge_ap_details(
    aps: dict[str, RltechAp],
    *,
    previous: RltechData | None,
    legacy: dict[str, RltechApDetail],
    web: dict[str, RltechApDetail],
) -> dict[str, RltechApDetail]:
    """Merge AP details field by field for the APs in the current inventory.

    Start from the last known detail, then apply fresh port-80 values, then
    fresh 8080 values, so that a value refreshed this poll wins and 8080 wins
    over legacy when both refreshed the same field.
    """
    previous_details = previous.ap_details if previous is not None else {}
    merged: dict[str, RltechApDetail] = {}
    for mac in aps:
        detail = previous_details.get(mac)
        legacy_detail = legacy.get(mac)
        if legacy_detail is not None:
            detail = _overlay_ap_detail(
                detail, replace(legacy_detail, detail_source="legacy")
            )
        web_detail = web.get(mac)
        if web_detail is not None:
            detail = _overlay_ap_detail(
                detail, web_detail, always=_WEB_DETAIL_STATUS_FIELDS
            )
        if detail is not None:
            merged[mac] = detail
    return merged


class RltechClient:
    """Minimal async client for the RLTech OLT Web UI."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        legacy_base_urls: Sequence[str] | None = None,
        legacy_username: str | None = None,
        legacy_password: str | None = None,
        timeout: int = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.legacy_base_urls = [url.rstrip("/") for url in legacy_base_urls or []]
        self.legacy_username = legacy_username or ""
        self.legacy_password = legacy_password or ""
        self.timeout = timeout
        self.token: str | None = None
        self._lock = asyncio.Lock()
        self._ap_detail_cursor = 0
        self.legacy_timeout = LEGACY_REQUEST_TIMEOUT
        self.legacy_breakers: dict[str, CircuitBreaker] = {}
        self.legacy_states: dict[str, SourceState] = {}
        self.last_web_attempted = False
        self.last_web_error: Exception | None = None
        # Set when a request in the current burst timed out or found the
        # session expired: no further requests may use this token, only the
        # logout in finally (boa expires a token idle for more than 10 s).
        self._burst_unsafe = False
        self.last_burst_ms: int | None = None
        # AP task rows from the last AP list page (None: not parsed).
        self.last_ap_tasks: dict[str, RltechApTask] | None = None
        # AP reboots requested through the AC, by AP MAC (in memory only).
        self.reboot_records: dict[str, RltechApRebootRecord] = {}
        self.last_reboot: RltechApRebootRecord | None = None
        # Last AC reboot request (mag-reset.asp), in memory only.
        self.last_ac_reboot: RltechAcRebootRecord | None = None
        # Monotonic clock for the reboot submit guard (replaceable in tests).
        self._clock = time.monotonic
        self.ap_detail_stats: dict[str, Any] = {
            "attempts": 0,
            "success": 0,
            "partial": 0,
            "fetch_errors": 0,
            "parse_errors": 0,
            "budget_skipped": 0,
            "aborted": 0,
            "last_error": None,
        }

    async def _login_to_base_url(
        self,
        session: Any,
        base_url: str,
        username: str,
        password: str,
    ) -> str:
        """Authenticate to one ecntToken Web UI base URL and return its token."""
        base_url = base_url.rstrip("/")
        fields = [
            ("username", username),
            ("password", password),
            ("Language_Flag", "0"),
            ("selectLanguage", "English"),
        ]
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": "EBOOVALUE=ecntBaorga; loginTimes=0",
            "Origin": base_url,
            "Referer": f"{base_url}/cgi-bin/login.asp",
            "X-Requested-With": "XMLHttpRequest",
        }
        async with session.post(
            f"{base_url}/cgi-bin/check_auth.json",
            data=fields,
            headers=headers,
            allow_redirects=False,
            timeout=_client_timeout(self.timeout),
        ) as response:
            text = await response.text()
            if response.status != 200:
                # Not a credentials problem (proxy page, wrong service): do
                # not trigger reauth.
                raise UnexpectedResponse(f"login HTTP status {response.status}")
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UnexpectedResponse("login did not return JSON") from exc

        if str(result.get("Locked", "0")) == "1":
            raise AccountLockedError("Web login is locked")
        if str(result.get("Logged", "0")) != "0":
            raise AccountBusyError("account is already logged into the Web UI")
        if str(result.get("Privilege", "0")) == "0":
            raise AuthenticationError("invalid username/password or no privilege")
        if str(result.get("Active", "0")) != "1":
            raise AuthenticationError("account is not active")

        token = str(result.get("ecntToken", ""))
        if not token or set(token) == {"0"}:
            raise AuthenticationError("login returned no usable token")
        return token

    async def login(self, session: Any) -> None:
        """Authenticate and retain the returned token."""
        self.token = await self._login_to_base_url(
            session,
            self.base_url,
            self.username,
            self.password,
        )

    async def logout(self, session: Any, *, timeout: float | None = None) -> None:
        """Log out the retained Web UI token.

        The token is dropped before the request so it is used at most once:
        after a failed logout the firmware releases the session by itself
        after 10 s idle, while re-sending a stale token can make boa delete
        every ecntToken, logging out a person using the Web UI
        (git_FTTR boa-asp ctc_auth_api.c:1027-1045).
        """
        if self.token is None:
            return
        token = self.token
        self.token = None
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Cookie": f"ecntToken={token}",
            "Referer": f"{self.base_url}/cgi-bin/sta-device.asp",
        }
        async with session.get(
            f"{self.base_url}/cgi-bin/logout.cgi",
            headers=headers,
            allow_redirects=False,
            timeout=_client_timeout(timeout if timeout is not None else self.timeout),
        ) as response:
            await response.read()
            if response.status != 200:
                raise UnexpectedResponse(f"logout HTTP status {response.status}")

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Hold the 8080 session lock (serialize with the polling burst).

        Used by the reconfigure flow so its identity check never collides
        with this entry's own poll on the single shared Web UI login.
        """
        async with self._lock:
            yield

    async def async_close(self, session: Any, *, timeout: float = 10) -> None:
        """Log out after any in-flight 8080 burst has released the lock.

        Used on unload and Home Assistant stop. Taking the lock first means
        the logout never races an active burst (review N1); fetch_snapshot
        already logs out in its finally block, so this is usually a no-op.
        """
        async with asyncio.timeout(timeout):
            async with self._lock:
                await self.logout(session)

    def _require_token(self) -> str:
        if self.token is None:
            raise SessionExpired("not authenticated")
        return self.token

    async def fetch_device_html(
        self, session: Any, *, timeout: float | None = None
    ) -> str:
        """Fetch the optional OLT/controller status page."""
        token = self._require_token()
        url = f"{self.base_url}/cgi-bin/sta-device.asp"
        async with session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": f"ecntToken={token}",
                "Referer": url,
            },
            allow_redirects=False,
            timeout=_client_timeout(timeout if timeout is not None else self.timeout),
        ) as response:
            text = await response.text(errors="replace")
            if 300 <= response.status < 400:
                raise SessionExpired("device-status request redirected")
            if response.status != 200:
                raise UnexpectedResponse(f"device-status HTTP status {response.status}")
        if "check_auth.json" in text and "username" in text:
            raise SessionExpired("device-status request returned login page")
        return text

    async def fetch_user_html(
        self, session: Any, *, timeout: float | None = None
    ) -> str:
        """Fetch LAN and LAN-PON status from sta-user.asp."""
        token = self._require_token()
        url = f"{self.base_url}/cgi-bin/sta-user.asp"
        async with session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": f"ecntToken={token}",
                "Referer": url,
            },
            allow_redirects=False,
            timeout=_client_timeout(timeout if timeout is not None else self.timeout),
        ) as response:
            text = await response.text(errors="replace")
            if 300 <= response.status < 400:
                raise SessionExpired("user-status request redirected")
            if response.status != 200:
                raise UnexpectedResponse(f"user-status HTTP status {response.status}")
        if "check_auth.json" in text and "username" in text:
            raise SessionExpired("user-status request returned login page")
        return text

    async def fetch_network_html(
        self, session: Any, *, timeout: float | None = None
    ) -> str:
        """Fetch sta-network.asp (uplink PON link section) with a plain GET.

        A GET carries no form, so the page's CATV commit block
        (``manual_commitflag``) never runs.
        """
        token = self._require_token()
        url = f"{self.base_url}/cgi-bin/sta-network.asp"
        async with session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": f"ecntToken={token}",
                "Referer": url,
            },
            allow_redirects=False,
            timeout=_client_timeout(timeout if timeout is not None else self.timeout),
        ) as response:
            text = await response.text(errors="replace")
            if 300 <= response.status < 400:
                raise SessionExpired("network-status request redirected")
            if response.status != 200:
                raise UnexpectedResponse(f"network-status HTTP status {response.status}")
        if "check_auth.json" in text and "username" in text:
            raise SessionExpired("network-status request returned login page")
        return text

    async def fetch_station_page(
        self,
        session: Any,
        *,
        page: int = 1,
        page_size: int = 10000,
    ) -> dict[str, Any]:
        """Fetch one station-list page."""
        token = self._require_token()
        fields = [
            ("pageidx_rows", f"{page},{page_size}"),
            ("filterkey_value", "Mac,"),
            ("search_item", "0"),
            ("search_condition", ""),
            ("auto_refresh", "0"),
            ("txtMaxRows", str(page_size)),
            ("txtCurPageIndex", str(page)),
            ("rebootToChangeMode", "Yes"),
        ]
        url = f"{self.base_url}/cgi-bin/ap_wlan_ac_client_list.asp"
        async with session.post(
            url,
            data=fields,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": f"ecntToken={token}; EBOOVALUE={eboo_value(fields)}",
                "Origin": self.base_url,
                "Referer": url,
            },
            allow_redirects=False,
            timeout=_client_timeout(self.timeout),
        ) as response:
            text = await response.text(errors="replace")
            if 300 <= response.status < 400:
                raise SessionExpired("station request redirected")
            if response.status != 200:
                raise UnexpectedResponse(f"station HTTP status {response.status}")
        return extract_embedded_json(text, "STA_manage")

    def _ap_list_fields(
        self,
        *,
        page: int = 1,
        page_size: int = 10000,
        upgrade_mac: str = "0",
        upgrade_action: str = "",
    ) -> list[tuple[str, str]]:
        """Return the ap_online_list.asp ConfigForm fields (fetch and reboot).

        One field set for both uses, in the order of the live form (phase 0,
        appendix A.5). ``delete`` is always AP_LIST_DELETE_NONE: the same
        page runs the XDel_AP hook (ap_online_list.asp:19), which only
        returns early for "0".
        """
        fields = [
            ("pageidx_rows", f"{page},{page_size}"),
            ("filtervalue", ""),
            ("filterkey_value", ""),
            ("upgrade_mac", upgrade_mac),
            ("upgrade_action", upgrade_action),
            ("delete", AP_LIST_DELETE_NONE),
            ("search_item", "0"),
            ("search_condition", ""),
            ("auto_refresh", "0"),
            ("txtMaxRows", str(page_size)),
            ("txtCurPageIndex", str(page)),
            ("get_value", "0"),
            ("save_value", "0"),
            ("click_num", "0"),
            ("rebootToChangeMode", "Yes"),
        ]
        # Hard stop, not just a default: never send anything that could
        # reach XDel_AP.
        if dict(fields)["delete"] != AP_LIST_DELETE_NONE:  # pragma: no cover
            raise AssertionError("ap_online_list.asp delete must stay '0'")
        return fields

    async def _post_ap_list(
        self,
        session: Any,
        fields: list[tuple[str, str]],
        *,
        what: str,
        timeout: float | None = None,
    ) -> str:
        """POST the AP list form and return the page text."""
        token = self._require_token()
        if dict(fields).get("delete") != AP_LIST_DELETE_NONE:
            raise AssertionError("ap_online_list.asp delete must stay '0'")
        url = f"{self.base_url}/cgi-bin/ap_online_list.asp"
        async with session.post(
            url,
            data=fields,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": f"ecntToken={token}; EBOOVALUE={eboo_value(fields)}",
                "Origin": self.base_url,
                "Referer": url,
            },
            allow_redirects=False,
            timeout=_client_timeout(timeout if timeout is not None else self.timeout),
        ) as response:
            text = await response.text(errors="replace")
            if 300 <= response.status < 400:
                raise SessionExpired(f"{what} request redirected")
            if response.status != 200:
                raise UnexpectedResponse(f"{what} HTTP status {response.status}")
        return text

    async def fetch_ap_page(
        self,
        session: Any,
        *,
        page: int = 1,
        page_size: int = 10000,
    ) -> dict[str, Any]:
        """Fetch one managed-AP inventory page.

        The same page embeds the AP task list (reboot status); it is kept in
        ``last_ap_tasks`` for the snapshot.
        """
        text = await self._post_ap_list(
            session,
            self._ap_list_fields(page=page, page_size=page_size),
            what="AP inventory",
        )
        payload = extract_embedded_json(text, "AP_manage")
        try:
            self.last_ap_tasks = parse_ap_task_list(text)
        except RltechError as err:
            _LOGGER.debug("Unable to parse the RLTech AP task list: %s", err)
            self.last_ap_tasks = None
        return payload

    async def fetch_ap_detail_html(
        self,
        session: Any,
        ap: RltechAp,
        *,
        timeout: float | None = None,
    ) -> str:
        """Fetch one managed-AP detail page."""
        token = self._require_token()
        if not ap.sn:
            raise UnexpectedResponse("AP detail request needs AP SN")
        mac_key = re.sub(r"[^0-9A-Fa-f]", "", ap.mac).upper()
        param1 = f"{mac_key}_{ap.sn}"
        url = f"{self.base_url}/cgi-bin/ap_online_detail.asp"
        async with session.get(
            f"{url}?param1={param1}&param2={ap.sn}",
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": f"ecntToken={token}",
                "Referer": f"{self.base_url}/cgi-bin/ap_online_list.asp",
            },
            allow_redirects=False,
            timeout=_client_timeout(timeout if timeout is not None else self.timeout),
        ) as response:
            text = await response.text(errors="replace")
            if 300 <= response.status < 400:
                raise SessionExpired("AP detail request redirected")
            if response.status != 200:
                raise UnexpectedResponse(f"AP detail HTTP status {response.status}")
        if "check_auth.json" in text and "username" in text:
            raise SessionExpired("AP detail request returned login page")
        return text

    async def reboot_ap_via_ac(
        self, session: Any, ap: RltechAp, *, now: datetime | None = None
    ) -> RltechApRebootRecord:
        """Reboot one managed AP through the AC (XAdd_Task, upgrade_action=2).

        One short 8080 burst under the session lock (after any running poll):
        login, re-read the AP list, check the AP is online and has no
        unfinished task (same checks as the Web UI op_reboot,
        ap_online_list.asp:503-545), submit the list form with only
        ``upgrade_mac``/``upgrade_action`` changed, logout. Success is the
        bare ``1`` the XAdd_Task hook prints into the page script.

        The outcome is kept in ``reboot_records``; errors are re-raised.
        """
        mac_key = re.sub(r"[^0-9A-Fa-f]", "", ap.mac).upper()
        mac = normalize_mac(mac_key)
        if mac is None:
            raise UnexpectedResponse("AP reboot needs a valid AP MAC")
        requested_at = now or datetime.now(UTC)
        record: RltechApRebootRecord | None = None
        try:
            async with self._lock:
                if self.token is not None:
                    _LOGGER.debug("Discarding stale RLTech Web UI token before reboot")
                    self.token = None
                self._burst_unsafe = False
                request_timeout = min(self.timeout, AP_DETAIL_REQUEST_TIMEOUT)
                try:
                    await self.login(session)
                    list_sent = self._clock()
                    text = await self._post_ap_list(
                        session,
                        self._ap_list_fields(),
                        what="AP inventory",
                        timeout=request_timeout,
                    )
                    current = _normalize_ap_payloads(
                        [extract_embedded_json(text, "AP_manage")]
                    ).get(mac)
                    if current is None:
                        raise ApRebootRejected("not_listed")
                    if current.online is not True:
                        raise ApRebootRejected("offline")
                    if ap_task_blocks_reboot(parse_ap_task_list(text).get(mac)):
                        raise ApRebootRejected("task_pending")
                    elapsed = self._clock() - list_sent
                    if elapsed > AP_REBOOT_SUBMIT_GUARD:
                        # Too close to boa's 10 s idle expiry: do not write,
                        # just log out (finally) and report a slow AC.
                        _LOGGER.debug(
                            "RLTech AP list took %.1f s; not submitting the reboot",
                            elapsed,
                        )
                        raise ApRebootRejected("slow")
                    text = await self._post_ap_list(
                        session,
                        self._ap_list_fields(
                            upgrade_mac=mac_key, upgrade_action=AP_REBOOT_ACTION
                        ),
                        what="AP reboot",
                        timeout=request_timeout,
                    )
                    task = None
                    with contextlib.suppress(RltechError):
                        task = parse_ap_task_list(text).get(mac)
                    if not xadd_task_accepted(text):
                        record = RltechApRebootRecord(
                            mac=mac,
                            requested_at=requested_at,
                            result="unconfirmed",
                            reboot_status=task.reboot_status if task else None,
                        )
                        raise UnexpectedResponse("the AC did not confirm the reboot task")
                    record = RltechApRebootRecord(
                        mac=mac,
                        requested_at=requested_at,
                        result="submitted",
                        reboot_status=task.reboot_status if task else None,
                    )
                    return record
                finally:
                    try:
                        await asyncio.shield(self.logout(session))
                    except Exception as exc:  # noqa: BLE001 - token already dropped
                        _LOGGER.debug("RLTech logout after reboot failed: %s", exc)
        except ApRebootRejected as err:
            record = RltechApRebootRecord(
                mac=mac,
                requested_at=requested_at,
                result=f"rejected_{err.reason}",
            )
            raise
        except Exception as err:
            if record is None:
                kind = error_kind(err)
                record = RltechApRebootRecord(
                    mac=mac,
                    requested_at=requested_at,
                    result=kind,
                    error=type(err).__name__,
                )
            raise
        finally:
            if record is not None:
                self.reboot_records[mac] = record
                self.last_reboot = record

    async def _post_ac_reboot(
        self,
        session: Any,
        fields: list[tuple[str, str]],
        *,
        timeout: float,
    ) -> tuple[int, str]:
        """POST the mag-reset.asp ResetForm (plain reboot only).

        Returns (HTTP status, body). Raises only what the request raised;
        the caller classifies every answer (review S1).
        """
        token = self._require_token()
        # Last line of defence: the body is checked right before it is sent.
        check_ac_reboot_fields(fields)
        url = f"{self.base_url}/cgi-bin/mag-reset.asp"
        async with session.post(
            url,
            data=fields,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                # setEBooValueCookie(document.ResetForm), as for XAdd_Task.
                "Cookie": f"ecntToken={token}; EBOOVALUE={eboo_value(fields)}",
                "Origin": self.base_url,
                "Referer": url,
            },
            allow_redirects=False,
            timeout=_client_timeout(timeout),
        ) as response:
            text = await response.text(errors="replace")
            return response.status, text

    async def reboot_ac(
        self,
        session: Any,
        *,
        now: datetime | None = None,
        on_submitted: Any = None,
    ) -> RltechAcRebootRecord:
        """Reboot the AC itself through mag-reset.asp (stage 6b).

        One short 8080 burst under the session lock (after any running poll):
        login, one POST of the whitelisted ResetForm body, logout in finally
        (shielded; expected to fail while the AC reboots, logged at debug).
        The POST is not idempotent and is never retried.

        Outcome (review S1/N2): a clean 200 page that is not the login page
        is ``submitted``. Once the body may be on the wire, anything else
        (3xx, login page, other status, timeout, dropped connection) is
        ``submitted_unconfirmed``: mag-reset.asp commits reboot_type before
        it renders, so the AC has probably accepted it. Both are returned
        and call ``on_submitted`` inside the lock, before the logout. A
        failure before anything was sent (login, busy, locked, connection
        refused / connect timeout) is recorded and re-raised; no window.
        The status and the first bytes of the answer (sanitised) are kept
        for diagnostics.
        """
        requested_at = now or datetime.now(UTC)
        record: RltechAcRebootRecord | None = None
        try:
            async with self._lock:
                if self.token is not None:
                    _LOGGER.debug("Discarding stale RLTech Web UI token before AC reboot")
                    self.token = None
                self._burst_unsafe = False
                try:
                    await self.login(session)
                    fields = ac_reboot_fields()
                    try:
                        status, text = await self._post_ac_reboot(
                            session,
                            fields,
                            timeout=min(self.timeout, AP_DETAIL_REQUEST_TIMEOUT),
                        )
                    except Exception as err:
                        if _ac_reboot_not_sent(err):
                            raise
                        _LOGGER.debug(
                            "RLTech AC reboot sent without an answer (%s); "
                            "not retrying",
                            type(err).__name__,
                        )
                        record = RltechAcRebootRecord(
                            requested_at=requested_at,
                            result="submitted_unconfirmed",
                            error=type(err).__name__,
                        )
                    else:
                        problem = _ac_reboot_answer_problem(status, text)
                        record = RltechAcRebootRecord(
                            requested_at=requested_at,
                            result="submitted_unconfirmed" if problem else "submitted",
                            error=problem,
                            http_status=status,
                            response_head=sanitize_response_head(text),
                        )
                        if problem:
                            _LOGGER.debug(
                                "RLTech AC reboot answer was %s; treating it as "
                                "submitted_unconfirmed",
                                problem,
                            )
                    if on_submitted is not None:
                        on_submitted()
                    return record
                finally:
                    try:
                        await asyncio.shield(
                            self.logout(session, timeout=AC_REBOOT_LOGOUT_TIMEOUT)
                        )
                    except Exception as exc:  # noqa: BLE001 - AC is rebooting
                        _LOGGER.debug(
                            "RLTech logout after the AC reboot failed "
                            "(expected while it reboots): %s",
                            type(exc).__name__,
                        )
        except Exception as err:
            if record is None:
                record = RltechAcRebootRecord(
                    requested_at=requested_at,
                    result=error_kind(err),
                    error=type(err).__name__,
                )
            raise
        finally:
            if record is not None:
                self.last_ac_reboot = record

    def reset_legacy_breakers(self) -> None:
        """Close every port-80 circuit breaker (after an AC reboot, 6b)."""
        for breaker in self.legacy_breakers.values():
            breaker.record_success()

    async def _fetch_all(
        self,
        fetch_page: Any,
        session: Any,
        *,
        page_size: int = 10000,
    ) -> list[dict[str, Any]]:
        pages = []
        page = 1
        seen = 0
        while True:
            payload = await fetch_page(session, page=page, page_size=page_size)
            pages.append(payload)
            rows = _payload_rows(payload)
            seen += len(rows)
            total = _payload_total(payload)
            if not rows or seen >= total:
                return pages
            page += 1

    def _ap_detail_due(
        self,
        aps: dict[str, RltechAp],
        previous: RltechData | None,
        *,
        now: datetime,
        scan_interval: int,
        detail_interval: int,
        force_all: bool = False,
    ) -> list[RltechAp]:
        """Return APs due for slow detail polling with a stable jitter.

        ``force_all`` returns every AP with a SN (after a pause, review S1).
        """
        if detail_interval <= 0:
            return []
        if force_all:
            return sorted((ap for ap in aps.values() if ap.sn), key=lambda ap: ap.sn)
        previous_details = previous.ap_details if previous is not None else {}
        polls_per_detail_interval = max(1, detail_interval // max(1, scan_interval))
        max_per_poll = max(
            1, (len(aps) + polls_per_detail_interval - 1) // polls_per_detail_interval
        )
        ordered_aps = sorted(
            (ap for ap in aps.values() if ap.sn),
            key=lambda ap: (
                zlib.crc32((ap.sn or ap.mac).encode("utf-8")),
                ap.sn or ap.mac,
            ),
        )
        # Only 8080 detail attempts count here: legacy and MQTT refresh
        # last_update every poll and would otherwise starve 8080 details.
        missing = [
            ap
            for ap in ordered_aps
            if previous_details.get(ap.mac) is None
            or previous_details[ap.mac].web_detail_update is None
        ]
        if missing:
            return self._ap_detail_batch(missing, max_per_poll)

        due: list[tuple[float, str, RltechAp]] = []
        for ap in ordered_aps:
            detail = previous_details.get(ap.mac)
            if detail is not None and detail.web_detail_update is not None:
                age = (now - detail.web_detail_update).total_seconds()
                if age < detail_interval:
                    continue
                priority = age
            else:
                priority = float(detail_interval)
            due.append((priority, ap.sn, ap))

        due.sort(key=lambda item: (-item[0], item[1]))
        return [ap for _, _, ap in due[:max_per_poll]]

    def _ap_detail_batch(self, aps: list[RltechAp], limit: int) -> list[RltechAp]:
        """Return a rotating AP detail batch."""
        if not aps or limit <= 0:
            return []
        start = self._ap_detail_cursor % len(aps)
        count = min(limit, len(aps))
        batch = [aps[(start + offset) % len(aps)] for offset in range(count)]
        self._ap_detail_cursor = (start + count) % len(aps)
        return batch

    async def _fetch_due_ap_details(
        self,
        session: Any,
        aps: dict[str, RltechAp],
        previous: RltechData | None,
        *,
        now: datetime,
        scan_interval: int,
        detail_interval: int,
        deadline: float | None = None,
        local_timezone: tzinfo = UTC,
        force_all: bool = False,
    ) -> dict[str, RltechApDetail]:
        """Fetch due AP detail pages inside the current 8080 session.

        Returns only the APs attempted this poll. A failed attempt yields a
        bookkeeping-only detail (detail_error, web_detail_update) so the AP is
        retried after detail_interval instead of on every poll; the caller
        merges it over the last known values. ``deadline`` is a
        time.monotonic() value bounding the whole 8080 burst.
        """
        previous_details = previous.ap_details if previous is not None else {}
        stats = self.ap_detail_stats
        details: dict[str, RltechApDetail] = {}
        due = self._ap_detail_due(
            aps,
            previous,
            now=now,
            scan_interval=scan_interval,
            detail_interval=detail_interval,
            force_all=force_all,
        )
        for index, ap in enumerate(due):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining < 1:
                stats["budget_skipped"] += len(due) - index
                _LOGGER.debug(
                    "RLTech 8080 burst budget exhausted; deferring %s AP detail(s)",
                    len(due) - index,
                )
                break
            stats["attempts"] += 1
            timeout: float = min(self.timeout, AP_DETAIL_REQUEST_TIMEOUT)
            if remaining is not None:
                timeout = min(timeout, remaining)
            try:
                text = await self.fetch_ap_detail_html(session, ap, timeout=timeout)
            except SessionExpired as err:
                # The shared session is gone; later detail requests would fail
                # the same way. Do not mark the AP as attempted.
                stats["fetch_errors"] += 1
                stats["last_error"] = f"session_expired: {err}"
                _LOGGER.debug("RLTech AP detail session expired at %s: %s", ap.mac, err)
                self._burst_unsafe = True
                break
            except Exception as err:  # noqa: BLE001 - keep polling other APs
                stats["fetch_errors"] += 1
                # Exception text can embed the detail URL (MAC/SN); keep only
                # the type so diagnostics stay redacted.
                stats["last_error"] = f"fetch_error: {type(err).__name__}"
                _LOGGER.debug(
                    "Unable to fetch AP detail for %s: %s: %s",
                    ap.mac,
                    type(err).__name__,
                    err,
                )
                timed_out = isinstance(err, (TimeoutError, asyncio.TimeoutError))
                details[ap.mac] = RltechApDetail(
                    mac=ap.mac,
                    detail_source="8080",
                    detail_error="timeout" if timed_out else "fetch_error",
                    web_detail_update=now,
                )
                if timed_out:
                    # boa may be stuck in popen(ubus); a further request would
                    # land more than 10 s after the last one and expire the
                    # session. Stop here; the caller still logs out.
                    stats["aborted"] += len(due) - index - 1
                    self._burst_unsafe = True
                    break
                continue
            try:
                detail = parse_ap_detail(
                    text, now=now, local_timezone=local_timezone
                )
            except Exception as err:  # noqa: BLE001 - list/stations stay valid
                stats["parse_errors"] += 1
                stats["last_error"] = f"parse_error: {err}"
                _LOGGER.debug(
                    "Unable to parse AP detail for %s: %s: %s",
                    ap.mac,
                    type(err).__name__,
                    err,
                )
                details[ap.mac] = RltechApDetail(
                    mac=ap.mac,
                    detail_source="8080",
                    detail_error="parse_error",
                    web_detail_update=now,
                )
                continue
            if detail.mac is not None and detail.mac != ap.mac:
                # The AC answered with another AP's row; never attribute it
                # to the requested AP.
                stats["parse_errors"] += 1
                stats["last_error"] = "mac_mismatch"
                _LOGGER.debug(
                    "Discarding AP detail for %s: page reports %s", ap.mac, detail.mac
                )
                details[ap.mac] = RltechApDetail(
                    mac=ap.mac,
                    detail_source="8080",
                    detail_error="mac_mismatch",
                    web_detail_update=now,
                )
                continue
            if detail.detail_error:
                stats["partial"] += 1
                stats["last_error"] = detail.detail_error
            else:
                stats["success"] += 1
            old = previous_details.get(ap.mac)
            details[ap.mac] = replace(
                detail,
                mac=ap.mac,
                last_update=now,
                web_detail_update=now,
                last_boot=_stable_datetime(
                    detail.last_boot, old.last_boot if old is not None else None
                ),
            )
        return details

    def _web_status_due(
        self, previous: RltechData | None, *, now: datetime, interval: int
    ) -> bool:
        """Return whether sta-device/sta-user should be read this poll."""
        if interval <= 0:
            return False
        if previous is None or previous.web_status_update is None:
            return True
        if self._ac_rebooted_since(previous.web_status_update):
            # First poll after an AC reboot request: read the new uptime.
            return True
        return (now - previous.web_status_update).total_seconds() >= interval

    def _read_before_ac_reboot(
        self, status: RltechOltStatus | None, now: datetime
    ) -> bool:
        """Return whether ``status`` was read before a just-requested reboot.

        Only inside AC_REBOOT_STALE_READ_WINDOW after a sent reboot request,
        and only when the page gave a boot time older than the request: the
        AC had not gone down yet when it was read (review 7 finding 2).
        """
        record = self.last_ac_reboot
        if (
            status is None
            or status.last_boot is None
            or record is None
            or record.result not in AC_REBOOT_SENT_RESULTS
        ):
            return False
        if (now - record.requested_at).total_seconds() > AC_REBOOT_STALE_READ_WINDOW:
            return False
        return status.last_boot < record.requested_at - AC_REBOOT_BOOT_SLACK

    def _ac_rebooted_since(self, when: datetime | None) -> bool:
        """Return whether an AC reboot was sent after ``when`` (or ever)."""
        record = self.last_ac_reboot
        if record is None or record.result not in AC_REBOOT_SENT_RESULTS:
            return False
        return when is None or record.requested_at > when

    async def _fetch_web_status(
        self,
        session: Any,
        previous: RltechData | None,
        *,
        now: datetime,
        deadline: float | None = None,
    ) -> tuple[
        RltechOltStatus | None,
        dict[int, RltechLanPort],
        dict[int, RltechLanPonPort],
        RltechUplinkPon | None,
        bool,
    ]:
        """Read controller status pages inside the current 8080 session.

        sta-device, sta-user, then sta-network (uplink PON). Returns (status,
        lan_ports, lanpon_ports, uplink_pon, attempted). Failures are logged
        at debug level only and the caller keeps the previous values; a
        timeout or an expired session ends the burst (no further request may
        use the token).
        """
        status: RltechOltStatus | None = None
        lan_ports: dict[int, RltechLanPort] = {}
        lanpon_ports: dict[int, RltechLanPonPort] = {}
        uplink_pon: RltechUplinkPon | None = None
        attempted = False
        for page in ("device", "user", "network"):
            timeout: float = min(self.timeout, AP_DETAIL_REQUEST_TIMEOUT)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    _LOGGER.debug("RLTech 8080 burst budget exhausted before %s page", page)
                    break
                timeout = min(timeout, remaining)
            attempted = True
            try:
                if page == "device":
                    text = await self.fetch_device_html(session, timeout=timeout)
                    status = parse_olt_status(text, now=now)
                elif page == "user":
                    text = await self.fetch_user_html(session, timeout=timeout)
                    lan_ports = parse_lan_ports(text)
                    lanpon_ports = parse_lanpon_ports(text)
                    lan_ip, lan_mac = parse_ac_lan_identity(text)
                    status = replace(
                        status or RltechOltStatus(), lan_ip=lan_ip, lan_mac=lan_mac
                    )
                else:
                    text = await self.fetch_network_html(session, timeout=timeout)
                    uplink_pon = parse_uplink_pon(text)
            except SessionExpired as err:
                _LOGGER.debug("RLTech %s status page session expired: %s", page, err)
                self._burst_unsafe = True
                break
            except Exception as err:  # noqa: BLE001 - optional controller status
                _LOGGER.debug(
                    "Unable to read RLTech %s status page: %s: %s",
                    page,
                    type(err).__name__,
                    err,
                )
                if isinstance(err, TimeoutError):
                    self._burst_unsafe = True
                    break
        previous_status = previous.web_status if previous is not None else None
        return (
            _stabilize_olt_status_against(status, previous_status),
            lan_ports,
            lanpon_ports,
            uplink_pon,
            attempted,
        )

    async def legacy_login(self, session: Any, base_url: str, ltime: int) -> None:
        """Log into the legacy port-80 UI."""
        fields = {
            "ltime": str(ltime),
            "interval": "0",
            "lang": "en",
            "role": "15",
            "word": self.legacy_username,
            "code": self.legacy_password,
        }
        async with session.post(
            f"{base_url}/goform/setLoginCfg",
            data=fields,
            headers=self._legacy_headers(base_url, ltime, f"{base_url}/login_en.asp"),
            allow_redirects=False,
            timeout=_client_timeout(self.legacy_timeout),
        ) as response:
            text = await response.text(errors="replace")
            location = response.headers.get("Location", "")
            if response.status not in {200, 302}:
                raise LegacyAuthenticationError(f"legacy login HTTP status {response.status}")
            if "error=1" in location or "error=1" in text:
                raise LegacyAuthenticationError("legacy username/password rejected")

    async def legacy_logout(self, session: Any, base_url: str, ltime: int) -> None:
        """Log out of the legacy port-80 UI."""
        fields = {
            "ltime": str(ltime),
            "language": "en",
        }
        async with session.post(
            f"{base_url}/goform/setLogoutCfg",
            data=fields,
            headers=self._legacy_headers(base_url, ltime, f"{base_url}/top.asp?ltime={ltime}"),
            allow_redirects=False,
            timeout=_client_timeout(self.legacy_timeout),
        ) as response:
            await response.read()

    async def legacy_fetch_html(
        self,
        session: Any,
        base_url: str,
        ltime: int,
        path: str,
    ) -> str:
        """Fetch one legacy port-80 page."""
        async with session.get(
            f"{base_url}{path}",
            headers=self._legacy_headers(base_url, ltime, f"{base_url}/index.asp"),
            allow_redirects=False,
            timeout=_client_timeout(self.legacy_timeout),
        ) as response:
            text = await response.text(errors="replace")
            if 300 <= response.status < 400:
                raise SessionExpired(
                    f"legacy request redirected to {response.headers.get('Location')}"
                )
            if response.status != 200:
                raise UnexpectedResponse(f"legacy HTTP status {response.status}")
        return text

    def _legacy_headers(self, base_url: str, ltime: int, referer: str) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": f"ltime={ltime}; interval=0; role=15",
            "Origin": base_url,
            "Referer": referer,
            "User-Agent": "Mozilla/5.0",
        }

    async def _fetch_legacy_source(
        self,
        session: Any,
        base_url: str,
        *,
        now: datetime,
        local_timezone: tzinfo = UTC,
    ) -> tuple[RltechOltStatus, dict[int, RltechLanPort], dict[int, RltechLanPonPort], dict[str, RltechApDetail]]:
        """Fetch one complete legacy port-80 source."""
        ltime = int(time.time())
        await self.legacy_login(session, base_url, ltime)
        try:
            runinfo_html = await self.legacy_fetch_html(
                session, base_url, ltime, f"/runinfo.asp?ltime={ltime}"
            )
            lan_html = await self.legacy_fetch_html(
                session, base_url, ltime, f"/lan_info.asp?ltime={ltime}"
            )
            lanpon_html = await self.legacy_fetch_html(
                session, base_url, ltime, f"/lanpon_info.asp?ltime={ltime}"
            )
            onu_details = await self._fetch_legacy_onu_details(
                session,
                base_url,
                ltime,
                local_timezone=local_timezone,
            )
            return (
                parse_legacy_runinfo(runinfo_html, now=now),
                parse_legacy_lan_ports(lan_html),
                parse_legacy_lanpon_ports(lanpon_html),
                onu_details,
            )
        finally:
            try:
                await asyncio.shield(self.legacy_logout(session, base_url, ltime))
            except Exception as err:  # noqa: BLE001 - legacy logout should not poison data
                _LOGGER.debug("RLTech legacy logout cleanup failed for %s: %s", base_url, err)

    async def _fetch_legacy_onu_details(
        self,
        session: Any,
        base_url: str,
        ltime: int,
        *,
        local_timezone: tzinfo = UTC,
    ) -> dict[str, RltechApDetail]:
        """Fetch all legacy ONU pages from one source."""
        details: dict[str, RltechApDetail] = {}
        for page in range(1, 21):
            html_text = await self.legacy_fetch_html(
                session,
                base_url,
                ltime,
                f"/onu_mgmt.asp?{urlencode({'page': page, 'port_id': 0, 'ltime': ltime})}",
            )
            page_details = parse_legacy_onu_mgmt(
                html_text,
                source_host=urlsplit(base_url).hostname or base_url,
                local_timezone=local_timezone,
            )
            if not page_details:
                break
            details.update(page_details)
        return details

    async def _fetch_legacy_snapshot(
        self,
        session: Any,
        aps: dict[str, RltechAp],
        previous: RltechData | None,
        *,
        now: datetime,
        local_timezone: tzinfo = UTC,
        join_previous_details: bool = True,
        status_max_age: float = WEB_STATUS_MAX_STATUS_CYCLES * 300,
    ) -> tuple[
        RltechOltStatus | None,
        dict[int, RltechLanPort],
        dict[int, RltechLanPonPort],
        dict[str, RltechApDetail],
        dict[str, RltechLegacyOltSource],
    ]:
        """Fetch and merge all configured legacy port-80 sources.

        With ``join_previous_details=False`` only the AP details refreshed by
        port 80 in this poll are returned, for field-level merging.
        """
        if not self.legacy_base_urls:
            return None, {}, {}, {}, {}
        olt_status = None
        lan_ports: dict[int, RltechLanPort] = {}
        lanpon_ports: dict[int, RltechLanPonPort] = {}
        by_sn: dict[str, RltechApDetail] = {}
        sources: dict[str, RltechLegacyOltSource] = {}
        source_error = False
        for index, base_url in enumerate(self.legacy_base_urls):
            host = urlsplit(base_url).hostname or base_url
            breaker = self.legacy_breakers.setdefault(host, CircuitBreaker())
            state = self.legacy_states.setdefault(host, SourceState(f"legacy:{host}"))
            if not breaker.allow(now):
                # Circuit open: no request, keep last good values.
                source_error = True
                continue
            try:
                source_status, source_lan, source_lanpon, source_onus = (
                    await self._fetch_legacy_source(
                        session,
                        base_url,
                        now=now,
                        local_timezone=local_timezone,
                    )
                )
            except Exception as err:  # noqa: BLE001 - keep other source / last good values
                source_error = True
                # Port 80 is an optional enhancement that is closed on the WAN
                # side by design (other_mgr.c:237-241): never warn about it.
                kind = error_kind(err)
                state.record_failure(kind, now, type(err).__name__)
                if breaker.record_failure(now):
                    _LOGGER.debug(
                        "RLTech port 80 on %s unavailable (%s); pausing it until %s",
                        host,
                        kind,
                        breaker.open_until,
                    )
                else:
                    _LOGGER.debug(
                        "Unable to fetch RLTech legacy status from %s: %s: %s",
                        base_url,
                        type(err).__name__,
                        err,
                    )
                continue
            breaker.record_success()
            if state.record_success(now):
                _LOGGER.info("RLTech port 80 status on %s is available again", host)
            previous_source = (
                previous.legacy_sources.get(host) if previous is not None else None
            )
            source_status = _stabilize_olt_status_against(
                source_status,
                previous_source.olt_status if previous_source is not None else None,
            )
            if index == 0:
                olt_status = source_status
                lan_ports = source_lan
                lanpon_ports = source_lanpon
            sources[host] = RltechLegacyOltSource(
                host=host,
                base_url=base_url,
                olt_status=source_status,
                lan_ports=source_lan,
                lanpon_ports=source_lanpon,
                last_success=now,
            )
            by_sn.update(source_onus)

        if source_error and previous is not None:
            # Fall back to port 80's own last values (the master source), never
            # to previous.olt_status/lan_ports: those are merged results that
            # already contain 8080 data and would freeze it (review M2). If
            # port 80 never succeeded there is nothing to fall back to.
            master_url = self.legacy_base_urls[0]
            master = previous.legacy_sources.get(
                urlsplit(master_url).hostname or master_url
            )
            if master is not None:
                master = self._aged_legacy_source(master, now=now, max_age=status_max_age)
                sources[master.host] = master
                olt_status = olt_status or master.olt_status
                if not lan_ports:
                    lan_ports = master.lan_ports
                if not lanpon_ports:
                    lanpon_ports = master.lanpon_ports
            for host, source in previous.legacy_sources.items():
                sources.setdefault(host, source)

        ap_details = _join_legacy_ap_details(
            aps, by_sn, previous if join_previous_details else None, now=now
        )
        return olt_status, lan_ports, lanpon_ports, ap_details, sources

    def _aged_legacy_source(
        self, source: RltechLegacyOltSource, *, now: datetime, max_age: float
    ) -> RltechLegacyOltSource:
        """Clear a port-80 source's stale readings before falling back to it.

        Same rule as the 8080 status (review 7 finding 1): readings older
        than ``max_age`` (2 status intervals), or taken before a sent AC
        reboot request, become None; the identity fields stay.
        """
        status = source.olt_status
        if status is None:
            return source
        last = source.last_success
        stale = (
            max_age > 0
            and last is not None
            and (now - last).total_seconds() > max_age
        ) or (last is not None and self._ac_rebooted_since(last))
        record = self.last_ac_reboot
        if (
            not stale
            and record is not None
            and record.result in AC_REBOOT_SENT_RESULTS
            and status.last_boot is not None
            and status.last_boot < record.requested_at - AC_REBOOT_BOOT_SLACK
        ):
            # Read after the request but before the AC went down.
            stale = True
        if not stale:
            return source
        return replace(source, olt_status=status_without_readings(status))

    async def fetch_snapshot(
        self,
        session: Any,
        *,
        previous: RltechData | None = None,
        station_retention: int = 3600,
        include_ap_inventory: bool = True,
        include_station_inventory: bool = True,
        include_hardware_status: bool = True,
        scan_interval: int = 60,
        local_timezone: tzinfo = UTC,
        detail_interval: int = 300,
        burst_budget: float = 25,
        force_all_details: bool = False,
        status_interval: int = 300,
    ) -> RltechData:
        """Fetch one snapshot, keeping 8080 inventory and port-80 status independent.

        All 8080 requests share one login: AP list, station list, then the AP
        detail pages that are due, then logout. Detail requests stop once the
        burst has run for ``burst_budget`` seconds; the rest wait for the next
        poll.
        """
        started = time.monotonic()
        now = datetime.now(UTC)
        ap_pages: list[dict[str, Any]] = []
        station_pages: list[dict[str, Any]] = []
        web_details: dict[str, RltechApDetail] = {}
        web_status_fresh: tuple[
            RltechOltStatus | None,
            dict[int, RltechLanPort],
            dict[int, RltechLanPonPort],
            RltechUplinkPon | None,
            bool,
        ] | None = None
        primary_error: Exception | None = None
        primary_attempted = include_ap_inventory or include_station_inventory

        burst_ms: int | None = None
        async with self._lock:
            # The lock covers only the 8080 session; port 80 runs afterwards so
            # a slow or unreachable legacy host never delays the Web UI burst
            # or an AP reboot waiting for the session.
            if primary_attempted:
                if self.token is not None:
                    # Never reuse a token across polls, not even for logout:
                    # the old session is idle past REQ_SHORT_TIMEOUT and
                    # expires on its own.
                    _LOGGER.debug("Discarding stale RLTech Web UI token before login")
                    self.token = None

                burst_started = time.monotonic()
                self._burst_unsafe = False
                self.last_ap_tasks = None
                try:
                    await self.login(session)
                    ap_pages = (
                        await self._fetch_all(self.fetch_ap_page, session)
                        if include_ap_inventory
                        else []
                    )
                    station_pages = (
                        await self._fetch_all(self.fetch_station_page, session)
                        if include_station_inventory
                        else []
                    )
                    if include_ap_inventory:
                        web_details = await self._fetch_due_ap_details(
                            session,
                            _normalize_ap_payloads(ap_pages),
                            previous,
                            now=now,
                            scan_interval=scan_interval,
                            detail_interval=detail_interval,
                            deadline=burst_started + burst_budget,
                            local_timezone=local_timezone,
                            force_all=force_all_details,
                        )
                    if self._burst_unsafe:
                        # Skip the status pages and log out right away; they
                        # stay due and are read on the next poll.
                        _LOGGER.debug(
                            "RLTech 8080 session unsafe; skipping status pages"
                        )
                    elif self._web_status_due(
                        previous, now=now, interval=status_interval
                    ):
                        web_status_fresh = await self._fetch_web_status(
                            session,
                            previous,
                            now=now,
                            deadline=burst_started + burst_budget,
                        )
                except AuthenticationError:
                    raise
                except Exception as exc:  # noqa: BLE001 - port 80 can still refresh
                    # State changes are logged once by the coordinator.
                    _LOGGER.debug(
                        "Unable to fetch RLTech 8080 inventory: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    primary_error = exc
                finally:
                    try:
                        await asyncio.shield(self.logout(session))
                    except Exception as exc:  # noqa: BLE001 - token already dropped
                        _LOGGER.debug("RLTech logout cleanup failed: %s", exc)
                    burst_ms = int((time.monotonic() - burst_started) * 1000)

        self.last_web_attempted = primary_attempted
        self.last_web_error = primary_error
        self.last_burst_ms = burst_ms

        aps = (
            _normalize_ap_payloads(ap_pages)
            if primary_error is None
            else previous.aps
            if previous is not None
            else {}
        )
        olt_status = None
        lan_ports: dict[int, RltechLanPort] = {}
        lanpon_ports: dict[int, RltechLanPonPort] = {}
        legacy_details: dict[str, RltechApDetail] = {}
        legacy_sources: dict[str, RltechLegacyOltSource] = {}
        if include_hardware_status:
            olt_status, lan_ports, lanpon_ports, legacy_details, legacy_sources = (
                await self._fetch_legacy_snapshot(
                    session,
                    aps,
                    previous,
                    now=now,
                    local_timezone=local_timezone,
                    join_previous_details=False,
                    status_max_age=WEB_STATUS_MAX_STATUS_CYCLES * status_interval,
                )
            )
        ap_details = _merge_ap_details(
            aps,
            previous=previous,
            legacy=legacy_details,
            web=web_details if primary_error is None else {},
        )
        legacy_success = any(
            source.last_success == now for source in legacy_sources.values()
        )

        if primary_error is None:
            data = normalize_snapshot(
                ap_pages,
                station_pages,
                olt_html=None,
                user_html=None,
                previous=previous,
                now=now,
                station_retention=station_retention if include_station_inventory else 0,
                poll_duration_ms=int((time.monotonic() - started) * 1000),
                ap_details=ap_details,
            )
            if not primary_attempted:
                data = replace(
                    data,
                    last_success=previous.last_success if previous else None,
                    last_success_8080=(
                        previous.last_success_8080 if previous else None
                    ),
                )
            if not include_station_inventory and previous is not None:
                data = replace(data, stations=previous.stations)
        elif previous is not None and legacy_success:
            data = replace(
                previous,
                poll_duration_ms=int((time.monotonic() - started) * 1000),
            )
        elif include_hardware_status and (olt_status is not None or legacy_sources):
            data = RltechData(
                aps={},
                ap_details=ap_details,
                stations={},
                last_success_80=now if legacy_success else None,
                poll_duration_ms=int((time.monotonic() - started) * 1000),
            )
        else:
            raise primary_error

        ap_tasks = previous.ap_tasks if previous is not None else {}
        if (
            primary_error is None
            and include_ap_inventory
            and self.last_ap_tasks is not None
        ):
            ap_tasks = self.last_ap_tasks

        web_status = previous.web_status if previous is not None else None
        web_lan_ports = previous.web_lan_ports if previous is not None else {}
        web_lanpon_ports = (
            previous.web_lanpon_ports if previous is not None else {}
        )
        web_status_update = (
            previous.web_status_update if previous is not None else None
        )
        web_status_parsed = (
            previous.web_status_parsed if previous is not None else None
        )
        if web_status is not None and self._ac_rebooted_since(web_status_update):
            # An AC reboot was sent after the last status read: the old
            # uptime and boot time are wrong now (stage 7). A read in this
            # poll (due, see _web_status_due) replaces them below.
            web_status = status_without_readings(web_status)
        uplink_pon = previous.uplink_pon if previous is not None else None
        uplink_pon_update = (
            previous.uplink_pon_update if previous is not None else None
        )
        if primary_error is None and web_status_fresh is not None:
            fresh_status, fresh_lan, fresh_lanpon, fresh_uplink, attempted = (
                web_status_fresh
            )
            if self._read_before_ac_reboot(fresh_status, now):
                # The AC had not gone down yet: keep only its identity and
                # leave the status pages due, so the first read after the
                # reboot replaces them (review 7 finding 2).
                fresh_status = status_without_readings(fresh_status)
            else:
                if attempted:
                    web_status_update = now
                if fresh_status is not None:
                    web_status_parsed = now
            web_status = fresh_status or web_status
            web_lan_ports = fresh_lan or web_lan_ports
            web_lanpon_ports = fresh_lanpon or web_lanpon_ports
            if fresh_uplink is not None:
                uplink_pon = fresh_uplink
                uplink_pon_update = now
        uplink_pon, uplink_pon_update = _age_uplink_pon(
            uplink_pon, uplink_pon_update, now=now, status_interval=status_interval
        )
        web_status = _age_web_status(
            web_status, web_status_parsed, now=now, status_interval=status_interval
        )
        if self._read_before_ac_reboot(olt_status, now):
            # Port 80 answered before the AC went down (review 7 finding 2).
            olt_status = status_without_readings(olt_status)

        # Port 80 wins for the master ports only when the *master* source
        # refreshed in this poll (review N1: a slave-only success must not
        # let the master's stale fallback beat fresh 8080 values).
        master_source = (
            legacy_sources.get(
                urlsplit(self.legacy_base_urls[0]).hostname
                or self.legacy_base_urls[0]
            )
            if self.legacy_base_urls
            else None
        )
        if master_source is not None and master_source.last_success == now:
            effective_lan = lan_ports or web_lan_ports
            effective_lanpon = lanpon_ports or web_lanpon_ports
        else:
            effective_lan = web_lan_ports or lan_ports
            effective_lanpon = web_lanpon_ports or lanpon_ports
        if web_lanpon_ports:
            # The AC's own ponport_info list decides which LAN-PON ports exist
            # (RH8001GR: LANPON1 only); port 80 never adds one (stage 6b).
            effective_lanpon = {
                ponid: port
                for ponid, port in effective_lanpon.items()
                if ponid in web_lanpon_ports
            }
        effective_status = _merge_olt_status(web_status, olt_status)
        return replace(
            data,
            olt_status=(
                _stabilize_olt_status(effective_status, previous)
                if effective_status is not None
                else data.olt_status
            ),
            lan_ports=effective_lan or data.lan_ports,
            lanpon_ports=effective_lanpon or data.lanpon_ports,
            legacy_sources=legacy_sources or data.legacy_sources,
            last_success_80=now if legacy_success else data.last_success_80,
            ap_details=ap_details or data.ap_details,
            web_status=web_status,
            web_lan_ports=web_lan_ports,
            web_lanpon_ports=web_lanpon_ports,
            web_status_update=web_status_update,
            web_status_parsed=web_status_parsed,
            uplink_pon=uplink_pon,
            uplink_pon_update=uplink_pon_update,
            ap_tasks=ap_tasks,
        )
