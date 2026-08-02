"""Fetching an agent's server-managed system prompt and model.

One call returns both fields. One and not two, so there is no second failure
mode and no window in which the prompt and the model disagree about which
version they came from.

The values are cached on the SDK's state container by the refresh loop and read
by the ADK plugin on every model call. Two accessors return the raw stored body
and the raw model id, unwrapped: wrapping exists to solve idempotent
re-application in a field shared with control guidance, which is an ADK-plugin
problem. A caller driving their own client does not have it. The contract is: we
store it, version it and hand it to you; applying it is yours.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .client import AgentControlClient
from .validation import ensure_agent_name


@dataclass(frozen=True)
class AgentConfigSnapshot:
    """One fetch of an agent's configuration, as the server resolved it.

    ``prompt_source`` and ``model_source`` are resolved server-side and are not
    re-derived here. That is what keeps the startup delivery gate and the
    allowlist-membership check in one place instead of in every client, and it
    is why an older SDK meeting a newer server degrades to "do nothing" rather
    than to "guess".
    """

    body: str | None = None
    prompt_enabled: bool = True
    prompt_source: str = "none"
    model_id: str | None = None
    model_provider: str | None = None
    model_source: str = "code"
    model_allowed: bool = True
    model_cost_tier: str | None = None
    delivery_state: str = "active"
    etag: str | None = None
    current_version: int = 0
    fetched_at: dt.datetime | None = field(default=None)

    @property
    def managed_prompt(self) -> str | None:
        """The body to apply, or ``None`` when nothing should be applied.

        Every reason not to apply - cleared, disabled, delivery gated off - has
        already collapsed into ``prompt_source`` on the server.
        """
        if self.prompt_source != "managed":
            return None
        return self.body

    @property
    def managed_model(self) -> tuple[str, str] | None:
        """``(model_id, provider)`` to apply, or ``None``.

        ``None`` whenever the server did not say ``managed``, whenever the id is
        missing, and - the case worth naming - whenever the provider is absent.
        The SDK never infers a provider from the id string: that inference is
        precisely how a name in a dropdown becomes a destination nobody chose.
        """
        if self.model_source != "managed":
            return None
        if not self.model_id or not self.model_provider:
            return None
        return self.model_id, self.model_provider

    @classmethod
    def from_response(
        cls, payload: dict[str, Any], *, fetched_at: dt.datetime
    ) -> AgentConfigSnapshot:
        """Build a snapshot from the wire payload, tolerating unknown fields."""
        return cls(
            body=payload.get("body"),
            prompt_enabled=bool(payload.get("prompt_enabled", True)),
            prompt_source=str(payload.get("prompt_source") or "none"),
            model_id=payload.get("model_id"),
            model_provider=payload.get("model_provider"),
            model_source=str(payload.get("model_source") or "code"),
            model_allowed=bool(payload.get("model_allowed", True)),
            model_cost_tier=payload.get("model_cost_tier"),
            delivery_state=str(payload.get("delivery_state") or "active"),
            etag=payload.get("etag"),
            current_version=int(payload.get("current_version") or 0),
            fetched_at=fetched_at,
        )

    def differs_from(self, other: AgentConfigSnapshot | None) -> bool:
        """Whether either field changed, ignoring the fetch timestamp.

        Drives the change callback. Comparing the resolved values rather than
        the etag means a change in *delivery* - the gate opening, a model
        leaving the allowlist - counts as a change, which is what a caller
        reacting to configuration actually wants to hear about.
        """
        if other is None:
            return True
        return (
            self.managed_prompt != other.managed_prompt
            or self.managed_model != other.managed_model
        )


async def get_agent_config(
    client: AgentControlClient, agent_name: str
) -> dict[str, Any]:
    """GET this agent's configuration.

    Raises on transport and HTTP errors. The caller decides policy, which for
    the refresh loop means keeping the last known values rather than treating a
    control-plane outage as an agent outage.
    """
    normalized = ensure_agent_name(agent_name)
    response = await client.http_client.get(f"/api/v1/agents/{normalized}/config")
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload
