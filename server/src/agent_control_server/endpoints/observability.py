"""Observability API endpoints.

This module provides endpoints for:
1. Event ingestion (POST /events) - SDK sends batched events
2. Event queries (POST /events/query) - Query raw events by trace_id, etc.
3. Stats (GET /stats) - Aggregated statistics for dashboards

All endpoints declare operation-based auth dependencies.

Dependencies are stored on app.state during server lifespan (see main.py):
- app.state.event_ingestor: EventIngestor
- app.state.event_store: EventStore
"""

import logging
import time
from typing import Literal, cast

from agent_control_models import (
    BatchEventsRequest,
    BatchEventsResponse,
    ControlExecutionEvent,
    ControlStatsResponse,
    EventQueryRequest,
    EventQueryResponse,
    StatsResponse,
    StatsTotals,
)
from agent_control_models.traces import (
    TRACE_HOP_LIMIT_DEFAULT,
    TRACE_HOP_LIMIT_MAX,
    TraceResponse,
)
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..db import get_async_db
from ..models import AgentConfig
from ..observability.ingest.base import EventIngestor
from ..observability.store.base import (
    EventStore,
    TimeRange,
    get_bucket_size,
    parse_time_range,
)
from ..services.agent_names import normalize_agent_name_or_422
from ..services.traces import TraceService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
)


# =============================================================================
# Dependency Injection (via app.state)
# =============================================================================


def get_event_ingestor(request: Request) -> EventIngestor:
    """Get the event ingestor from app.state."""
    ingestor = getattr(request.app.state, "event_ingestor", None)
    if ingestor is None:
        raise RuntimeError("EventIngestor not initialized - check server startup")
    return cast(EventIngestor, ingestor)


def get_event_store(request: Request) -> EventStore:
    """Get the event store from app.state."""
    store = getattr(request.app.state, "event_store", None)
    if store is None:
        raise RuntimeError("EventStore not initialized - check server startup")
    return cast(EventStore, store)


# =============================================================================
# Event Ingestion
# =============================================================================


#: Reserved for values the server authors about an agent's configuration. A
#: client-supplied key carrying it is dropped on ingest; see below.
_SERVER_AUTHORED_METADATA_PREFIX = "agent_control."


async def _stamp_server_authored_config_metadata(
    events: list[ControlExecutionEvent],
    *,
    db: AsyncSession,
    namespace_key: str,
) -> None:
    """Record what the control plane believes each agent should be running.

    The SDK already reports ``reported.config_etag`` and ``reported.model_id``
    on every control execution event. Those are self-reports by the audited
    party, so on their own they answer nothing: an agent running a stale or
    forged configuration reports exactly what it likes. Stamping the server's
    own view beside them turns that into a queryable divergence rather than an
    invisible lie.

    Two properties this deliberately does *not* claim.

    The lookup is a **current-row read at ingest time, not a point-in-time
    one**, so the two values can disagree for entirely benign reasons when an
    event is ingested after a configuration change. Any runbook query built on
    this has to say so, because an operator with no guidance will trust the
    reported value - it is the one that reads like an answer.

    And the reserved ``agent_control.`` prefix is enforced **here**, on every
    incoming event, before anything is stamped. The SDK's event builder drops
    client-supplied keys carrying it too, but that half is a convenience rather
    than a control: an ingest request is client-authored end to end, so a
    process that wants to forge the server's view simply does not use the SDK.
    Stripping only where a configuration row happens to exist would leave the
    forgery working for every agent that has never been configured, which is
    most of them. The audited party must not be able to author its own audit
    record, so the strip is unconditional and the stamp is what refills it.

    One query per distinct agent name per batch, and only agents that actually
    have a configuration row are stamped.
    """
    if not events:
        return

    stripped: list[ControlExecutionEvent] = []
    for event in events:
        metadata = event.metadata or {}
        if any(key.startswith(_SERVER_AUTHORED_METADATA_PREFIX) for key in metadata):
            event.metadata = {
                key: value
                for key, value in metadata.items()
                if not key.startswith(_SERVER_AUTHORED_METADATA_PREFIX)
            }
            stripped.append(event)
    if stripped:
        logger.warning(
            "Dropped client-supplied %r metadata on %d ingested event(s): that "
            "prefix is reserved for server-authored values.",
            _SERVER_AUTHORED_METADATA_PREFIX,
            len(stripped),
        )

    agent_names = {event.agent_name for event in events if event.agent_name}
    if not agent_names:
        return

    result = await db.execute(
        select(AgentConfig.agent_name, AgentConfig.etag, AgentConfig.model_id).where(
            AgentConfig.namespace_key == namespace_key,
            AgentConfig.agent_name.in_(agent_names),
        )
    )
    current = {row.agent_name: (row.etag, row.model_id) for row in result}
    if not current:
        return

    for event in events:
        row = current.get(event.agent_name)
        if row is None:
            continue
        etag, model_id = row
        metadata = dict(event.metadata or {})
        metadata["agent_control.config_etag_current"] = etag
        metadata["agent_control.model_id_current"] = model_id
        event.metadata = metadata


