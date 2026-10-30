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
        self._url = url.rstrip("/") + "/api/status"
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
