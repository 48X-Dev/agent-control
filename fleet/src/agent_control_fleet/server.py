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

# What a session-bound runtime token is minted against, so the probe asks for a
# token of the same kind rather than one shaped like the agent-level exchange.
_SESSION_TARGET_TYPE = "agent_session"

_HALT_REFUSAL_CONSEQUENCE = (
    "Started anyway, these executors would lose the operator STOP button "
    "silently while the console still showed the halt recorded. Resolve "
    "section 4.4 of docs/plans/agent-fleet-topology.md and give "
    "AGENT_CONTROL_FLEET_EXECUTOR_API_KEY a key that path accepts."
)


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
        """Section 4.4: an executor that cannot claim halts has lost the STOP button.

        Which question that is depends on how the server is configured, and both
        configurations are reachable, so this reads the mode rather than assuming
        it. Section 4.4.1 is what assuming cost.
        """

        token = self._exchange_probe_runtime_token(executor_api_key)
        if token is None:
            self._refuse_when_api_key_cannot_claim(executor_api_key)
            return
        self._refuse_when_runtime_token_is_not_verified(token)

    def _exchange_probe_runtime_token(self, executor_api_key: str) -> str | None:
        """Exchange the executor key for a runtime token; ``None`` means API-key mode.

        The 503 is the mode signal rather than a fault. The endpoint answers it
        exactly when the server holds no runtime token config, and that same
        config is what decides whether session creation mints a session token and
        whether the halt route belongs to the JWT provider at all.
        """

        response = self._post(
            f"{_API_PREFIX}/auth/runtime-token-exchange",
            body={
                "target_type": _SESSION_TARGET_TYPE,
                "target_id": _CREDENTIAL_PROBE_SESSION_KEY,
            },
            headers={"X-API-Key": executor_api_key},
        )
        if response.status_code in (404, 503):
            return None
        if response.status_code >= 400:
            raise ServerError(
                "executor_credential_cannot_halt",
                "The key in AGENT_CONTROL_FLEET_EXECUTOR_API_KEY was refused "
                f"(HTTP {response.status_code}) exchanging for the runtime token this "
                "server binds nudge and halt delivery to. " + _HALT_REFUSAL_CONSEQUENCE,
            )
        payload = response.json()
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ServerError(
                "server_response",
                "POST /auth/runtime-token-exchange answered 200 with no token, so the "
                "fleet cannot tell whether this key can reach the halt path.",
            )
        return token

    def _refuse_when_api_key_cannot_claim(self, executor_api_key: str) -> None:
        """API-key mode: the halt claim is authorized by the key the executor holds."""

        response = self._post(
            f"{_API_PREFIX}/agent-sessions/"
            f"{quote(_CREDENTIAL_PROBE_SESSION_KEY)}/halts/claim",
            body={"boundary": "tool"},
            headers={"X-API-Key": executor_api_key},
        )
        if response.status_code in (401, 403):
            raise ServerError(
                "executor_credential_cannot_halt",
                "The key in AGENT_CONTROL_FLEET_EXECUTOR_API_KEY was refused "
                f"(HTTP {response.status_code}) on the halt claim every executor makes "
                "at every tool boundary. " + _HALT_REFUSAL_CONSEQUENCE,
            )

    def _refuse_when_runtime_token_is_not_verified(self, token: str) -> None:
        """JWT mode: the halt claim is authorized by a token, so the token is the probe.

        A 403 is the healthy answer and is not a refusal. The exchange grant
        carries ``runtime.use`` alone, while the token session creation mints
        carries ``agent_nudges.consume``, so the route refusing this token on
        scope is it verifying the signature and applying the rule an executor's
        own token satisfies. A 401 is the one outcome that means the halt path
        will not accept a token this same server minted.
        """

        response = self._post(
            f"{_API_PREFIX}/agent-sessions/"
            f"{quote(_CREDENTIAL_PROBE_SESSION_KEY)}/halts/claim",
            body={"boundary": "tool"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            raise ServerError(
                "executor_credential_cannot_halt",
                "This server minted a runtime token for the executor credential and "
                "then answered HTTP 401 when that token claimed a halt, so the mint "
                "and verify sides disagree. " + _HALT_REFUSAL_CONSEQUENCE,
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

    def _post(
        self, path: str, *, body: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        try:
            return httpx.post(
                f"{self.base_url}{path}",
                json=body,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ServerError(
                "server_unreachable", f"{self.base_url} is unreachable: {exc}"
            ) from exc

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
