"""The statement that decides which dispatcher owns a task, and nothing else.

Split out of :mod:`agent_control_server.services.agent_tasks` for the same
reason ``turn_locks.py`` is split out of the turn flow: this is the piece where
a plausible-looking read-then-write passes every test its author writes and
fails under exactly the concurrency it was added to prevent. Keeping it in one
short module means the contract can be read in one sitting.

**The claim is one statement.** ``UPDATE ... WHERE ... RETURNING``. Zero rows
back means somebody else holds it, which is a 409 and a signal to move to the
next task rather than to retry this one. The row is taken ``FOR UPDATE`` first,
which serialises concurrent claims on one task so the staleness comparison is
the only unfenced part of the claim rather than the whole of it - the same
shape, and the same reasoning, as ``acquire_turn_lock``.

**Three differences from the turn lock, all deliberate.**

``paused_quota`` is reclaimable. Quota exhaustion is the single most likely
moment for a dispatcher to be restarted, because it is when an operator
notices the fleet is stuck and intervenes. A task abandoned at that moment and
left out of the reclaim predicate becomes a permanent orphan: no queued poll
sees it, no reclaim matches it, and the partial unique index then blocks its
source ref from ever being imported again. The issue becomes un-runnable with
nothing in the console explaining why.

``running_unknown`` is not reclaimable by a dispatcher. It is the status for a
turn that timed out where nothing can prove the invocation died. A machine that
automatically resumes work that may still be running is the duplicated-email
failure with extra steps, so only a human clears it.

**The deadline is set once, on the first claim, and reclaim does not move it.**
A reclaim that reset it would let a task whose dispatcher keeps dying live
forever, one lease at a time, under a column whose whole purpose is to stop
that.

**Resume position is read from the steps.** Never from ``current_step``: a
dispatcher that died between a completed step and its own bookkeeping leaves
that counter behind, and the counter is the half that is allowed to be wrong.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from agent_control_models.tasks import (
    RECLAIMABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    AgentTaskStatus,
    AgentTaskStepStatus,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RECLAIM_DETAIL = "reclaimed after the dispatcher's lease expired"
"""Written onto every step this claim abandons.