@router.post(
    "/events",
    status_code=202,
    response_model=BatchEventsResponse,
)
async def ingest_events(
    request: BatchEventsRequest,
    db: AsyncSession = Depends(get_async_db),
    ingestor: EventIngestor = Depends(get_event_ingestor),
    principal: Principal = Depends(require_operation(Operation.OBSERVABILITY_WRITE)),
) -> BatchEventsResponse:
    """
    Ingest batched control execution events.

    Events are stored directly to the database with ~5-20ms latency.

    Before storage, each event is stamped with the server's own view of the
    reporting agent's configuration. See
    :func:`_stamp_server_authored_config_metadata`.

    Args:
        request: Batch of events to ingest
        db: Database session, used only for the configuration stamp
        ingestor: Event ingestor (injected)

    Returns:
        BatchEventsResponse with counts of received/processed/dropped
    """
    start_time = time.perf_counter()

    await _stamp_server_authored_config_metadata(
        request.events, db=db, namespace_key=principal.namespace_key
    )

    result = await ingestor.ingest(
        request.events,
        namespace_key=principal.namespace_key,
    )

    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.debug(
        f"Ingested {result.received} events "
        f"(processed={result.processed}, dropped={result.dropped}) in {duration_ms:.2f}ms"
    )

    # Determine status
    status: Literal["queued", "partial", "failed"]
    if result.dropped == 0:
        status = "queued"  # Keep "queued" for API compatibility
    elif result.processed > 0:
        status = "partial"
    else:
        status = "failed"

    return BatchEventsResponse(
        received=result.received,
        enqueued=result.processed,  # Map to "enqueued" for API compatibility
        dropped=result.dropped,
        status=status,
    )


# =============================================================================
# Event Queries (Raw Events)
# =============================================================================


@router.post(
    "/events/query",
    response_model=EventQueryResponse,
)
async def query_events(
    request: EventQueryRequest,
    store: EventStore = Depends(get_event_store),
    principal: Principal = Depends(require_operation(Operation.OBSERVABILITY_READ)),
) -> EventQueryResponse:
    """
    Query raw control execution events.

    Supports filtering by:
    - trace_id: Get all events for a request
    - span_id: Get all events for a function call
    - control_execution_id: Get a specific event
    - agent_name: Filter by agent
    - control_ids: Filter by controls
    - actions: Filter by actions (deny, steer, observe)
    - matched: Filter by matched status
    - check_stages: Filter by check stage (pre, post)
    - applies_to: Filter by call type (llm_call, tool_call)
    - start_time/end_time: Filter by time range

    Results are paginated with limit/offset.

    Args:
        request: Query parameters
        store: Event store (injected)

    Returns:
        EventQueryResponse with matching events and pagination info
    """
    return await store.query_events(request, namespace_key=principal.namespace_key)


# =============================================================================
# Traces
# =============================================================================


@router.get(
    "/traces/{trace_id}",
    response_model=TraceResponse,
    summary="Get the ordered hops of one trace",
    response_description="Hops in (timestamp, span_id) order",
)
async def get_trace(
    trace_id: str,
    limit: int = Query(
        TRACE_HOP_LIMIT_DEFAULT,
        ge=1,
        le=TRACE_HOP_LIMIT_MAX,
        description=(
            f"Maximum hops to return (default {TRACE_HOP_LIMIT_DEFAULT}, max "
            f"{TRACE_HOP_LIMIT_MAX}). A longer trace comes back with "
            "truncated=true and its full total_hop_count."
        ),
    ),
    db: AsyncSession = Depends(get_async_db),
    store: EventStore = Depends(get_event_store),
    principal: Principal = Depends(require_operation(Operation.OBSERVABILITY_READ)),
) -> TraceResponse:
    """Read one multi-agent chain as a sequence of control executions.

    The word here is deliberately *chain* and not *task*. A ``task`` in this
    product is a row in ``agent_tasks``: one unit of work in the dispatch
    ledger, with a key, a claim and a set of steps. This route is a different
    thing that used to share the word, and two meanings for one noun is how a
    reader ends up asking a trace rollup for a ledger row.

    Each hop is one control execution: which agent ran it, the team that agent
    belongs to now, the span and timestamp it reported, the control, and what
    the control decided.

    **This is not how a dispatch task's chain is read.** Hops here come
    exclusively from ``ControlExecutionEvent`` rows, which only the SDK writes,
    so an agent with no bound control that fired contributes zero hops and
    vanishes from the answer entirely - a three-agent chain where two have no
    controls renders as one agent, with nothing indicating the rest is missing.
    A task's chain is ``GET /agent-tasks/{task_key}/chain``, built from
    ``agent_task_steps``, which records every hop whether or not a control
    fired. This route is the forensic view of one of those hops.

    Hops are sorted by ``(timestamp, span_id)``, so a tie in timestamps still
    yields the same order on every read. The timestamps come from the clients
    that emitted the events, so this is an observed sequence and not a causal
    chain; hops that time could not separate carry ``out_of_order=true``.

    A trace with no events in the caller's namespace is a 404.
    """
    return await TraceService(db, store).get_trace(
        namespace_key=principal.namespace_key,
        trace_id=trace_id,
        limit=limit,
    )


