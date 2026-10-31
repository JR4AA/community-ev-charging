"""The solar-rate switch: Home Assistant tells the server when there's spare solar."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ChargingConfigEntry
from .api import CannotConnect, InvalidAuth
from .entity import ChargingEntity


async def async_setup_entry(hass: HomeAssistant, entry: ChargingConfigEntry, add: AddEntitiesCallback) -> None:
    add([SolarRate(entry.runtime_data)])


class SolarRate(ChargingEntity, SwitchEntity):
    """On: energy is billed at the server's solar price (SOLAR_PRICE_PER_KWH)."""

    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "solar_rate", None)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("solar_rate_on"))

    async def _set(self, on: bool) -> None:
        try:
            await self.coordinator.api.set_solar(on)
        except (CannotConnect, InvalidAuth) as err:
            raise HomeAssistantError(f"Couldn't set the solar rate: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
