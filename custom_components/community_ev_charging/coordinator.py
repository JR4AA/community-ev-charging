"""Polls the charging server."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CannotConnect, ChargingApi, InvalidAuth
from .const import CONF_TOKEN, DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class ChargingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL, config_entry=entry)
        self.api = ChargingApi(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.api.status()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed("The charging server refused the token") from err
        except CannotConnect as err:
            raise UpdateFailed(f"Can't reach the charging server: {err}") from err
