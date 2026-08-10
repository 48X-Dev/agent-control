"""How the sync talks to Drive: the flags, the retries and one token refresh.

Split out of ``drive_client.py`` along the seam between how a call is made and
what it asks for. The shared-drive flags are attached here, on every request,
because 5.7 measured what their absence does: ``files.get`` answers 404 and
``files.list`` answers zero rows, and both read exactly like a folder nobody
shared. A flag attached per call site is a flag one call site can forget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from .config import SyncConfig
from .drive_auth import DriveTokenProvider

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 30.0


class DriveError(RuntimeError):
    """Anything the Drive read path gives up on, always with a named code."""

    code: str = "drive_error"


async def _sleep(seconds: float) -> None:
    """Indirection so the retry paths are testable without waiting."""
    await asyncio.sleep(seconds)


def retry_after(response: httpx.Response, fallback: float) -> float:
    """An upstream Retry-After in seconds, capped; anything else falls back to the backoff."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return fallback
    try:
        return max(0.0, min(float(raw), MAX_BACKOFF_SECONDS))
    except ValueError:
        return fallback


class DriveTransport:
    """One Drive call, with the shared-drive flags, the retries and a token refresh."""

    def __init__(
        self, tokens: DriveTokenProvider, client: httpx.AsyncClient, config: SyncConfig
    ) -> None:
        self._tokens = tokens
        self._client = client
        self._config = config

    async def json(
        self, path: str, params: Mapping[str, str], *, list_call: bool = False
    ) -> dict[str, Any]:
        response = await self.request(path, params, list_call=list_call)
        if response.status_code != 200:
            raise DriveError(f"Drive answered HTTP {response.status_code} for {path}.")
        payload = response.json()
        if not isinstance(payload, dict):
            raise DriveError(f"Drive answered {path} with a body that is not an object.")
        return payload

    async def request(
        self, path: str, params: Mapping[str, str], *, list_call: bool = False
    ) -> httpx.Response:
        """One Drive call with the shared-drive flags, retries, and one token refresh."""
        query = dict(params)
        query["supportsAllDrives"] = "true"
        if list_call:
            query["includeItemsFromAllDrives"] = "true"

        forgotten = False
        delay = BACKOFF_SECONDS
        response: httpx.Response | None = None
        failure: Exception | None = None
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            token = await self._tokens.bearer_token()
            try:
                response = await self._client.get(
                    f"{DRIVE_API_BASE}{path}",
                    params=query,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._config.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                failure = exc
                response = None
                if attempt >= MAX_ATTEMPTS:
                    break
                await _sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            if response.status_code == 401 and not forgotten:
                forgotten = True
                self._tokens.forget()
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= MAX_ATTEMPTS:
                    break
                await _sleep(retry_after(response, delay))
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            return response
        if response is None:
            raise DriveError(
                f"Drive was unreachable for {path} after {MAX_ATTEMPTS} attempts "
                f"({type(failure).__name__})."
            ) from failure
        return response
