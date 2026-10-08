"""The only door to Muungano's data: the API's `/assistant/*` routes.

An allow-list of (method, path) pairs, the service token on every call, the
admin's Telegram id for the API's log, a timeout and a per-minute cap. Nothing
else can be reached through this client, whatever a tool asks for.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any, Optional

import httpx

# Every route is a read, even the POSTs: they take a body of filters.
ALLOWED = (
    ("GET", re.compile(r"^/assistant/athletes$")),
    ("GET", re.compile(r"^/assistant/catalog$")),
    ("GET", re.compile(r"^/assistant/athletes/\d+/summary$")),
    ("GET", re.compile(r"^/assistant/athletes/\d+/workouts$")),
    ("GET", re.compile(r"^/assistant/athletes/\d+/workouts/\d+/laps$")),
    ("GET", re.compile(r"^/assistant/athletes/\d+/(payments|plan|profile)$")),
    ("GET", re.compile(r"^/assistant/receipts$")),
    ("GET", re.compile(r"^/assistant/receipts/\d+$")),
    ("GET", re.compile(r"^/assistant/applications$")),
    ("GET", re.compile(r"^/assistant/applications/\d+$")),
    ("GET", re.compile(r"^/assistant/garmin/errors$")),
    ("GET", re.compile(r"^/assistant/messages$")),
    ("POST", re.compile(r"^/assistant/athletes/query$")),
    ("POST", re.compile(r"^/assistant/athletes/aggregate$")),
    ("POST", re.compile(r"^/assistant/athletes/series$")),
    ("POST", re.compile(r"^/assistant/messages/preview$")),
)
# A member's bank document: fetched to show it to the admin, never handed to the model.
FILES = (("GET", re.compile(r"^/assistant/receipts/\d+/file$")),)
# The routes that change something. Only a button an admin pressed gets here: `MuunganoApi.write`
# is called from `Bot._on_button` and from no tool of the model.
WRITES = (
    ("POST", re.compile(r"^/assistant/receipts/\d+/decide$")),
    ("POST", re.compile(r"^/assistant/messages$")),
    ("POST", re.compile(r"^/assistant/applications/\d+/decide$")),
)


class ApiError(Exception):
    """A failed call, with a message that is safe to show an admin."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class MuunganoApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        max_per_min: int = 60,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._token = token
        self._max_per_min = max_per_min
        self._calls: deque[float] = deque()
        self._http = client or httpx.AsyncClient(base_url=base_url, timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _check(self, method: str, path: str, allowed: tuple = ALLOWED) -> None:
        if not any(m == method and rx.match(path) for m, rx in allowed):
            raise ApiError(0, "That route is not allowed for the assistant")
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= self._max_per_min:
            raise ApiError(429, "Too many requests to the API; wait a minute")
        self._calls.append(now)

    async def request(
        self,
        method: str,
        path: str,
        *,
        telegram_user_id: int,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        allowed: tuple = ALLOWED,
    ) -> dict[str, Any]:
        self._check(method, path, allowed)
        headers = {"X-Bot-Token": self._token, "X-Telegram-User-Id": str(telegram_user_id)}
        try:
            response = await self._http.request(method, path, params=params, json=json, headers=headers)
        except httpx.TimeoutException:
            raise ApiError(0, "The API took too long to answer") from None
        except httpx.HTTPError:
            raise ApiError(0, "I could not reach the API") from None

        try:
            body = response.json()
        except ValueError:
            raise ApiError(response.status_code, "The API answered something unexpected") from None

        if response.status_code == 401:
            # A configuration problem, not something the admin can fix.
            raise ApiError(401, "The API rejected my token")
        if not isinstance(body, dict) or not body.get("success"):
            message = ((body or {}).get("error") or {}).get("message") if isinstance(body, dict) else None
            raise ApiError(response.status_code, str(message or "The API could not do that"))
        return body

    async def get(self, path: str, *, telegram_user_id: int, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return await self.request("GET", path, telegram_user_id=telegram_user_id, params=params)

    async def post(self, path: str, *, telegram_user_id: int, json: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", path, telegram_user_id=telegram_user_id, json=json)

    async def write(self, path: str, *, telegram_user_id: int, json: dict[str, Any]) -> dict[str, Any]:
        """A call that changes something, on behalf of the admin who pressed the button."""
        return await self.request("POST", path, telegram_user_id=telegram_user_id, json=json, allowed=WRITES)

    async def get_file(self, path: str, *, telegram_user_id: int) -> tuple[bytes, str]:
        """A file and its media type."""
        self._check("GET", path, FILES)
        headers = {"X-Bot-Token": self._token, "X-Telegram-User-Id": str(telegram_user_id)}
        try:
            response = await self._http.get(path, headers=headers)
        except httpx.HTTPError:
            raise ApiError(0, "I could not reach the API") from None
        if response.status_code != 200:
            raise ApiError(response.status_code, "The file is not available")
        return response.content, response.headers.get("content-type", "application/octet-stream").split(";")[0]
