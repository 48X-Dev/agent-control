"""Agent-to-executor bindings and their HTTP wire models."""

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
    """Which executor implementation serves an agent."""

    GOOGLE_ADK = "google_adk"


def validate_executor_base_url(value: str) -> str:
    """Return a normalized executor base URL, or raise ``ValueError``."""
    candidate = value.strip()
    if not candidate:
        raise ValueError("base_url must not be empty")
    if len(candidate) > EXECUTOR_BASE_URL_MAX_LENGTH:
        raise ValueError(f"base_url must be at most {EXECUTOR_BASE_URL_MAX_LENGTH} characters")
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
    """Normalize an inbound agent name, rejecting anything unusable."""
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
    """Create or replace the executor binding for one agent."""

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
