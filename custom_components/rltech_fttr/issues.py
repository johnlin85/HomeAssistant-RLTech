"""Repairs issues for RLTech FTTR.

Only the required 8080 Web UI source raises issues. Port 80 and MQTT are
optional LAN-only enhancements and degrade silently (plan 3.2.4 / 3.5).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .sources import ISSUE_WEB_UI_BUSY, ISSUE_WEB_UI_UNREACHABLE

_WEB_ISSUES = {
    ISSUE_WEB_UI_BUSY: ir.IssueSeverity.WARNING,
    ISSUE_WEB_UI_UNREACHABLE: ir.IssueSeverity.ERROR,
}


def _issue_id(issue: str, entry: ConfigEntry) -> str:
    return f"{issue}_{entry.entry_id}"


@callback
def async_sync_web_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    desired: str | None,
    placeholders: dict[str, str],
) -> None:
    """Create ``desired`` (if any) and delete the other 8080 issues."""
    for issue, severity in _WEB_ISSUES.items():
        issue_id = _issue_id(issue, entry)
        if issue == desired:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=severity,
                translation_key=issue,
                translation_placeholders=placeholders,
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def async_clear_web_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete every 8080 issue of an entry (used on unload)."""
    for issue in _WEB_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(issue, entry))
