"""Access to the running coordinator of a config entry (entry.runtime_data)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState


def loaded_coordinator(entry: ConfigEntry | None) -> Any:
    """Return the coordinator of a loaded entry, else None.

    ``entry.runtime_data`` is set in async_setup_entry after the first
    refresh and removed by Home Assistant on unload. The state check also
    hides a coordinator left behind by a setup that failed later on.
    """
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        return None
    return getattr(entry, "runtime_data", None)
