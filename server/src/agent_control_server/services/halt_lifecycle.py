"""What a turn's start and end do to the stops recorded against it.

Split out of :mod:`services.halts` because the caller is different in kind: the
turn lock's acquire and release paths work in raw SQL inside transactions they
own, and they must not grow a dependency on the request-shaped service next
door. Keeping these two statements together also keeps the rule they share
readable as one rule.

**Expiry is an event, and the event is the next acquire.** There is no sweeper
in this codebase and this is deliberately not the place to invent one. A turn
that ends stamps its own halt from the release path; a replica that dies stamps
nothing, so the next acquire ages out anything still bound to an earlier turn.
That closes the replica-death case by construction rather than by assertion.

**Lock order: ``agent_sessions`` first.** Both statements below run after the
caller has taken the session row, and reversing that anywhere deadlocks in
exactly the race this design exists for - a halt claimed at the instant its turn
ends - inside the one code path that guarantees the turn lock gets released.
"""

from __future__ import annotations

from agent_control_models.halts import HaltStatus
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .executor_metrics import HALTS_APPLIED_STILL_IN_FLIGHT, HALTS_EXPIRED


async def expire_halts_from_earlier_turns(
    db: AsyncSession,
    *,
    namespace_key: str,
    session_id: int,
    new_trace_id: str,
) -> int:
    """Age out stops bound to any turn but the one starting now.

    This is the replica-death case, and it is the reason expiry lives inside
    the acquire rather than in a sweep. A halt whose claiming process died sits
    ``pending`` forever; nothing else in this system would ever look at it
    again, and the join in :meth:`HaltsService.apply_at_boundary` already
    refuses to deliver it. This statement is what stops it lingering in the
    console as a stop that never landed.

    An ``applied`` halt from an earlier turn that never got a ``turn_ended_at``
    is caught in the same statement, and it is worth counting separately: it
    means an executor acknowledged a stop and this server never saw that turn
    end. The acknowledgement comes from the party being stopped, so the two
    disagreeing is exactly the case a console must not render as a clean stop.

    Runs inside the acquire transaction, which has already locked the session
    row.
    """
    result = await db.execute(
        text(
            "UPDATE agent_session_halts "
            "   SET status = CASE WHEN status = 'pending' "
            "                     THEN 'expired' ELSE status END, "
            "       turn_ended_at = COALESCE(turn_ended_at, now()) "
            " WHERE namespace_key = :ns "
            "   AND session_id = :id "
            "   AND status IN ('pending', 'applied') "
            "   AND turn_ended_at IS NULL "
            "   AND target_trace_id <> :trace "
            "RETURNING status"
        ),
        {"ns": namespace_key, "id": session_id, "trace": new_trace_id},
    )
    rows = result.fetchall()
    for row in rows:
        if row[0] == HaltStatus.EXPIRED.value:
            HALTS_EXPIRED.inc()
        else:
            HALTS_APPLIED_STILL_IN_FLIGHT.inc()
    return len(rows)


async def close_halts_for_turn(
    db: AsyncSession,
    *,
    namespace_key: str,
    session_id: int,
    trace_id: str,
) -> None:
    """Stamp this turn's halt with the moment the turn really ended.

    ``turn_ended_at`` is the only state a console may render as *stopped*.
    ``applied`` on its own is an assertion by the process being stopped, and
    the consequence of believing it wrongly is the side effect the human was
    trying to prevent. This server observes the ending independently, which is
    what this statement records.

    A halt still ``pending`` when its turn ends becomes ``expired``: the turn
    finished before the stop reached a boundary, which is a different outcome
    from being stopped and reads differently in the transcript.

    Runs inside the release transaction, after the session row has been
    updated, so the lock order is unchanged.
    """
    result = await db.execute(
        text(
            "UPDATE agent_session_halts "
            "   SET turn_ended_at = COALESCE(turn_ended_at, now()), "
            "       status = CASE WHEN status = 'pending' "
            "                     THEN 'expired' ELSE status END "
            " WHERE namespace_key = :ns "
            "   AND session_id = :id "
            "   AND target_trace_id = :trace "
            "RETURNING status"
        ),
        {"ns": namespace_key, "id": session_id, "trace": trace_id},
    )
    for row in result.fetchall():
        if row[0] == HaltStatus.EXPIRED.value:
            HALTS_EXPIRED.inc()
