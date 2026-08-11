"""Observability models for tracking control executions."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator

from .actions import (
    ActionDecision,
    normalize_action,
    validate_action_list,
)
from .agent import AGENT_NAME_MIN_LENGTH, AGENT_NAME_PATTERN, normalize_agent_name
from .base import BaseModel

# =============================================================================
# Core Event Model
# =============================================================================


class ControlExecutionEvent(BaseModel):
    """Represents a single control execution event."""

    # Unique identifiers (OpenTelemetry-compatible)
    control_execution_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique ID for this control execution",
    )
    trace_id: str = Field(
        ...,
        min_length=1,
        description="Trace ID for distributed tracing (SDK generates OTEL-compatible 32-char hex)",
    )
    span_id: str = Field(
        ...,
        min_length=1,
        description="Span ID for distributed tracing (SDK generates OTEL-compatible 16-char hex)",
    )

    # Agent identity
    agent_name: str = Field(
        ...,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Identifier of the agent",
    )

    # Control info
    control_id: int = Field(..., description="Database ID of the control")
    control_name: str = Field(..., description="Name of the control (denormalized)")

    # Execution context
    check_stage: Literal["pre", "post"] = Field(..., description="Check stage: 'pre' or 'post'")
    applies_to: Literal["llm_call", "tool_call"] = Field(
        ..., description="Type of call: 'llm_call' or 'tool_call'"
    )

    # Result
    action: ActionDecision = Field(..., description="Action taken by the control")
    matched: bool = Field(..., description="Whether the evaluator matched (True) or not (False)")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score (0.0 to 1.0)")

    # Timing
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the control was executed (UTC)",
    )
    execution_duration_ms: float | None = Field(
        default=None, ge=0, description="Execution duration in milliseconds"
    )

    # Optional details
    evaluator_name: str | None = Field(default=None, description="Name of the evaluator used")
    selector_path: str | None = Field(
        default=None, description="Selector path used to extract data"
    )
    error_message: str | None = Field(
        default=None, description="Error message if evaluation failed"
    )

    # Extensibility
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, v: str) -> str:
        """Validate trace_id is non-empty."""
        if not v or not v.strip():
            raise ValueError("trace_id cannot be empty")
        return v

    @field_validator("span_id")
    @classmethod
    def validate_span_id(cls, v: str) -> str:
        """Validate span_id is non-empty."""
        if not v or not v.strip():
            raise ValueError("span_id cannot be empty")
        return v

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))

    @field_validator("action", mode="before")
    @classmethod
    def normalize_event_action(cls, value: str) -> ActionDecision:
        return normalize_action(value)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "control_execution_id": "550e8400-e29b-41d4-a716-446655440000",
                    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                    "span_id": "00f067aa0ba902b7",
                    "agent_name": "my-agent",
                    "control_id": 123,
                    "control_name": "sql-injection-check",
                    "check_stage": "pre",
                    "applies_to": "llm_call",
                    "action": "deny",
                    "matched": True,
                    "confidence": 0.95,
                    "timestamp": "2025-01-09T10:30:00Z",
                    "execution_duration_ms": 15.3,
                    "evaluator_name": "regex",
                    "selector_path": "input",
                }
            ]
        }
    }


# =============================================================================
# Batch Ingestion Models
# =============================================================================


class BatchEventsRequest(BaseModel):
    """Request model for batch event ingestion."""

    events: list[ControlExecutionEvent] = Field(
        ..., min_length=1, max_length=1000, description="List of events to ingest"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "events": [
                        {
                            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                            "span_id": "00f067aa0ba902b7",
                            "agent_name": "my-agent",
                            "control_id": 123,
                            "control_name": "sql-injection-check",
                            "check_stage": "pre",
                            "applies_to": "llm_call",
                            "action": "deny",
                            "matched": True,
                            "confidence": 0.95,
                        }
                    ]
                }
            ]
        }
    }


class BatchEventsResponse(BaseModel):
    """Response model for batch event ingestion."""

    received: int = Field(..., ge=0, description="Number of events received")
    enqueued: int = Field(..., ge=0, description="Number of events enqueued")
    dropped: int = Field(..., ge=0, description="Number of events dropped")
    status: Literal["queued", "partial", "failed"] = Field(
        ..., description="Overall ingestion status"
    )


# =============================================================================
# Query Models
# =============================================================================


class EventQueryRequest(BaseModel):
    """Request model for querying raw events."""

    trace_id: str | None = Field(
        default=None, description="Filter by trace ID (all events for a request)"
    )
    span_id: str | None = Field(
        default=None, description="Filter by span ID (all events for a function)"
    )
    control_execution_id: str | None = Field(
        default=None, description="Filter by specific event ID"
    )
    agent_name: str | None = Field(
        default=None,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Filter by agent identifier",
    )
    control_ids: list[int] | None = Field(default=None, description="Filter by control IDs")
    actions: list[ActionDecision] | None = Field(default=None, description="Filter by actions")
    matched: bool | None = Field(default=None, description="Filter by matched status")
    check_stages: list[Literal["pre", "post"]] | None = Field(
        default=None, description="Filter by check stages"
    )
    applies_to: list[Literal["llm_call", "tool_call"]] | None = Field(
        default=None, description="Filter by call types"
    )
    start_time: datetime | None = Field(default=None, description="Filter events after this time")
    end_time: datetime | None = Field(default=None, description="Filter events before this time")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum events")
    offset: int = Field(default=0, ge=0, description="Pagination offset")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"},
                {
                    "agent_name": "my-agent",
                    "actions": ["deny", "observe"],
                    "start_time": "2025-01-09T00:00:00Z",
                    "limit": 50,
                },
            ]
        }
    }

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_agent_name(str(value))

    @field_validator("actions", mode="before")
    @classmethod
    def validate_actions_filter(cls, value: list[str] | None) -> list[ActionDecision] | None:
        if value is None:
            return None
        return validate_action_list(value)


class EventQueryResponse(BaseModel):
    """Response model for event queries."""

    events: list[ControlExecutionEvent] = Field(..., description="Matching events")
    total: int = Field(..., ge=0, description="Total matching events")
    limit: int = Field(..., description="Limit used in query")
    offset: int = Field(..., description="Offset used in query")


# =============================================================================
# Statistics Models
# =============================================================================


class ControlStats(BaseModel):
    """Aggregated statistics for a single control."""

    control_id: int = Field(..., description="Control ID")
    control_name: str = Field(..., description="Control name")
    execution_count: int = Field(..., ge=0, description="Total executions")
    match_count: int = Field(..., ge=0, description="Total matches")
    non_match_count: int = Field(..., ge=0, description="Total non-matches")
    deny_count: int = Field(..., ge=0, description="Deny actions")
    steer_count: int = Field(..., ge=0, description="Steer actions")
    observe_count: int = Field(..., ge=0, description="Observe actions")
    error_count: int = Field(..., ge=0, description="Evaluation errors")
    avg_confidence: float = Field(..., ge=0.0, le=1.0, description="Average confidence")
    avg_duration_ms: float | None = Field(default=None, ge=0, description="Average duration (ms)")


class StatsRequest(BaseModel):
    """Request model for aggregated statistics."""

    agent_name: str = Field(
        ...,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Agent identifier",
    )
    time_range: Literal["1m", "5m", "15m", "1h", "24h", "7d", "30d", "180d", "365d"] = Field(
        default="5m", description="Time range"
    )
    include_timeseries: bool = Field(
        default=False, description="Include time-series data points for trend visualization"
    )

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))


class TimeseriesBucket(BaseModel):
    """Single data point in a time-series."""

    timestamp: datetime = Field(..., description="Start time of the bucket (UTC)")

    @field_validator("timestamp")
    @classmethod
    def ensure_timezone_aware(cls, v: datetime) -> datetime:
        """Ensure timestamp is timezone-aware (UTC)."""
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    execution_count: int = Field(..., ge=0, description="Total executions in bucket")
    match_count: int = Field(..., ge=0, description="Matches in bucket")
    non_match_count: int = Field(..., ge=0, description="Non-matches in bucket")
    error_count: int = Field(..., ge=0, description="Errors in bucket")
    action_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Action breakdown: {deny, steer, observe}",
    )
    avg_confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Average confidence score"
    )
    avg_duration_ms: float | None = Field(default=None, ge=0, description="Average duration (ms)")


class StatsTotals(BaseModel):
    """Agent-level aggregate statistics."""

    execution_count: int = Field(..., ge=0, description="Total executions")
    match_count: int = Field(default=0, ge=0, description="Total matches")
    non_match_count: int = Field(default=0, ge=0, description="Total non-matches")
    error_count: int = Field(default=0, ge=0, description="Total errors")
    action_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Action breakdown for matches: {deny, steer, observe}",
    )
    timeseries: list[TimeseriesBucket] | None = Field(
        default=None,
        description="Time-series data points (only when include_timeseries=true)",
    )


class StatsResponse(BaseModel):
    """Response model for agent-level aggregated statistics."""

    agent_name: str = Field(
        ...,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Agent identifier",
    )
    time_range: str = Field(..., description="Time range used")
    totals: StatsTotals = Field(..., description="Agent-level aggregate statistics")
    controls: list[ControlStats] = Field(..., description="Per-control breakdown")

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))


class ControlStatsResponse(BaseModel):
    """Response model for control-level statistics."""

    agent_name: str = Field(
        ...,
        min_length=AGENT_NAME_MIN_LENGTH,
        pattern=AGENT_NAME_PATTERN,
        description="Agent identifier",
    )
    time_range: str = Field(..., description="Time range used")
    control_id: int = Field(..., description="Control ID")
    control_name: str = Field(..., description="Control name")
    stats: StatsTotals = Field(..., description="Control statistics")

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))
