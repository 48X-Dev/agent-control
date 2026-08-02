"""Store-level coverage for reading one trace.

``PostgresEventStore`` overrides ``query_trace`` with a single ordered,
counted query; the ``EventStore`` base keeps a default built on
``query_events`` so third-party stores need no change. Production only ever
exercises the override, so the default is covered here directly and the two
are checked against each other on the same data.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from agent_control_models.observability import (
    ControlExecutionEvent,
    EventQueryRequest,
    EventQueryResponse,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_control_server.models import DEFAULT_NAMESPACE_KEY
from agent_control_server.observability.store.base import EventStore, StatsResult
from agent_control_server.observability.store.postgres import PostgresEventStore

from .conftest import async_engine, engine

_BASE_TIME = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clear_event_table() -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM control_execution_events"))
    yield


def _store() -> PostgresEventStore:
    return PostgresEventStore(
        async_sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)
    )


def _event(
    *,
    trace_id: str,
    span_id: str,
    timestamp: datetime,
    agent_name: str | None = None,
) -> ControlExecutionEvent:
    return ControlExecutionEvent(
        trace_id=trace_id,
        span_id=span_id,
        agent_name=agent_name or f"agent-{uuid4().hex[:12]}",
        control_id=1,
        control_name="pii-check",
        check_stage="pre",
        applies_to="llm_call",
        action="observe",
        matched=False,
        confidence=0.5,
        timestamp=timestamp,
    )


class InMemoryEventStore(EventStore):
    """Minimal store that satisfies the ``query_events`` contract.

    Returns events newest first with a pre-pagination total, which is what the
    base ``query_trace`` default relies on. Only ``trace_id`` filtering is
    implemented, since that is all the default uses.
    """

    def __init__(self, events: list[ControlExecutionEvent]) -> None:
        self.events = events
        self.query_calls = 0

    async def store(
        self, events: list[ControlExecutionEvent], *, namespace_key: str
    ) -> int:
        del namespace_key
        self.events.extend(events)
        return len(events)

    async def query_stats(self, *args: object, **kwargs: object) -> StatsResult:
        raise NotImplementedError

    async def query_events(
        self, query: EventQueryRequest, *, namespace_key: str
    ) -> EventQueryResponse:
        del namespace_key
        self.query_calls += 1
        matching = [event for event in self.events if event.trace_id == query.trace_id]
        matching.sort(key=lambda event: _as_utc(event.timestamp), reverse=True)
        page = matching[query.offset : query.offset + query.limit]
        return EventQueryResponse(
            events=page,
            total=len(matching),
            limit=query.limit,
            offset=query.offset,
        )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# =============================================================================
# Postgres override
# =============================================================================


@pytest.mark.asyncio
async def test_postgres_query_trace_orders_oldest_first_and_reports_total() -> None:
    # Given: a trace stored out of order
    store = _store()
    trace_id = uuid4().hex
    await store.store(
        [
            _event(
                trace_id=trace_id,
                span_id="span-03",
                timestamp=_BASE_TIME + timedelta(seconds=2),
            ),
            _event(trace_id=trace_id, span_id="span-01", timestamp=_BASE_TIME),
            _event(
                trace_id=trace_id,
                span_id="span-02",
                timestamp=_BASE_TIME + timedelta(seconds=1),
            ),
        ],
        namespace_key=DEFAULT_NAMESPACE_KEY,
    )

    # When: the whole trace is read
    result = await store.query_trace(
        trace_id, namespace_key=DEFAULT_NAMESPACE_KEY, limit=10
    )

    # Then: events come back oldest first with the full count
    assert [event.span_id for event in result.events] == [
        "span-01",
        "span-02",
        "span-03",
    ]
    assert result.total == 3


@pytest.mark.asyncio
async def test_postgres_query_trace_caps_the_page_but_not_the_total() -> None:
    store = _store()
    trace_id = uuid4().hex
    await store.store(
        [
            _event(
                trace_id=trace_id,
                span_id=f"span-{index:02d}",
                timestamp=_BASE_TIME + timedelta(seconds=index),
            )
            for index in range(1, 6)
        ],
        namespace_key=DEFAULT_NAMESPACE_KEY,
    )

    result = await store.query_trace(
        trace_id, namespace_key=DEFAULT_NAMESPACE_KEY, limit=2
    )

    assert [event.span_id for event in result.events] == ["span-01", "span-02"]
    assert result.total == 5


@pytest.mark.asyncio
async def test_postgres_query_trace_is_empty_for_an_unknown_trace() -> None:
    store = _store()

    result = await store.query_trace(
        uuid4().hex, namespace_key=DEFAULT_NAMESPACE_KEY, limit=10
    )

    assert result.events == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_postgres_query_trace_is_scoped_to_the_namespace() -> None:
    # Given: the same trace ID stored under two namespaces
    store = _store()
    trace_id = uuid4().hex
    await store.store(
        [_event(trace_id=trace_id, span_id="span-01", timestamp=_BASE_TIME)],
        namespace_key="ns-a",
    )
    await store.store(
        [
            _event(
                trace_id=trace_id,
                span_id="span-02",
                timestamp=_BASE_TIME + timedelta(seconds=1),
            ),
            _event(
                trace_id=trace_id,
                span_id="span-03",
                timestamp=_BASE_TIME + timedelta(seconds=2),
            ),
        ],
        namespace_key="ns-b",
    )

    # When/Then: each namespace reads only its own events
    ns_a = await store.query_trace(trace_id, namespace_key="ns-a", limit=10)
    assert ns_a.total == 1
    assert [event.span_id for event in ns_a.events] == ["span-01"]

    ns_b = await store.query_trace(trace_id, namespace_key="ns-b", limit=10)
    assert ns_b.total == 2
    assert [event.span_id for event in ns_b.events] == ["span-02", "span-03"]

    assert (
        await store.query_trace(trace_id, namespace_key="ns-c", limit=10)
    ).total == 0


# =============================================================================
# Base default, and parity with the override
# =============================================================================


@pytest.mark.asyncio
async def test_base_default_matches_the_postgres_override_on_the_same_trace() -> None:
    # Given: a trace in Postgres
    store = _store()
    trace_id = uuid4().hex
    await store.store(
        [
            _event(
                trace_id=trace_id,
                span_id=f"span-{index:02d}",
                timestamp=_BASE_TIME + timedelta(seconds=index),
            )
            for index in (3, 1, 4, 2)
        ],
        namespace_key=DEFAULT_NAMESPACE_KEY,
    )

    # When: it is read through the override and through the inherited default
    override = await store.query_trace(
        trace_id, namespace_key=DEFAULT_NAMESPACE_KEY, limit=3
    )
    default = await EventStore.query_trace(
        store, trace_id, namespace_key=DEFAULT_NAMESPACE_KEY, limit=3
    )

    # Then: both return the same page and the same total
    assert [event.span_id for event in default.events] == [
        event.span_id for event in override.events
    ]
    assert default.total == override.total == 4


@pytest.mark.asyncio
async def test_base_default_reads_the_earliest_page_of_a_long_trace() -> None:
    # Given: a store whose query_events returns newest first
    trace_id = uuid4().hex
    store = InMemoryEventStore(
        [
            _event(
                trace_id=trace_id,
                span_id=f"span-{index:02d}",
                timestamp=_BASE_TIME + timedelta(seconds=index),
            )
            for index in range(1, 6)
        ]
    )

    # When: the default reads a capped page
    result = await store.query_trace(trace_id, namespace_key="default", limit=2)

    # Then: it offsets from the end of that ordering to get the oldest hops
    assert [event.span_id for event in result.events] == ["span-01", "span-02"]
    assert result.total == 5


@pytest.mark.asyncio
async def test_base_default_is_empty_for_an_unknown_trace_without_a_second_query() -> (
    None
):
    store = InMemoryEventStore([])

    result = await store.query_trace(uuid4().hex, namespace_key="default", limit=10)

    assert result.events == []
    assert result.total == 0
    assert store.query_calls == 1


@pytest.mark.asyncio
async def test_base_default_sorts_naive_timestamps_as_utc() -> None:
    # Given: a trace mixing naive and aware client clocks
    trace_id = uuid4().hex
    store = InMemoryEventStore(
        [
            _event(
                trace_id=trace_id,
                span_id="span-02",
                timestamp=(_BASE_TIME + timedelta(seconds=1)).replace(tzinfo=None),
            ),
            _event(trace_id=trace_id, span_id="span-01", timestamp=_BASE_TIME),
            _event(
                trace_id=trace_id,
                span_id="span-03",
                timestamp=_BASE_TIME + timedelta(seconds=2),
            ),
        ]
    )

    # When: the default orders them
    result = await store.query_trace(trace_id, namespace_key="default", limit=10)

    # Then: the naive timestamp is read as UTC rather than raising on comparison
    assert [event.span_id for event in result.events] == [
        "span-01",
        "span-02",
        "span-03",
    ]


@pytest.mark.asyncio
async def test_base_default_breaks_timestamp_ties_by_span_id() -> None:
    trace_id = uuid4().hex
    store = InMemoryEventStore(
        [
            _event(trace_id=trace_id, span_id="span-zz", timestamp=_BASE_TIME),
            _event(trace_id=trace_id, span_id="span-aa", timestamp=_BASE_TIME),
        ]
    )

    result = await store.query_trace(trace_id, namespace_key="default", limit=10)

    assert [event.span_id for event in result.events] == ["span-aa", "span-zz"]
