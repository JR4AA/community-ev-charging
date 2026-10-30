"""Set up from the UI: the server's address and its API token."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CannotConnect, ChargingApi, InvalidAuth
from .const import CONF_TOKEN, DOMAIN

SCHEMA = vol.Schema({vol.Required(CONF_URL): str, vol.Required(CONF_TOKEN): str})


class ChargingConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _check(self, url: str, token: str) -> str | None:
        try:
            await ChargingApi(async_get_clientsession(self.hass), url, token).status()
        except InvalidAuth:
            return "invalid_auth"
        except CannotConnect:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            if not (error := await self._check(url, user_input[CONF_TOKEN].strip())):
                return self.async_create_entry(
                    title=url.split("//")[-1], data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN].strip()})
            errors["base"] = error
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(
            SCHEMA, user_input or {CONF_URL: "http://"}), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if not (error := await self._check(entry.data[CONF_URL], token)):
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})
            errors["base"] = error
        return self.async_show_form(step_id="reauth_confirm",
                                    data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}), errors=errors)
