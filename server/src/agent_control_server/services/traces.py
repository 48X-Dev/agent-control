"""Assembly of a trace into an ordered sequence of hops.

A trace is read back as the control executions that carried its trace ID,
sorted oldest first. Events carry no parent span, so the result is an observed
sequence rather than a causal graph: two hops sit next to each other because
their timestamps put them there, not because one caused the other.

The event store and the team tables are both filtered on the caller's
namespace, and agent names never cross from one namespace's events to another
namespace's teams.
"""

from __future__ import annotations

from agent_control_models.errors import ErrorCode
from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.traces import TraceHop, TraceResponse, TraceTeamRef
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import NotFoundError
from ..models import Team as TeamRow
from ..observability.store.base import EventStore
from .teams import TeamsService


class TraceService:
    """Reads one trace and labels each hop with the acting agent's team."""

    def __init__(self, db: AsyncSession, store: EventStore) -> None:
        self._db = db
        self._store = store

    async def get_trace(
        self, *, namespace_key: str, trace_id: str, limit: int
    ) -> TraceResponse:
        """Return the hops of ``trace_id``, oldest first, capped at ``limit``.

        A trace with more hops than the cap comes back with the earliest
        ``limit`` of them, ``truncated=true`` and the full ``total_hop_count``,
        so a caller can always tell a short trace from a clipped one.

        Raises ``NotFoundError`` when the namespace holds no events for the
        trace, which is also what an unknown trace ID looks like.
        """
        result = await self._store.query_trace(
            trace_id, namespace_key=namespace_key, limit=limit
        )
        if result.total == 0:
            raise NotFoundError(
                error_code=ErrorCode.RESOURCE_NOT_FOUND,
                detail=f"Trace '{trace_id}' has no recorded control executions",
                resource="Trace",
                resource_id=trace_id,
                hint=(
                    "Verify the trace ID and that its events belong to this "
                    "namespace. Events are only readable once ingestion has "
                    "written them."
                ),
            )

        teams = await TeamsService(self._db).teams_for_agents(
            namespace_key=namespace_key,
            agent_names=sorted({event.agent_name for event in result.events}),
        )
        hops = _build_hops(result.events, teams)

        return TraceResponse(
            trace_id=trace_id,
            hops=hops,
            hop_count=len(hops),
            total_hop_count=result.total,
            truncated=result.total > len(hops),
            limit=limit,
            out_of_order=any(hop.out_of_order for hop in hops),
        )


def _build_hops(
    events: list[ControlExecutionEvent], teams: dict[str, TeamRow]
) -> list[TraceHop]:
    """Turn ordered events into hops, flagging the ones time could not place.

    A hop is flagged when its timestamp is not strictly later than its
    predecessor's. That is the only ordering problem the data exposes: hops
    come from independent clients, so a genuinely skewed clock simply sorts
    into the wrong slot with nothing left behind to detect it.
    """
    hops: list[TraceHop] = []
    for event in events:
        team = teams.get(event.agent_name)
        hop = TraceHop(
            agent_name=event.agent_name,
            team=(
                TraceTeamRef(slug=team.slug, display_name=team.display_name)
                if team is not None
                else None
            ),
            span_id=event.span_id,
            timestamp=event.timestamp,
            control_name=event.control_name,
            action=event.action,
            matched=event.matched,
        )
        if hops and hop.timestamp <= hops[-1].timestamp:
            hop.out_of_order = True
        hops.append(hop)
    return hops
