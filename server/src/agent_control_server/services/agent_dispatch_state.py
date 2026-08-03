"""The ceilings on the dispatch loop, and the switches that stop it.

The loop runs in another process. Every number that bounds it is here, and the
reason is one sentence the project has defended repeatedly: **a budget enforced
by the process being budgeted is not a control.** A dispatcher in a retry loop,
a second dispatcher started by a different operator, a bad release, or any
holder of an ordinary authenticated key calling ``POST /turns`` directly all
spend without consulting a limit that lives in the dispatcher's own memory.

Four things in this module are load-bearing.

**Charging a turn is one statement.** ``INSERT ... ON CONFLICT DO UPDATE ...
WHERE ... RETURNING``, exactly the shape ``turn_locks.py`` uses and for the same
reason: a read followed by a write would pass every test its author wrote and
fail under the concurrency it was added to prevent, with two dispatch turns both
reading "under budget" and both spending. The statement creates the row on first
use, rolls the window when it has expired, increments the counter, and refuses -
by returning zero rows - when either switch is thrown or the allowance is gone.

**A refusal is diagnosed by a second read, never guessed.** Zero rows is
ambiguous between paused, halted and exhausted, and the caller needs to be told
which: a pause clears in a minute and an exhausted budget clears at the top of
the hour. The read happens in the same transaction, after the failed write, so
what it reports is what refused.

**It is a hot row, and that is bounded rather than denied.** Only turns for a
session with ``agent_task_id`` set are *charged*; human chat keeps the existing
in-process ``TurnQuota`` and writes nothing here. The one thing every turn does
read is the kill switch, because level 3's copy promises it stops human chat
too, and that is a primary-key read of a row most namespaces have never
created. Fleet turns are limited to
tens per hour by the very ceiling being enforced, so contention on this row is
by construction not a problem. It has to be a row rather than a process-local
bucket because ``turn_quota.py``'s own docstring says its bucket is per process:
*"With N replicas a principal gets N times the configured allowance."* Roughly
right is fine for a rate limit on human chat. It is not fine for the ceiling
that stops an autonomous loop, where the observed limit being an unknown
multiple of the configured one is the whole failure.

**The window is fixed, not sliding.** Two integers and one statement, at the
cost of allowing up to twice the ceiling across a window boundary. A sliding
window would need a row per turn to count over. For a ceiling whose job is to
stop a loop before it spends a fortune, an allowance that is occasionally 2x and
never unbounded is the right trade, and it is the only shape that fits in one
statement on the turn path.

Nothing here starts anything. There is no interval, no poll, no worker and no
timer, and if one appears in this module the architectural line has been
crossed.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from agent_control_models.dispatch import (
    DISPATCH_WINDOW_SECONDS,
    DispatchBudget,
    DispatchStateSnapshot,
)
from agent_control_models.errors import ErrorCode, ErrorReason
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import DispatchSettings, ExecutorSettings
from ..errors import APIError
from .executor_metrics import (
    TURN_REJECT_BUDGET,
    TURN_REJECT_CONCURRENCY,
    TURN_REJECT_HALTED,
    TURN_REJECT_PAUSED,
    TURNS_REJECTED,
)

_WINDOW = float(DISPATCH_WINDOW_SECONDS)

_MIN_RETRY_AFTER_SECONDS = 1.0
"""Never hand back zero. A dispatcher told to retry immediately retries
immediately, and the row it is waiting on has not moved."""


@dataclass(frozen=True, slots=True)
class _StateRow:
    """One namespace's dispatch row, read out as a value.

    A plain value rather than an ORM row, matching ``_TurnTarget`` next door:
    every caller here reads it after its statement has run and some of them read
    it in a transaction that is about to be rolled back, so an attribute that
    could lazy-load would be IO on a connection nobody expects to use.
    """

    max_tasks_per_hour: int
    max_turns_per_hour: int
    turns_window_start: dt.datetime
    turns_in_window: int
    paused_at: dt.datetime | None
    paused_by: str | None
    paused_reason: str | None
    halted_at: dt.datetime | None
    halted_by: str | None
    halted_reason: str | None
    updated_at: dt.datetime


# The window predicate, written once. Repeating it inline three times in one
# statement is how the roll condition and the charge condition drift apart.
_WINDOW_EXPIRED = (
    "agent_dispatch_state.turns_window_start <= now() - (:window * interval '1 second')"
)

_CHARGE_TURN = text(
    "INSERT INTO agent_dispatch_state "
    "       (namespace_key, max_tasks_per_hour, max_turns_per_hour, "
    "        turns_window_start, turns_in_window, updated_at) "
    "VALUES (:ns, :max_tasks, :max_turns, now(), 1, now()) "
    "ON CONFLICT (namespace_key) DO UPDATE "
    f"   SET turns_window_start = CASE WHEN {_WINDOW_EXPIRED} THEN now() "
    "           ELSE agent_dispatch_state.turns_window_start END, "
    f"       turns_in_window = CASE WHEN {_WINDOW_EXPIRED} THEN 1 "
    "           ELSE agent_dispatch_state.turns_in_window + 1 END, "
    "       updated_at = now() "
    # Both switches, then the allowance. The ceiling is re-checked on the
    # window-roll branch too: without ``max_turns_per_hour >= 1`` a namespace
    # whose ceiling had been set to zero would still get one turn every hour,
    # which is a ceiling of zero that is not zero.
    " WHERE agent_dispatch_state.dispatch_paused_at IS NULL "
    "   AND agent_dispatch_state.executors_halted_at IS NULL "
    f"   AND (({_WINDOW_EXPIRED} AND agent_dispatch_state.max_turns_per_hour >= 1) "
    "        OR agent_dispatch_state.turns_in_window "
    "           < agent_dispatch_state.max_turns_per_hour) "
    "RETURNING turns_in_window, max_turns_per_hour, turns_window_start"
)

_READ_STATE = text(
    "SELECT namespace_key, max_tasks_per_hour, max_turns_per_hour, "
    "       turns_window_start, turns_in_window, "
    "       dispatch_paused_at, dispatch_paused_by, dispatch_paused_reason, "
    "       executors_halted_at, executors_halted_by, executors_halted_reason, "
    "       updated_at "
    "  FROM agent_dispatch_state WHERE namespace_key = :ns"
)

_ENSURE_ROW = text(
    "INSERT INTO agent_dispatch_state "
    "       (namespace_key, max_tasks_per_hour, max_turns_per_hour) "
    "VALUES (:ns, :max_tasks, :max_turns) "
    "ON CONFLICT (namespace_key) DO NOTHING"
)

_LOCK_ROW = text(
    "SELECT max_tasks_per_hour FROM agent_dispatch_state "
    " WHERE namespace_key = :ns FOR UPDATE"
)

_COUNT_TASKS_IN_WINDOW = text(
    "SELECT count(*) FROM agent_tasks "
    " WHERE namespace_key = :ns "
    "   AND created_at > now() - (:window * interval '1 second')"
)

_COUNT_AGENT_TURNS_IN_FLIGHT = text(
    "SELECT count(*) FROM agent_sessions "
    " WHERE namespace_key = :ns "
    "   AND agent_name = :agent "
    "   AND agent_task_id IS NOT NULL "
    "   AND session_key <> :key "
    # ``in_flight_trace_id`` rather than ``in_flight_since``: a turn that timed
    # out clears the lock and deliberately keeps the marker, because the
    # truthful answer to "is this agent doing something" is still yes and that
    # invocation is still spending.
    "   AND in_flight_trace_id IS NOT NULL "
    # Bounded by the same staleness window that lets the session's *own* lock be
    # taken over. Past it the server already permits a second turn on that very
    # session, so refusing a different one on the strength of the same marker
    # would be the stricter half of an inconsistent pair - and a task requeued
    # after a human resolved its timeout would find its agent blocked forever by
    # a session nothing will ever clear.
    "   AND COALESCE(in_flight_since, last_activity_at) "
    "       > now() - (:stale * interval '1 second')"
)


async def charge_dispatch_turn(
    db: AsyncSession,
    *,
    namespace_key: str,
    agent_name: str,
    session_key: str,
    dispatch: DispatchSettings,
    executor: ExecutorSettings,
) -> None:
    """Refuse or charge one dispatch-origin turn. Runs in the caller's transaction.

    Called from ``_acquire_turn`` for sessions with ``agent_task_id`` set, and
    for nothing else. Four refusals in order of authority: the executors are
    halted, dispatch is paused, the namespace has spent its hour, or this
    agent is already running a dispatch turn.

    The transaction must not be committed unless the turn actually starts. Every
    refusal path in ``_acquire_turn`` - including the one for a session that is
    already running a turn - unwinds without committing, which un-charges the
    counter this moved. A turn that never happened must not be billed.

    The per-agent check runs *after* the charge on purpose. The charge takes an
    exclusive lock on this namespace's one state row and holds it to commit, so
    every concurrent dispatch turn in the namespace is serialized behind it and
    the count below cannot be read by two callers who then both proceed. Run it
    first and it is a read-then-write with the usual outcome.
    """
    charged = await db.execute(
        _CHARGE_TURN,
        {
            "ns": namespace_key,
            "max_tasks": dispatch.default_max_tasks_per_hour,
            "max_turns": dispatch.default_max_turns_per_hour,
            "window": _WINDOW,
        },
    )
    if charged.first() is None:
        await _raise_refusal(db, namespace_key=namespace_key)

    in_flight = await db.scalar(
        _COUNT_AGENT_TURNS_IN_FLIGHT,
        {
            "ns": namespace_key,
            "agent": agent_name,
            "key": session_key,
            "stale": float(executor.turn_stale_after_seconds),
        },
    )
    if int(in_flight or 0) >= dispatch.max_concurrent_tasks_per_agent:
        TURNS_REJECTED.labels(reason=TURN_REJECT_CONCURRENCY).inc()
        raise APIError(
            status_code=409,
            error_code=ErrorCode.AGENT_CONCURRENCY_EXCEEDED,
            reason=ErrorReason.CONFLICT,
            detail=(
                f"Agent {agent_name} is already running {in_flight} dispatch turn(s), "
                f"which is this deployment's ceiling of "
                f"{dispatch.max_concurrent_tasks_per_agent}."
            ),
            hint=(
                "One task per agent at a time. The plugin has never been shown to "
                "be safe under concurrent invocations, so this is a ceiling rather "
                "than a queue."
            ),
            resource="AgentSession",
            resource_id=session_key,
        )


async def require_dispatch_not_paused(
    db: AsyncSession, *, namespace_key: str, action: str
) -> None:
    """Refuse dispatch work while either switch is thrown. Charges nothing.

    The import path and the claim path call this. Both are *optimisations* in
    the same sense the dispatcher's own pre-checks are: they stop rows being
    created that could never run, and they let a confirm say "the namespace is
    paused" instead of queueing four tasks nobody will see move. **The
    enforcement point is** :func:`charge_dispatch_turn` **on the turn path**,
    and this sentence is here so a later reader does not delete that check
    because this one already exists.
    """
    state = await _read_row(db, namespace_key=namespace_key)
    if state is None:
        return
    _raise_if_switched_off(state, action=action)


async def require_executors_not_halted(
    db: AsyncSession, *, namespace_key: str, action: str
) -> None:
    """Level 3, on the session-creation path and on every turn. Reaches human chat.

    Both entry points call it, and the turn path calls it for every session
    rather than only for a task's: a chat opened before the switch was thrown
    would otherwise keep starting turns, which is exactly the thing an operator
    reaching for the authoritative stop is trying to end. The charge below
    re-checks the same flag inside its own statement, so a dispatch turn meets
    it whichever of the two runs first.

    ``create_session`` consults only the halt and not the pause: pausing stops
    *new dispatch work*, and a human opening a chat while the fleet is paused is
    the operator going to look at what happened. Halting is the authoritative
    stop and refuses everything, which is what the copy beside that button has
    to say.
    """
    state = await _read_row(db, namespace_key=namespace_key)
    if state is None or state.halted_at is None:
        return
    _raise_halted(state, action=action)


async def charge_imported_tasks(
    db: AsyncSession,
    *,
    namespace_key: str,
    count: int,
    settings: DispatchSettings,
) -> None:
    """Refuse an import that would cross the hourly task ceiling. Inserts nothing.

    ``max_tasks_per_hour`` sits in a safety table beside ``max_turns_per_hour``,
    and an unenforced ceiling in a safety table is worse than no ceiling because
    operators read it and believe it. This is its named enforcement point: the
    import handler, counted over the same hour, **in the transaction that
    inserts the rows**, refusing with 429 after inserting nothing.

    That is the correct home for it because tasks are created only by import,
    unlike turns, which any holder of an ordinary key can start directly. A
    bypass of this ceiling therefore does not become a bypass of the money
    ceiling: turn spend stays bounded independently by
    :func:`charge_dispatch_turn`.

    The state row is locked before the count so two imports racing cannot both
    read nineteen and both commit twenty.
    """
    if count <= 0:
        return
    await _ensure_row(db, namespace_key=namespace_key, settings=settings)
    ceiling = await db.scalar(_LOCK_ROW, {"ns": namespace_key})
    already = int(
        await db.scalar(_COUNT_TASKS_IN_WINDOW, {"ns": namespace_key, "window": _WINDOW})
        or 0
    )
    ceiling = int(ceiling if ceiling is not None else settings.default_max_tasks_per_hour)
    if already + count <= ceiling:
        return
    raise APIError(
        status_code=429,
        error_code=ErrorCode.DISPATCH_BUDGET_EXCEEDED,
        reason=ErrorReason.CONFLICT,
        detail=(
            f"This namespace has imported {already} task(s) in the last hour and its "
            f"ceiling is {ceiling}. Committing {count} more would cross it, so nothing "
            "was created."
        ),
        hint=(
            "Confirm a smaller set, or wait for the window to roll. Nothing was "
            "inserted, so the same confirm works again later."
        ),
        resource="AgentTask",
        extra_details={"retry_after_seconds": DISPATCH_WINDOW_SECONDS},
    )


# -- reads and operator writes ---------------------------------------------


async def read_snapshot(
    db: AsyncSession, *, namespace_key: str, settings: DispatchSettings
) -> DispatchStateSnapshot:
    """The state and what is left of the hour. Advisory, for a banner or a preview.

    A namespace that has never dispatched anything has no row, and this reports
    the defaults rather than creating one. A read that writes is a read that
    cannot be done from a replica, and the preview calls this on every render of
    a confirm.
    """
    state = await _read_row(db, namespace_key=namespace_key)
    tasks_used = int(
        await db.scalar(_COUNT_TASKS_IN_WINDOW, {"ns": namespace_key, "window": _WINDOW})
        or 0
    )
    now = dt.datetime.now(dt.UTC)
    if state is None:
        return DispatchStateSnapshot(
            paused=False,
            executors_halted=False,
            budget=_budget(
                max_turns=settings.default_max_turns_per_hour,
                turns_used=0,
                max_tasks=settings.default_max_tasks_per_hour,
                tasks_used=tasks_used,
                window_started_at=now,
            ),
            updated_at=now,
        )

    window_start = state.turns_window_start
    expired = (now - window_start).total_seconds() >= _WINDOW
    return DispatchStateSnapshot(
        paused=state.paused_at is not None,
        paused_at=state.paused_at,
        paused_by_hash=state.paused_by,
        paused_reason=state.paused_reason,
        executors_halted=state.halted_at is not None,
        executors_halted_at=state.halted_at,
        executors_halted_by_hash=state.halted_by,
        executors_halted_reason=state.halted_reason,
        budget=_budget(
            max_turns=state.max_turns_per_hour,
            # A window nobody has charged a turn in since it expired still holds
            # the old count. Reporting it would tell an operator the hour is
            # spent when the next turn will roll it, so the *reported* figure
            # rolls too. The counter itself is moved by the charge statement and
            # by nothing else, because a read that writes is not a read.
            turns_used=0 if expired else state.turns_in_window,
            max_tasks=state.max_tasks_per_hour,
            tasks_used=tasks_used,
            window_started_at=now if expired else window_start,
        ),
        updated_at=state.updated_at,
    )


async def set_paused(
    db: AsyncSession,
    *,
    namespace_key: str,
    paused: bool,
    caller_hash: str | None,
    reason: str | None,
    settings: DispatchSettings,
) -> DispatchStateSnapshot:
    """Level 1. Idempotent: pressing pause twice is one paused namespace.

    Re-pausing an already paused namespace overwrites the reason and the
    credential tag, which is what an operator escalating an incident expects.
    Resuming clears all three, so the banner does not keep naming a stop that is
    no longer in force.
    """
    await _ensure_row(db, namespace_key=namespace_key, settings=settings)
    await db.execute(
        text(
            "UPDATE agent_dispatch_state "
            "   SET dispatch_paused_at = CASE WHEN :on THEN now() ELSE NULL END, "
            "       dispatch_paused_by = CASE WHEN :on THEN :hash ELSE NULL END, "
            "       dispatch_paused_reason = CASE WHEN :on THEN :reason ELSE NULL END, "
            "       updated_at = now() "
            " WHERE namespace_key = :ns"
        ),
        {"ns": namespace_key, "on": paused, "hash": caller_hash, "reason": reason},
    )
    return await read_snapshot(db, namespace_key=namespace_key, settings=settings)


async def set_executors_halted(
    db: AsyncSession,
    *,
    namespace_key: str,
    halted: bool,
    caller_hash: str | None,
    reason: str | None,
    settings: DispatchSettings,
) -> DispatchStateSnapshot:
    """Level 3, the authoritative stop. One flag refuses everything; one clears it.

    Deliberately not implemented by disabling every ``agent_runtimes`` binding.
    Bindings already disabled for unrelated reasons become indistinguishable
    afterwards, so re-enabling everything after the incident silently turns on
    things somebody deliberately turned off. An emergency stop that destroys the
    state you need to recover from it makes operators reluctant to press it,
    which is the worst property an emergency stop can have. The flag loses
    nothing.
    """
    await _ensure_row(db, namespace_key=namespace_key, settings=settings)
    await db.execute(
        text(
            "UPDATE agent_dispatch_state "
            "   SET executors_halted_at = CASE WHEN :on THEN now() ELSE NULL END, "
            "       executors_halted_by = CASE WHEN :on THEN :hash ELSE NULL END, "
            "       executors_halted_reason = CASE WHEN :on THEN :reason ELSE NULL END, "
            "       updated_at = now() "
            " WHERE namespace_key = :ns"
        ),
        {"ns": namespace_key, "on": halted, "hash": caller_hash, "reason": reason},
    )
    return await read_snapshot(db, namespace_key=namespace_key, settings=settings)


# -- internals --------------------------------------------------------------


def _budget(
    *,
    max_turns: int,
    turns_used: int,
    max_tasks: int,
    tasks_used: int,
    window_started_at: dt.datetime,
) -> DispatchBudget:
    return DispatchBudget(
        max_turns_per_hour=max_turns,
        turns_used_this_hour=turns_used,
        turns_remaining_this_hour=max(0, max_turns - turns_used),
        max_tasks_per_hour=max_tasks,
        tasks_created_this_hour=tasks_used,
        tasks_remaining_this_hour=max(0, max_tasks - tasks_used),
        window_started_at=window_started_at,
        window_resets_at=window_started_at + dt.timedelta(seconds=DISPATCH_WINDOW_SECONDS),
    )


async def _ensure_row(
    db: AsyncSession, *, namespace_key: str, settings: DispatchSettings
) -> None:
    await db.execute(
        _ENSURE_ROW,
        {
            "ns": namespace_key,
            "max_tasks": settings.default_max_tasks_per_hour,
            "max_turns": settings.default_max_turns_per_hour,
        },
    )


async def _read_row(db: AsyncSession, *, namespace_key: str) -> _StateRow | None:
    """The row as a typed value, or ``None`` when the namespace has never used one.

    Parsed into a dataclass rather than passed around as a mapping. Half of this
    module's job is deciding *which* refusal to raise from these six nullable
    columns, and a bag of ``object`` makes that decision unreadable to a type
    checker and to the next person.
    """
    result = await db.execute(_READ_STATE, {"ns": namespace_key})
    row = result.mappings().first()
    if row is None:
        return None
    return _StateRow(
        max_tasks_per_hour=int(row["max_tasks_per_hour"]),
        max_turns_per_hour=int(row["max_turns_per_hour"]),
        turns_window_start=_aware(row["turns_window_start"]),
        turns_in_window=int(row["turns_in_window"]),
        paused_at=_optional_moment(row["dispatch_paused_at"]),
        paused_by=_optional_text(row["dispatch_paused_by"]),
        paused_reason=_optional_text(row["dispatch_paused_reason"]),
        halted_at=_optional_moment(row["executors_halted_at"]),
        halted_by=_optional_text(row["executors_halted_by"]),
        halted_reason=_optional_text(row["executors_halted_reason"]),
        updated_at=_aware(row["updated_at"]),
    )


def _optional_moment(value: object) -> dt.datetime | None:
    return _aware(value) if isinstance(value, dt.datetime) else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


async def _raise_refusal(db: AsyncSession, *, namespace_key: str) -> None:
    """Say which of the three refusals it was, from the row that refused.

    Zero rows back from the charge is ambiguous, and the three answers have
    different remedies: a halt needs an admin, a pause needs an admin, and an
    exhausted hour needs a wait. Guessing would send a dispatcher into a retry
    loop against a flag no amount of waiting clears.
    """
    state = await _read_row(db, namespace_key=namespace_key)
    if state is None:
        # The insert half of the charge could not have conflicted, so a missing
        # row here means somebody deleted it between the two statements. Refuse
        # rather than invent an allowance.
        raise APIError(
            status_code=409,
            error_code=ErrorCode.DISPATCH_PAUSED,
            reason=ErrorReason.CONFLICT,
            detail="This namespace has no dispatch state row, so no turn may be charged.",
            hint="Pause and resume the namespace to recreate it.",
        )
    _raise_if_switched_off(state, action="A dispatch turn")

    window_start = state.turns_window_start
    resets_at = window_start + dt.timedelta(seconds=DISPATCH_WINDOW_SECONDS)
    retry_after = max(
        _MIN_RETRY_AFTER_SECONDS,
        (resets_at - dt.datetime.now(dt.UTC)).total_seconds(),
    )
    TURNS_REJECTED.labels(reason=TURN_REJECT_BUDGET).inc()
    raise APIError(
        status_code=429,
        error_code=ErrorCode.DISPATCH_BUDGET_EXCEEDED,
        reason=ErrorReason.CONFLICT,
        detail=(
            f"This namespace has spent its hourly allowance of "
            f"{state.max_turns_per_hour} dispatch turns. The window opened at "
            f"{window_start.isoformat()}."
        ),
        hint=(
            "The counter rolls at the top of the window. Raise the namespace's "
            "max_turns_per_hour if this ceiling is genuinely too low - and note "
            "that a turn is a proxy for spend, not a measure of it."
        ),
        extra_details={"retry_after_seconds": math.ceil(retry_after)},
    )


def _raise_if_switched_off(state: _StateRow, *, action: str) -> None:
    """Halt first, then pause. Ordered by authority rather than by severity."""
    if state.halted_at is not None:
        _raise_halted(state, action=action)
    if state.paused_at is not None:
        TURNS_REJECTED.labels(reason=TURN_REJECT_PAUSED).inc()
        raise APIError(
            status_code=409,
            error_code=ErrorCode.DISPATCH_PAUSED,
            reason=ErrorReason.CONFLICT,
            detail=(
                f"{action} is refused: dispatch in this namespace was paused at "
                f"{state.paused_at.isoformat()}"
                f"{_by(state.paused_by)}."
                f"{_because(state.paused_reason)}"
            ),
            hint=(
                "An admin resumes dispatch. Running turns are not stopped by a "
                "pause; stopping those is a halt."
            ),
        )


def _raise_halted(state: _StateRow, *, action: str) -> None:
    TURNS_REJECTED.labels(reason=TURN_REJECT_HALTED).inc()
    raise APIError(
        status_code=409,
        error_code=ErrorCode.EXECUTORS_HALTED,
        reason=ErrorReason.CONFLICT,
        detail=(
            f"{action} is refused: executors in this namespace were halted at "
            f"{state.halted_at.isoformat() if state.halted_at else 'an unknown time'}"
            f"{_by(state.halted_by)}."
            f"{_because(state.halted_reason)}"
        ),
        hint=(
            "An admin releases the halt. It refuses every new session and every "
            "new turn in this namespace, human chat included, and it does not "
            "stop a tool that is already executing."
        ),
    )


def _by(caller_hash: str | None) -> str:
    # A credential tag, not a person. Phrased so nobody reads it as a name.
    return f" by credential {caller_hash}" if caller_hash else ""


def _because(reason: str | None) -> str:
    return f" Reason given: {reason}" if reason else ""


def _aware(moment: object) -> dt.datetime:
    """Postgres hands these back tz-aware; SQLite would not, and arithmetic on a
    naive datetime against ``now(tz=UTC)`` raises rather than being wrong."""
    if not isinstance(moment, dt.datetime):
        return dt.datetime.now(dt.UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