# =============================================================================
# Statistics (Query-Time Aggregation)
# =============================================================================


@router.get(
    "/stats",
    response_model=StatsResponse,
)
async def get_stats(
    agent_name: str,
    time_range: TimeRange = "5m",
    include_timeseries: bool = False,
    store: EventStore = Depends(get_event_store),
    principal: Principal = Depends(require_operation(Operation.OBSERVABILITY_READ)),
) -> StatsResponse:
    """
    Get agent-level aggregated statistics.

    Returns totals across all controls plus per-control breakdown.
    Use /stats/controls/{control_id} for single control stats.

    Args:
        agent_name: Agent to get stats for
        time_range: Time range (1m, 5m, 15m, 1h, 24h, 7d, 30d, 180d, 365d)
        include_timeseries: Include time-series data points for trend visualization
        store: Event store (injected)

    Returns:
        StatsResponse with agent-level totals and per-control breakdown
    """
    agent_name = normalize_agent_name_or_422(agent_name)
    interval = parse_time_range(time_range)
    bucket_size = get_bucket_size(time_range) if include_timeseries else None

    result = await store.query_stats(
        agent_name,
        interval,
        control_id=None,
        include_timeseries=include_timeseries,
        bucket_size=bucket_size,
        namespace_key=principal.namespace_key,
    )

    return StatsResponse(
        agent_name=agent_name,
        time_range=time_range,
        totals=StatsTotals(
            execution_count=result.total_executions,
            match_count=result.total_matches,
            non_match_count=result.total_non_matches,
            error_count=result.total_errors,
            action_counts=result.action_counts,
            timeseries=result.timeseries,
        ),
        controls=result.stats,
    )


@router.get(
    "/stats/controls/{control_id}",
    response_model=ControlStatsResponse,
)
async def get_control_stats(
    control_id: int,
    agent_name: str,
    time_range: TimeRange = "5m",
    include_timeseries: bool = False,
    store: EventStore = Depends(get_event_store),
    principal: Principal = Depends(require_operation(Operation.OBSERVABILITY_READ)),
) -> ControlStatsResponse:
    """
    Get statistics for a single control.

    Returns stats for the specified control with optional time-series.

    Args:
        control_id: Control ID to get stats for
        agent_name: Agent to get stats for
        time_range: Time range (1m, 5m, 15m, 1h, 24h, 7d, 30d, 180d, 365d)
        include_timeseries: Include time-series data points for trend visualization
        store: Event store (injected)

    Returns:
        ControlStatsResponse with control stats and optional timeseries
    """
    agent_name = normalize_agent_name_or_422(agent_name)
    interval = parse_time_range(time_range)
    bucket_size = get_bucket_size(time_range) if include_timeseries else None

    result = await store.query_stats(
        agent_name,
        interval,
        control_id=control_id,
        include_timeseries=include_timeseries,
        bucket_size=bucket_size,
        namespace_key=principal.namespace_key,
    )

    # Get control name from the stats (should be exactly one)
    control_name = result.stats[0].control_name if result.stats else f"control-{control_id}"

    return ControlStatsResponse(
        agent_name=agent_name,
        time_range=time_range,
        control_id=control_id,
        control_name=control_name,
        stats=StatsTotals(
            execution_count=result.total_executions,
            match_count=result.total_matches,
            non_match_count=result.total_non_matches,
            error_count=result.total_errors,
            action_counts=result.action_counts,
            timeseries=result.timeseries,
        ),
    )


# =============================================================================
# Health / Status
# =============================================================================


@router.get(
    "/status",
    dependencies=[Depends(require_operation(Operation.OBSERVABILITY_READ))],
)
async def get_status(request: Request) -> dict:
    """
    Get observability system status.

    Returns basic health information.
    """
    return {
        "status": "ok",
        "ingestor_initialized": hasattr(request.app.state, "event_ingestor"),
        "store_initialized": hasattr(request.app.state, "event_store"),
    }
