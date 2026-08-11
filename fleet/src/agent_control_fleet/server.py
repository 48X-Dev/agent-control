"""The server calls the fleet makes, and the two credential refusals it owes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

__all__ = [
    "AgentRuntimeRow",
    "ServerClient",
    "ServerError",
]

_API_PREFIX = "/api/v1"
_REQUEST_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 2.0

# A session key no session service mints, so the halt claim joins against
# nothing and writes nothing. Only its authorization outcome is read.
_CREDENTIAL_PROBE_SESSION_KEY = "fleet-credential-probe-session"


class ServerError(RuntimeError):
    """A refused or unreachable server, named by code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentRuntimeRow:
    """One executor binding as the server holds it."""

    agent_name: str
    base_url: str
    executor_app_name: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ServerClient:
    """Everything the fleet asks the control plane, over one base URL."""

    base_url: str
    api_key: str

    def wait_for_health(self, *, timeout_seconds: float) -> None:
        """Replaces ``depends_on: service_healthy``, which this runtime does not have."""

        deadline = time.monotonic() + timeout_seconds
        last = "no response"
        while True:
            try:
                response = httpx.get(
                    f"{self.base_url}/health", timeout=_REQUEST_TIMEOUT_SECONDS
                )
                if response.status_code == 200:
                    return
                last = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last = str(exc)
            if time.monotonic() >= deadline:
                raise ServerError(
                    "server_unreachable",
                    f"{self.base_url}/health did not answer 200 within "
                    f"{timeout_seconds:.0f}s ({last}). Nothing downstream ran.",
                )
            time.sleep(_POLL_INTERVAL_SECONDS)

    def refuse_when_credentials_are_off(self) -> None:
        """An uncredentialed read that succeeds means every separation here is inert."""

        try:
            response = httpx.get(
                f"{self.base_url}{_API_PREFIX}/agent-runtimes",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ServerError(
                "server_unreachable", f"{self.base_url} is unreachable: {exc}"
            ) from exc
        if response.status_code < 400:
            raise ServerError(
                "credentials_disabled",
                f"{self.base_url} served an uncredentialed read of agent-runtimes "
                f"(HTTP {response.status_code}). With API keys off every operation "
                "succeeds unauthenticated, so the admin key this job holds separates "
                "nothing. Set AGENT_CONTROL_API_KEY_ENABLED=true and try again.",
            )

    def refuse_when_executor_credential_cannot_halt(self, executor_api_key: str) -> None:
        """Section 4.4: an executor that cannot claim halts has lost the STOP button."""

        try:
            response = httpx.post(
                f"{self.base_url}{_API_PREFIX}/agent-sessions/"
                f"{quote(_CREDENTIAL_PROBE_SESSION_KEY)}/halts/claim",
                json={"boundary": "tool"},
                headers={"X-API-Key": executor_api_key},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ServerError(
                "server_unreachable", f"{self.base_url} is unreachable: {exc}"
            ) from exc
        if response.status_code in (401, 403):
            raise ServerError(
                "executor_credential_cannot_halt",
                "The key in AGENT_CONTROL_FLEET_EXECUTOR_API_KEY was refused "
                f"(HTTP {response.status_code}) on the halt claim every executor makes "
                "at every tool boundary. Started anyway, these executors would lose the "
                "operator STOP button silently while the console still showed the halt "
                "recorded. Resolve section 4.4 of docs/plans/agent-fleet-topology.md and "
                "give this setting a key that path accepts.",
            )

    def list_runtimes(self) -> tuple[AgentRuntimeRow, ...]:
        payload = self._get(f"{_API_PREFIX}/agent-runtimes")
        rows = payload.get("runtimes")
        if not isinstance(rows, list):
            raise ServerError("server_response", "GET /agent-runtimes returned no runtimes list.")
        return tuple(
            AgentRuntimeRow(
                agent_name=str(row["agent_name"]),
                base_url=str(row["base_url"]),
                executor_app_name=str(row["executor_app_name"]),
                enabled=bool(row.get("enabled", True)),
            )
            for row in rows
            if isinstance(row, dict)
        )

    def list_registered_agents(self) -> tuple[str, ...]:
        payload = self._get(f"{_API_PREFIX}/agents", params={"limit": 200})
        agents = payload.get("agents")
        if not isinstance(agents, list):
            raise ServerError("server_response", "GET /agents returned no agents list.")
        return tuple(
            str(agent["agent_name"])
            for agent in agents
            if isinstance(agent, dict) and "agent_name" in agent
        )

    def bind_runtime(self, *, agent_name: str, base_url: str, executor_app_name: str) -> None:
        try:
            response = httpx.put(
                f"{self.base_url}{_API_PREFIX}/agent-runtimes/{quote(agent_name)}",
                json={"base_url": base_url, "executor_app_name": executor_app_name},
                headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ServerError(
                "server_unreachable", f"{self.base_url} is unreachable: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise ServerError(
                "bind_refused",
                f"PUT /agent-runtimes/{agent_name} returned HTTP {response.status_code}: "
                f"{response.text.strip()[:400]}",
            )

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"X-API-Key": self.api_key},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ServerError(
                "server_unreachable", f"{self.base_url} is unreachable: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise ServerError(
                "server_refused",
                f"GET {path} returned HTTP {response.status_code}: {response.text.strip()[:400]}",
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ServerError("server_response", f"GET {path} did not return an object.")
        return payload
