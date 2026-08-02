"""Base interfaces for event storage.

This module defines the EventStore ABC that all event stores must implement.
The interface supports raw event storage and query-time aggregation (no pre-aggregation).

Built-in implementations:
    - PostgresEventStore: Postgres with JSONB storage and query-time aggregation

Custom implementations users can create:
    - ClickhouseEventStore: Native JSON + columnar = fast aggregation
    - TimescaleDBEventStore: Time-series optimized Postgres extension
    - ElasticsearchEventStore: Full-text search capabilities
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Literal

from agent_control_models.observability import (
    ControlExecutionEvent,
    ControlStats,
    EventQueryRequest,
    EventQueryResponse,
    TimeseriesBucket,
)
from pydantic import BaseModel, Field

# Type alias for time range literals
TimeRange = Literal["1m", "5m", "15m", "1h", "24h", "7d", "30d", "180d", "365d"]


class StatsResult(BaseModel):
    """Result of a stats query.

    Contains per-control statistics and totals, aggregated at query time
    from raw events.

    Invariant: total_executions = total_matches + total_non_matches + total_errors

    Matches have actions (deny, steer, observe) tracked in action_counts.
    sum(action_counts.values()) == total_matches

    Attributes:
        stats: List of per-control statistics
        total_executions: Total executions across all controls
        total_matches: Total matches across all controls (evaluator matched)
        total_non_matches: Total non-matches across all controls (evaluator didn't match)
        total_errors: Total errors across all controls (evaluation failed)
        action_counts: Breakdown of actions for matched executions
        timeseries: Optional time-series data points
    """

    stats: list[ControlStats] = Field(default_factory=list, description="Per-control statistics")
    total_executions: int = Field(default=0, ge=0, description="Total executions")
    total_matches: int = Field(default=0, ge=0, description="Total matches")
    total_non_matches: int = Field(default=0, ge=0, description="Total non-matches")
    total_errors: int = Field(default=0, ge=0, description="Total errors")
    action_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Action breakdown for matches: {deny, steer, observe}",
    )
    timeseries: list[TimeseriesBucket] | None = Field(
        default=None,
        description="Time-series data points (only when include_timeseries=true)",
    )


class TraceEventsResult(BaseModel):
    """Events belonging to one trace, ordered oldest first.

    Attributes:
        events: Events sorted by (timestamp, span_id), capped at the requested limit
        total: Number of events the trace has in total, before the cap
    """

    events: list[ControlExecutionEvent] = Field(
        default_factory=list, description="Events ordered by (timestamp, span_id)"
    )
    total: int = Field(default=0, ge=0, description="Total events in the trace")


# Re-export query types from models for convenience
EventQuery = EventQueryRequest
EventQueryResult = EventQueryResponse


# Time range string to timedelta mapping
TIME_RANGE_MAP: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "180d": timedelta(days=180),
    "365d": timedelta(days=365),
}


def parse_time_range(time_range: TimeRange) -> timedelta:
    """Convert time range string to timedelta."""
    return TIME_RANGE_MAP[time_range]


# Bucket size mapping for time-series data
# Aims for 6-30 data points per time range for clean charts
BUCKET_SIZE_MAP: dict[str, timedelta] = {
    "1m": timedelta(seconds=10),   # 6 buckets
    "5m": timedelta(seconds=30),   # 10 buckets
    "15m": timedelta(minutes=1),   # 15 buckets
    "1h": timedelta(minutes=5),    # 12 buckets
    "24h": timedelta(hours=1),     # 24 buckets
    "7d": timedelta(hours=6),      # 28 buckets
    "30d": timedelta(days=1),      # 30 buckets
    "180d": timedelta(days=7),     # ~26 buckets
    "365d": timedelta(days=30),    # ~12 buckets
}


def get_bucket_size(time_range: TimeRange) -> timedelta:
    """Get bucket size for a time range."""
    return BUCKET_SIZE_MAP[time_range]


def _trace_order_key(event: ControlExecutionEvent) -> tuple[datetime, str]:
    """Sort key placing trace events oldest first, ties broken by span.

    Naive timestamps are read as UTC so a trace mixing naive and aware client
    clocks stays sortable.
    """
    timestamp = event.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp, event.span_id


class EventStore(ABC):
    """Storage backend for observability events.

    This ABC defines the interface for event storage. Implementations
    store raw events and perform aggregation at query time (no pre-aggregation).

    All methods are async to support both sync and async database drivers.
    """

    @abstractmethod
    async def store(
        self,
        events: list[ControlExecutionEvent],
        *,
        namespace_key: str,
    ) -> int:
        """Store raw events.

        Args:
            events: List of control execution events to store
            namespace_key: Namespace that owns the stored events

        Returns:
            Number of events successfully stored
        """
        pass

    @abstractmethod
    async def query_stats(
        self,
        agent_name: str,
        time_range: timedelta,
        *,
        control_id: int | None = None,
        include_timeseries: bool = False,
        bucket_size: timedelta | None = None,
        namespace_key: str,
    ) -> StatsResult:
        """Query stats (aggregated at query time from raw events).

        Args:
            agent_name: Identifier of the agent to query stats for
            time_range: Time range to aggregate over (from now)
            control_id: Optional control ID to filter by
            include_timeseries: Whether to include time-series data
            bucket_size: Bucket size for time-series (required if include_timeseries=True)
            namespace_key: Namespace whose events should be queried

        Returns:
            StatsResult with per-control and total statistics
        """
        pass

    @abstractmethod
    async def query_events(
        self,
        query: EventQuery,
        *,
        namespace_key: str,
    ) -> EventQueryResult:
        """Query raw events with filters and pagination.

        Implementations must return events newest first (timestamp descending)
        and must report the pre-pagination match count in ``total``. The
        default :meth:`query_trace` reads the oldest page by offsetting from
        the end of that ordering, so a store that orders differently has to
        override ``query_trace`` as well.

        Args:
            query: Query parameters (filters, pagination)
            namespace_key: Namespace whose events should be queried

        Returns:
            EventQueryResult with matching events and pagination info
        """
        pass

    async def query_trace(
        self,
        trace_id: str,
        *,
        namespace_key: str,
        limit: int,
    ) -> TraceEventsResult:
        """Read one trace's events, oldest first, capped at ``limit``.

        Ordering is by ``(timestamp, span_id)`` so that events sharing a
        timestamp still come back in a stable order.

        This default is built on :meth:`query_events` so existing stores keep
        working without changes. It costs two round trips and orders the page
        in Python; back ends that can sort and count in the query should
        override it. Because the count and the page come from separate reads,
        events written between the two shift the offset and can push the
        oldest hops out of the page, so a store under concurrent ingestion
        wants the override rather than this fallback.

        Args:
            trace_id: Trace to read
            namespace_key: Namespace whose events should be queried
            limit: Maximum events to return

        Returns:
            TraceEventsResult with the ordered page and the trace's total size
        """
        probe = await self.query_events(
            EventQueryRequest(trace_id=trace_id, limit=1),
            namespace_key=namespace_key,
        )
        if probe.total == 0:
            return TraceEventsResult(events=[], total=0)

        # query_events returns newest first, so the oldest events sit at the
        # end of the result set; offsetting from there yields the earliest page.
        window = min(limit, probe.total)
        page = await self.query_events(
            EventQueryRequest(
                trace_id=trace_id, limit=window, offset=probe.total - window
            ),
            namespace_key=namespace_key,
        )
        return TraceEventsResult(
            events=sorted(page.events, key=_trace_order_key),
            total=probe.total,
        )

    async def close(self) -> None:
        """Close any resources held by the store.

        Override in implementations that need cleanup (e.g., connection pools).
        """
        pass
