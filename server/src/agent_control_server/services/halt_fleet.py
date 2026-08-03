"""Level 2 of the fleet stop: one statement, every running turn in a namespace.

Best-effort, and the console must say so. Three decisions inside six lines of
SQL, each of which was the difference between a control and a comforting button.

**Not a loop over** ``HaltsService.create``. That function calls
``_enforce_quota``, bucketed per ``(namespace_key, caller_hash)`` on a sliding
sixty seconds with a default of thirty. A loop over more than thirty sessions
429s partway through, under exactly the condition that motivates a fleet stop:
many agents running at once. **A safety control that degrades as the incident
grows is worse than none, because it is trusted.** One statement, one
transaction, cannot half-succeed. The quota is deliberately not applied here:
the operation is ADMIN and the statement is idempotent.

**It selects on** ``in_flight_trace_id``, **not** ``in_flight_since``. The
insert copies that column into ``target_trace_id``, which is NOT NULL, so
selecting on the other one would produce zero halts for exactly the rows the
insert needs. It also skips every timed-out turn, because a 504 releases with
``turn_ended=False``: the lock clears and the marker is deliberately retained,
and a turn whose invocation is still running is the one a fleet stop most wants
to reach.

**It fails open, and that is said out loud.** Halt delivery is best-effort at
the executor: ``nudges.py`` returns ``None`` when the backoff is not clear, and
the post swallows timeouts and HTTP errors, and in both cases the tool runs. So
when the control plane is unreachable, or has just been erroring, no halt is
claimed at any boundary. Level 2 is a request that lands only when the executor
can still reach us. **None of levels 1 to 3 kills a tool that is already
executing, and a process kill does not unwind the email.**

Lock order, obeyed here as everywhere: ``agent_sessions`` first. The insert
reads the session rows in its own ``SELECT``, so the halt table is only ever
reached through them.
"""

from __future__ import annotations

import logging

from agent_control_models.dispatch import HaltFleetResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .executor_metrics import FLEET_HALTS_REQUESTED

_logger = logging.getLogger(__name__)

_COUNT_IN_FLIGHT = text(
    "SELECT count(*) AS total, "
    "       count(*) FILTER (WHERE agent_task_id IS NOT NULL) AS dispatch "
    "  FROM agent_sessions "
    " WHERE namespace_key = :ns AND in_flight_trace_id IS NOT NULL"
)

_HALT_EVERY_LIVE_TURN = text(
    "INSERT INTO agent_session_halts "
    "       (namespace_key, session_id, target_trace_id, mode, status, created_by_hash) "
    "SELECT s.namespace_key, s.id, s.in_flight_trace_id, 'graceful', 'pending', :hash "
    "  FROM agent_sessions s "
    " WHERE s.namespace_key = :ns "
    "   AND s.in_flight_trace_id IS NOT NULL "
    "ON CONFLICT ON CONSTRAINT uq_agent_session_halts_turn DO NOTHING "
    "RETURNING id"
)


async def halt_fleet(
    db: AsyncSession, *, namespace_key: str, caller_hash: str | None
) -> HaltFleetResponse:
    """Bind a stop to every turn running in this namespace. Returns what it did.

    The count is taken before the insert, in the same transaction, so the gap
    between it and ``halts_created`` is exactly the turns that already had a
    stop bound to them rather than a sampling artefact.

    Human chat sessions are included, and that is intentional: an operator
    reaching for a fleet stop is stopping the deployment, not a subset of it.
    The response says how many of the affected turns belonged to a dispatch task
    so the console can be honest about what else it just interrupted.
    """
    counts = (await db.execute(_COUNT_IN_FLIGHT, {"ns": namespace_key})).mappings().one()
    in_flight = int(counts["total"] or 0)
    dispatch_in_flight = int(counts["dispatch"] or 0)

    inserted = await db.execute(
        _HALT_EVERY_LIVE_TURN, {"ns": namespace_key, "hash": caller_hash}
    )
    created = len(inserted.scalars().all())
    FLEET_HALTS_REQUESTED.inc(created)

    _logger.warning(
        "Fleet stop requested. namespace=%s in_flight=%s halts_created=%s "
        "dispatch_in_flight=%s. Best-effort: a halt lands only at a boundary the "
        "executor reaches, and no tool already executing is stopped by it.",
        namespace_key,
        in_flight,
        created,
        dispatch_in_flight,
    )
    return HaltFleetResponse(
        sessions_in_flight=in_flight,
        halts_created=created,
        already_halted=max(0, in_flight - created),
        dispatch_sessions_in_flight=dispatch_in_flight,
    )
