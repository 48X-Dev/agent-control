"""Agent-to-executor bindings and their HTTP wire models.

An *executor* is the process that actually runs an agent. Agent Control never
runs agent code itself, so before a chat session can exist, something has to
answer "which process serves this agent". That is what a runtime binding is.

The word "runtime" already means something else in this codebase
(``Operation.RUNTIME_USE``, ``AGENT_CONTROL_RUNTIME_TOKEN_SECRET``), so every
field naming the process that runs the agent says *executor*: ``executor_kind``,
``executor_app_name``. The table keeps the name ``agent_runtimes`` because it is
the binding registry, not the process.

One agent binds to exactly one executor. That is not a simplification: the
Python SDK holds a single module-level agent per process
(``sdks/python/src/agent_control/_state.py``) and ``AgentControlPlugin.__init__``
raises when the names disagree, so one process serves one agent and a team of
five agents means five executor processes.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, StringConstraints

from .agent import AGENT_NAME_MIN_LENGTH, AGENT_NAME_PATTERN, normalize_agent_name
from .base import BaseModel

EXECUTOR_BASE_URL_MAX_LENGTH = 512
EXECUTOR_APP_NAME_MAX_LENGTH = 255


class ExecutorKind(StrEnum):
    """Which executor implementation serves an agent.

    One member today. It exists as an enum rather than a free string because
    the value selects a client implementation, and a typo in it should be a
    422 at the boundary rather than a 500 at resolution time.
    """

    GOOGLE_ADK = "google_adk"


def validate_executor_base_url(value: str) -> str:
    """Return a normalized executor base URL, or raise ``ValueError``.

    Only the origin plus an optional path is accepted. A query string, a
    fragment or embedded credentials are rejected rather than silently
    dropped: this string is concatenated with executor paths on every call, and
    a caller who thinks they configured ``?key=...`` should be told they did
    not. The trailing slash is stripped so ``base + "/apps/..."`` never
    produces a double slash.

    This is deployment configuration written by an admin, not user input, but
    it is still the one field on this row that turns into an outbound request,
    so it is validated here rather than at the call site.
    """
    candidate = value.strip()
    if not candidate:
        raise ValueError("base_url must not be empty")
    if len(candidate) > EXECUTOR_BASE_URL_MAX_LENGTH:
        raise ValueError(
            f"base_url must be at most {EXECUTOR_BASE_URL_MAX_LENGTH} characters"
        )
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise ValueError("base_url must start with http:// or https://")
    if not parts.netloc:
        raise ValueError("base_url must include a host")
    if "@" in parts.netloc:
        raise ValueError("base_url must not embed credentials")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not carry a query string or fragment")
    return candidate.rstrip("/")


ExecutorBaseUrl = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EXECUTOR_BASE_URL_MAX_LENGTH),
    AfterValidator(validate_executor_base_url),
]

ExecutorAppName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EXECUTOR_APP_NAME_MAX_LENGTH),
]

def _normalize_agent_name_input(value: object) -> str:
    """Normalize an inbound agent name, rejecting anything unusable.

    Runs *before* the length and pattern constraints so a caller who sends
    ``"My-Agent-Name"`` gets the same normalization every other agent-bearing
    endpoint applies, rather than a pattern violation for the capital letters.
    """
    return normalize_agent_name(str(value))


AgentName = Annotated[
    str,
    BeforeValidator(_normalize_agent_name_input),
    StringConstraints(
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
    ),
]


class AgentRuntime(BaseModel):
    """The executor that serves one agent, within one namespace."""

    namespace_key: str = Field(..., description="Namespace the binding belongs to.")
    agent_name: AgentName = Field(..., description="Normalized agent identifier.")
    executor_kind: ExecutorKind = Field(
        default=ExecutorKind.GOOGLE_ADK,
        description="Which executor implementation serves this agent.",
    )
    base_url: ExecutorBaseUrl = Field(
        ...,
        description=(
            "Origin of the executor process, reachable from the server only. "
            "It is never returned to a browser-facing surface as a link."
        ),
    )
    executor_app_name: ExecutorAppName = Field(
        ...,
        description=(
            "Name of the app the executor serves, e.g. the ADK ``App(name=...)``. "
            "Copied onto every session created against this binding."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Disabled bindings are kept but refuse new sessions, so an executor "
            "can be drained without losing its configuration."
        ),
    )
    created_at: dt.datetime = Field(..., description="When the binding was created.")
    updated_at: dt.datetime = Field(..., description="When the binding last changed.")


# =============================================================================
# Requests / responses
# =============================================================================


class UpsertAgentRuntimeRequest(BaseModel):
    """Create or replace the executor binding for one agent.

    Replace semantics, keyed by the agent in the path: every field on an
    existing binding is overwritten with what this body carries, so an omitted
    ``enabled`` re-enables a drained binding.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: ExecutorBaseUrl = Field(
        ..., description="Origin of the executor process serving this agent."
    )
    executor_app_name: ExecutorAppName = Field(
        ..., description="Name of the app the executor serves."
    )
    executor_kind: ExecutorKind = Field(
        default=ExecutorKind.GOOGLE_ADK,
        description="Which executor implementation serves this agent.",
    )
    enabled: bool = Field(
        default=True,
        description="Whether the binding accepts new sessions.",
    )


class AgentRuntimeResponse(BaseModel):
    """One executor binding."""

    namespace_key: str
    agent_name: str
    executor_kind: ExecutorKind
    base_url: str
    executor_app_name: str
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    created: bool | None = Field(
        default=None,
        description=(
            "True when this response created the binding, False when it "
            "replaced one. Null on reads."
        ),
    )


class ListAgentRuntimesResponse(BaseModel):
    """Every executor binding in the namespace, optionally filtered by agent."""

    runtimes: list[AgentRuntimeResponse] = Field(default_factory=list)


class DeleteAgentRuntimeResponse(BaseModel):
    """Result of removing an executor binding."""

    deleted: bool = Field(
        ...,
        description=(
            "True when a binding was removed; False when the agent had none, "
            "so a repeated delete is not an error."
        ),
    )
