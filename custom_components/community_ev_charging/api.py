"""Client for the charging server's read-only /api/status."""
from __future__ import annotations

from typing import Any

import aiohttp


class CannotConnect(Exception):
    """The server didn't answer."""


class InvalidAuth(Exception):
    """The server refused the token."""


class ChargingApi:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._base = url.rstrip("/")
        self._url = self._base + "/api/status"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def status(self) -> dict[str, Any]:
        try:
            async with self._session.get(self._url, headers=self._headers,
                                         timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (401, 403):
                    raise InvalidAuth
                if resp.status != 200:
                    raise CannotConnect(f"HTTP {resp.status}")
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err

    async def set_solar(self, on: bool) -> bool:
        try:
            async with self._session.post(self._base + "/api/solar", headers=self._headers, json={"on": on},
                                          timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (401, 403):
                    raise InvalidAuth
                if resp.status != 200:
                    raise CannotConnect(f"HTTP {resp.status}")
                return (await resp.json())["solar_rate_on"]
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err