The gap is made visible rather than papered over: a step in this state reached
the executor, may have spent money, and may have acted through a tool, and
nobody knows which."""

_RECLAIMABLE_SQL = ", ".join(f"'{status.value}'" for status in sorted(RECLAIMABLE_TASK_STATUSES))
_TERMINAL_SQL = ", ".join(f"'{status.value}'" for status in sorted(TERMINAL_TASK_STATUSES))


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    """What one successful claim established.

    ``prior_status`` is carried because the *safety argument* for resuming
    differs by it even where the arithmetic does not. A reclaimed ``running``
    task has an abandoned step at ``resume_step_index`` whose side effects, if
    any, already happened and are about to happen again. A reclaimed
    ``paused_quota`` task never reached the executor at all, which is the one
    genuinely safe retry in this design, and it is safe because of where the
    quota check sits rather than because anyone decided it was.
    """

    task_id: int
    prior_status: AgentTaskStatus
    resume_step_index: int
    reclaimed: bool
    abandoned_step_indexes: tuple[int, ...]
    deadline_at: dt.datetime
    heartbeat_at: dt.datetime
    chain_trace_id: str


async def claim_task(
    db: AsyncSession,
    *,
    namespace_key: str,
    task_key: str,
    instance_id: str,
    caller_hash: str | None,
    chain_trace_id: str,
    lease_seconds: float,
    deadline_seconds: float,
) -> ClaimOutcome | None:
    """Take the task for ``instance_id``, or return ``None`` if refused.

    Runs inside the caller's transaction and does not commit. ``None`` covers
    every refusal the claim predicate can express - the task is running under a
    live lease, it is terminal, it is ``blocked``, it is ``running_unknown`` -
    and the caller turns that into one conflict. Distinguishing them here would
    mean a second read that is wrong the moment it lands.

    ``chain_trace_id`` is minted by the server and applied only on the first
    claim. A reclaim keeps the original, because the chain is one chain however
    many processes carried it, and because the audited party never authors its
    own audit key.
    """
    prior = await db.execute(
        text(
            "SELECT id, status FROM agent_tasks "
            " WHERE namespace_key = :ns AND task_key = :key "
            "   FOR UPDATE"
        ),
        {"ns": namespace_key, "key": task_key},
    )
    prior_row = prior.first()
    if prior_row is None:
        return None
    prior_status = AgentTaskStatus(prior_row.status)

    claimed = await db.execute(
        text(
            "UPDATE agent_tasks "
            "   SET status = 'running', "
            "       claimed_by = :instance, "
            "       claimed_by_hash = :caller_hash, "
            "       claimed_at = now(), "
            "       heartbeat_at = now(), "
            # First claim starts the clock; a reclaim inherits it. Extending it
            # per reclaim would make the deadline unreachable by construction.
            "       deadline_at = CASE WHEN status = 'queued' "
            "                          THEN now() + (:deadline * interval '1 second') "
            "                          ELSE deadline_at END, "
            "       chain_trace_id = COALESCE(chain_trace_id, :trace), "
            "       failure_code = NULL, "
            "       failure_detail = NULL, "
            "       updated_at = now() "
            " WHERE namespace_key = :ns "
            "   AND task_key = :key "
            "   AND (status = 'queued' "
            f"        OR (status IN ({_RECLAIMABLE_SQL}) "
            "            AND heartbeat_at < now() - (:lease * interval '1 second'))) "
            "RETURNING id, deadline_at, heartbeat_at, chain_trace_id"
        ),
        {
            "ns": namespace_key,
            "key": task_key,
            "instance": instance_id,
            "caller_hash": caller_hash,
            "trace": chain_trace_id,
            "lease": float(lease_seconds),
            "deadline": float(deadline_seconds),
        },
    )
    row = claimed.first()
    if row is None:
        return None

    reclaimed = prior_status is not AgentTaskStatus.QUEUED
    abandoned = await _abandon_running_steps(db, task_id=row.id) if reclaimed else ()
    resume_step_index = await resume_step_index_for(db, task_id=row.id)
    return ClaimOutcome(
        task_id=row.id,
        prior_status=prior_status,
        resume_step_index=resume_step_index,
        reclaimed=reclaimed,
        abandoned_step_indexes=abandoned,
        deadline_at=row.deadline_at,
        heartbeat_at=row.heartbeat_at,
        chain_trace_id=row.chain_trace_id,
    )


async def heartbeat_task(
    db: AsyncSession,
    *,
    namespace_key: str,
    task_key: str,
    instance_id: str,
) -> tuple[dt.datetime, dt.datetime, AgentTaskStatus] | None:
    """Refresh the lease for the holder. ``None`` when the caller is not it.

    Fenced on ``claimed_by``, exactly as ``release_turn_lock`` is fenced on the
    in-flight trace, and for the same reason: the claim deliberately permits
    taking over a task whose holder appears to be gone, so an unfenced
    heartbeat from that holder's late cleanup would extend its *successor's*
    lease and two dispatchers would believe they held one task.

    Terminal tasks are excluded. A heartbeat against a finished task is a
    dispatcher that has lost track of what it is doing, and answering it
    cheerfully would keep it going.
    """
    result = await db.execute(
        text(
            "UPDATE agent_tasks "
            "   SET heartbeat_at = now(), updated_at = now() "
            " WHERE namespace_key = :ns "
            "   AND task_key = :key "
            "   AND claimed_by = :instance "
            f"   AND status NOT IN ({_TERMINAL_SQL}) "
            "RETURNING heartbeat_at, deadline_at, status"
        ),
        {"ns": namespace_key, "key": task_key, "instance": instance_id},
    )
    row = result.first()
    if row is None:
        return None
    return row.heartbeat_at, row.deadline_at, AgentTaskStatus(row.status)


async def resume_step_index_for(db: AsyncSession, *, task_id: int) -> int:
    """``MAX(step_index) WHERE status='completed'`` plus one, or zero.

    One formula covers all three prior statuses in the plan's resume table,
    and that is not a coincidence worth hiding: the step a ``running`` task
    abandoned and the step a ``paused_quota`` task was waiting to start are the
    same index. What differs between them is what re-running it costs, which is
    why the caller is told which one it was.
    """
    result = await db.execute(
        text(
            "SELECT COALESCE(MAX(step_index), -1) + 1 AS resume "
            "  FROM agent_task_steps "
            " WHERE task_id = :task_id AND status = :completed"
        ),
        {"task_id": task_id, "completed": AgentTaskStepStatus.COMPLETED.value},
    )
    return int(result.scalar_one())


async def _abandon_running_steps(db: AsyncSession, *, task_id: int) -> tuple[int, ...]:
    """Close out steps the previous holder left in flight.

    Marked rather than deleted. A step that was running when its dispatcher's
    lease expired is a thing that happened, and the row is the only record that
    a turn may have reached the executor and acted before anybody stopped
    watching.
    """
    result = await db.execute(
        text(
            "UPDATE agent_task_steps "
            "   SET status = :abandoned, "
            "       ended_at = now(), "
            "       failure_code = COALESCE(failure_code, :code), "
            "       failure_detail = COALESCE(failure_detail, :detail) "
            " WHERE task_id = :task_id AND status = :running "
            "RETURNING step_index"
        ),
        {
            "task_id": task_id,
            "abandoned": AgentTaskStepStatus.ABANDONED.value,
            "running": AgentTaskStepStatus.RUNNING.value,
            "code": "DISPATCHER_LEASE_EXPIRED",
            "detail": RECLAIM_DETAIL,
        },
    )
    return tuple(sorted(row.step_index for row in result))
