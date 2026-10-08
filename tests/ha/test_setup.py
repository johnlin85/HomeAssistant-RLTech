"""Setup/unload smoke tests against a real Home Assistant runtime."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from rltech_ha_helpers import make_entry


async def test_setup_and_unload(hass: HomeAssistant, fake_ac) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.rltech_olt_198_51_100_29_ap_count").state == "2"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_runtime_data_holds_the_coordinator_until_unload(
    hass: HomeAssistant, fake_ac
) -> None:
    from homeassistant.components import frontend

    from custom_components.rltech_fttr.const import DOMAIN, PANEL_URL_PATH
    from custom_components.rltech_fttr.coordinator import RltechCoordinator

    first = make_entry()
    second = make_entry(
        title="RLTech FTTR second",
        unique_id="http://198.51.100.30:8080",
        data={**first.data, "base_url": "http://198.51.100.30:8080"},
    )
    for entry in (first, second):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert isinstance(entry.runtime_data, RltechCoordinator)
    # Nothing is kept in hass.data any more (quality scale: runtime-data).
    assert DOMAIN not in hass.data
    assert frontend.async_panel_exists(hass, PANEL_URL_PATH)

    # One entry left: the shared sidebar panel stays.
    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert not hasattr(first, "runtime_data")
    assert frontend.async_panel_exists(hass, PANEL_URL_PATH)

    # Last entry gone: the panel goes too.
    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert not hasattr(second, "runtime_data")
    assert not frontend.async_panel_exists(hass, PANEL_URL_PATH)
