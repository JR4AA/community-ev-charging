"""Community EV Charging: sensors for a community-ev-charging server."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import ChargingCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type ChargingConfigEntry = ConfigEntry[ChargingCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ChargingConfigEntry) -> bool:
    coordinator = ChargingCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ChargingConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
