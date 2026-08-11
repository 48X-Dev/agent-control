"""Read models for a trace viewed as an ordered chain of hops."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, field_validator

from .actions import ActionDecision
from .agent import AGENT_NAME_MIN_LENGTH, AGENT_NAME_PATTERN, normalize_agent_name
from .base import BaseModel

TRACE_HOP_LIMIT_DEFAULT = 200

TRACE_HOP_LIMIT_MAX = 1000
"""Ceiling on hops returned for one trace.

Kept at or below ``EventQueryRequest``'s own maximum so a trace read never
needs more than a single page out of the event store.
"""


class TraceTeamRef(BaseModel):
    """The team an agent belonged to when a trace was read."""

    slug: str = Field(..., description="Stable key of the team.")
    display_name: str = Field(..., description="Human-readable team name.")


class TraceHop(BaseModel):
    """One control execution in a trace."""

    agent_name: str = Field(
        ...,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Agent that executed the control.",
    )
    team: TraceTeamRef | None = Field(
        default=None, description="Team the agent belongs to, or null when it has none."
    )
    span_id: str = Field(..., min_length=1, description="Span the hop belongs to.")
    timestamp: datetime = Field(..., description="Client-reported execution time (UTC).")
    control_name: str = Field(..., description="Name of the control that ran.")
    action: ActionDecision = Field(..., description="Action the control decided on.")
    matched: bool = Field(..., description="Whether the evaluator matched.")
    out_of_order: bool = Field(
        default=False,
        description=(
            "True when this hop's timestamp is not strictly later than the "
            "previous hop's, so its position was settled by span_id rather "
            "than by time."
        ),
    )

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))

    @field_validator("timestamp")
    @classmethod
    def ensure_timezone_aware(cls, v: datetime) -> datetime:
        """Treat a naive client timestamp as UTC."""
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class TraceResponse(BaseModel):
    """Ordered hops of a single trace."""

    trace_id: str = Field(..., min_length=1, description="Trace that was read.")
    hops: list[TraceHop] = Field(
        default_factory=list, description="Hops in (timestamp, span_id) order."
    )
    hop_count: int = Field(..., ge=0, description="Number of hops in this response.")
    total_hop_count: int = Field(..., ge=0, description="Number of hops the trace has in total.")
    truncated: bool = Field(
        ...,
        description=(
            "True when the trace has more hops than the response cap and only "
            "the earliest `hop_count` of them are included."
        ),
    )
    limit: int = Field(..., ge=1, description="Response cap applied to this read.")
    out_of_order: bool = Field(
        ...,
        description="True when any hop in this response is flagged out_of_order.",
    )
