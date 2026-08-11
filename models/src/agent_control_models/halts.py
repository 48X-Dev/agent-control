"""Stopping a running agent, and the wire models for it."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from .base import BaseModel

HALT_TOOL_NAME_MAX_LENGTH = 64
"""Ceiling on the executor-supplied tool name recorded against a halt."""

HaltToolName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=HALT_TOOL_NAME_MAX_LENGTH,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$",
    ),
]
"""The one field in this design carrying executor-chosen bytes.

It arrives from a process running arbitrary agent code and lands in an operator
console, so it is pattern-checked against a strict identifier server-side and
capped, and it renders under the chat panel's plain-text rule like everything
else that came out of a model."""


class HaltMode(StrEnum):
    """How the stop was carried out."""

    GRACEFUL = "graceful"
    RESTART = "restart"


class HaltStatus(StrEnum):
    """Where one halt is."""

    PENDING = "pending"
    APPLIED = "applied"
    EXPIRED = "expired"


class HaltBoundary(StrEnum):
    """Where a halt landed."""

    MODEL = "model"
    TOOL = "tool"
    PROCESS = "process"


class Halt(BaseModel):
    """One operator stop, bound to one turn."""

    id: int = Field(..., description="Identifier, unique within the namespace.")
    session_key: str = Field(..., description="Session the halt belongs to.")
    target_trace_id: str = Field(
        ...,
        description=(
            "The one turn this halt can ever apply to. Copied from the "
            "session's live turn at creation."
        ),
    )
    mode: HaltMode = Field(..., description="How the stop was carried out.")
    status: HaltStatus = Field(..., description="Where the halt is.")
    created_at: dt.datetime = Field(..., description="When stop was pressed.")
    applied_at: dt.datetime | None = Field(
        default=None, description="When the executor blocked at a boundary."
    )
    applied_at_boundary: HaltBoundary | None = Field(
        default=None, description="Which boundary it landed at."
    )
    applied_tool_name: str | None = Field(
        default=None,
        description=(
            "Tool the agent was about to run, for a halt that landed at a tool "
            "boundary. Executor-supplied, pattern-checked, rendered as plain "
            "text."
        ),
    )
    turn_ended_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When this server observed the turn actually end. This, not "
            "'applied', is what a UI may render as stopped."
        ),
    )


class CreateHaltRequest(BaseModel):
    """Stop the turn this session is running."""

    model_config = ConfigDict(extra="forbid")


class CreateHaltResponse(BaseModel):
    """The halt now recorded against the live turn."""

    halt: Halt = Field(..., description="The halt bound to the live turn.")
    created: bool = Field(..., description="False when a halt was already recorded for this turn.")


class ListHaltsResponse(BaseModel):
    """Halts recorded against one session, newest first."""

    session_key: str = Field(..., description="Session these halts belong to.")
    halts: list[Halt] = Field(default_factory=list)


# =============================================================================
# Machine side: claim and acknowledge
# =============================================================================


class ClaimHaltRequest(BaseModel):
    """Ask whether this boundary should stop."""

    model_config = ConfigDict(extra="forbid")

    boundary: HaltBoundary = Field(
        ..., description="Boundary this claim is running at: 'model' or 'tool'."
    )
    tool_name: HaltToolName | None = Field(
        default=None,
        description="Tool about to run, at a tool boundary.",
    )
    invocation_id: str | None = Field(
        default=None,
        max_length=128,
        description="Executor-supplied invocation identifier, for logs only.",
    )


class ClaimHaltResponse(BaseModel):
    """Whether to block here."""

    session_key: str = Field(..., description="Session the claim ran against.")
    halt: Halt | None = Field(
        default=None,
        description="The halt that was applied, or null when there is nothing to do.",
    )


class AckHaltRequest(BaseModel):
    """Enrich an applied halt after the fact."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Halt to enrich.")
    applied_tool_name: HaltToolName | None = Field(
        default=None, description="Tool the agent was about to run."
    )


class AckHaltResponse(BaseModel):
    """The halt after the acknowledgement."""

    session_key: str = Field(..., description="Session the acknowledgement ran against.")
    halt: Halt = Field(..., description="The halt as it now stands.")
