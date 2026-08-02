"""Read models for a trace viewed as an ordered chain of hops.

These are views over the control execution events in
:mod:`agent_control_models.observability`; nothing here is ingested or stored.
"""

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
    """The team an agent belonged to when a trace was read.

    Resolved at read time from current membership, not captured with the event,
    so a hop reflects where the agent sits now rather than where it sat when
    the control ran.
    """

    slug: str = Field(..., description="Stable key of the team.")
    display_name: str = Field(..., description="Human-readable team name.")


class TraceHop(BaseModel):
    """One control execution in a trace.

    A hop is an observation, not a causal step: events carry no parent span, so
    position in the sequence says only that this hop's timestamp sorted here.
    """

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
        """Treat a naive client timestamp as UTC.

        Events are accepted with lenient timestamps, and hops from a mix of
        naive and aware clients have to stay comparable to each other.
        """
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class TraceResponse(BaseModel):
    """Ordered hops of a single trace.

    Hops are sorted by ``(timestamp, span_id)``. Timestamps come from the
    clients that emitted the events, so a sequence spanning several machines
    can be misordered without that being detectable here; ``out_of_order``
    reports the one case that is detectable, which is hops the timestamps
    could not separate.
    """

    trace_id: str = Field(..., min_length=1, description="Trace that was read.")
    hops: list[TraceHop] = Field(
        default_factory=list, description="Hops in (timestamp, span_id) order."
    )
    hop_count: int = Field(..., ge=0, description="Number of hops in this response.")
    total_hop_count: int = Field(
        ..., ge=0, description="Number of hops the trace has in total."
    )
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
