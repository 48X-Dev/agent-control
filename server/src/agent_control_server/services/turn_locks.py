"""The two statements that make a session run one turn at a time.

Split out of the turn flow because these two are a matched pair with an exact
contract between them, and because the next phase adds writes to *both* of them
in the same transaction. Keeping them adjacent is what makes the lock-ordering
rule readable as one rule rather than as two coincidences.

**Acquire is one statement.** ``UPDATE ... WHERE ... RETURNING id``. Zero rows
back means somebody else holds the lock. A read followed by a write would pass
every test its author wrote and fail under exactly the concurrency it was added
to prevent: two handlers both read "free", both write "mine", and two
invocations then append to one conversation.

**Release is fenced.** The acquire deliberately permits taking over a lock whose
holder appears to be gone, so a turn can be reclaimed while it is still running.
An unfenced release from that turn's late cleanup would then clear its
*successor's* lock, and a third turn would start alongside the second. The fence
is ``AND in_flight_trace_id = :trace``: a handler can only ever release the lock
it is actually holding.

**Two columns, two clearing rules.** ``in_flight_since`` is the lock and
``in_flight_trace_id`` is the liveness marker. A turn that genuinely ended clears
both. A handler that gave up while the invocation carries on clears the lock
only, so the caller is unblocked while the system still knows an agent is
spending. Do not collapse them, and do not add a third.

**Lock order, obeyed here and everywhere:** ``agent_sessions`` first, before any
table that references it. Both functions below take the session row first.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import AsyncSessionLocal
from .executor_metrics import TURN_STALE_RECLAIMS
from .halt_lifecycle import close_halts_for_turn, expire_halts_from_earlier_turns

_logger = logging.getLogger(__name__)

_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
"""Serialization failure and deadlock. Both mean "the database threw this
transaction away; run it again", and both are transient by definition."""

_RELEASE_ATTEMPTS = 3
_RELEASE_RETRY_DELAY_SECONDS = 0.05


def new_trace_id() -> str:
    """Mint the trace id for one turn.

    128 bits of hex, matching what ``sdks/python/src/agent_control/tracing.py``
    generates and what the observability store indexes, so a turn's id is
    addressable by ``GET /observability/traces/{trace_id}`` without translation.

    Whether the agent's *own* guardrail decisions get recorded under this id is a
    separate and unverified question: it depends on the executor reading the
    value back out of the per-turn state delta. Until that is confirmed this is
    the server's identifier for the turn and nothing more, and no caller should
    be told that a link resolves to the whole invocation.
    """
    return secrets.token_hex(16)


async def acquire_turn_lock(
    db: AsyncSession,
    *,
    namespace_key: str,
    session_key: str,
    trace_id: str,
    stale_after_seconds: float,
) -> int | None:
    """Take the session's turn lock. Returns its row id, or ``None`` if refused.

    Runs inside the caller's transaction, which must not be committed until the
    caller has everything else it needs. The row is locked with ``FOR UPDATE``
    first: that serializes concurrent acquires on one session, which leaves the
    timestamp heuristic below as the only unfenced part of the lock rather than
    as the whole of it.
    """
    stale = float(stale_after_seconds)
    # The staleness flag rides along on the lock read rather than costing a
    # second round trip. It is used only to log and count; the takeover
    # predicate itself lives inside the update, where nothing can race it.
    locked = await db.execute(
        text(
            "SELECT in_flight_since IS NOT NULL "
            "       AND in_flight_since "
            "           < now() - (:stale * interval '1 second') AS was_stale "
            "  FROM agent_sessions "
            " WHERE namespace_key = :ns AND session_key = :key "
            "   FOR UPDATE"
        ),
        {"ns": namespace_key, "key": session_key, "stale": stale},
    )
    was_stale = bool(locked.scalar_one_or_none())

    acquired = await db.execute(
        text(
            "UPDATE agent_sessions "
            "   SET in_flight_since = now(), "
            "       in_flight_trace_id = :trace, "
            "       last_activity_at = now() "
            " WHERE namespace_key = :ns "
            "   AND session_key = :key "
            "   AND (in_flight_since IS NULL "
            "        OR in_flight_since "
            "            < now() - (:stale * interval '1 second')) "
            "RETURNING id"
        ),
        {"ns": namespace_key, "key": session_key, "trace": trace_id, "stale": stale},
    )
    session_id = acquired.scalar_one_or_none()
    if session_id is None:
        return None

    # Any stop still bound to an earlier turn dies here, in the one statement
    # guaranteed to run before a new turn exists. There is no sweeper in this
    # process and this is deliberately not the place to invent one: a halt
    # whose replica died would otherwise sit pending forever, rendering in the
    # console as a stop that never landed. The session row is already locked
    # above, so the lock order holds.
    await expire_halts_from_earlier_turns(
        db,
        namespace_key=namespace_key,
        session_id=int(session_id),
        new_trace_id=trace_id,
    )

    if was_stale:
        TURN_STALE_RECLAIMS.inc()
        _logger.warning(
            "Reclaimed a turn lock older than the staleness window. "
            "namespace=%s session_key_prefix=%s",
            namespace_key,
            session_key[:8],
        )
    return int(session_id)


async def release_turn_lock(
    *,
    session_id: int,
    namespace_key: str,
    trace_id: str,
    turn_ended: bool,
) -> None:
    """Clear this handler's turn state, and only this handler's.

    ``turn_ended`` picks the exit. True clears the lock and the liveness marker
    and records the trace as the session's most recent ended turn. False clears
    the lock alone: the invocation is still running, and the truthful answer to
    "is this agent doing something" is yes.

    Opens its own database session, because the caller's is long closed by the
    time a turn finishes. Retries a deadlock, because this is the one code path
    that guarantees the lock gets cleared and there is nowhere useful for it to
    fail to. Never raises: it runs inside a ``finally`` and would otherwise
    replace whatever the handler was already saying with a database error.
    """
    statement = (
        "UPDATE agent_sessions "
        "   SET in_flight_since = NULL, "
        "       in_flight_trace_id = NULL, "
        "       last_trace_id = :trace, "
        "       last_activity_at = now() "
        if turn_ended
        else "UPDATE agent_sessions "
        "   SET in_flight_since = NULL, "
        "       last_activity_at = now() "
    ) + (
        " WHERE namespace_key = :ns "
        "   AND id = :id "
        "   AND in_flight_trace_id = :trace "
        "RETURNING id"
    )
    params = {"ns": namespace_key, "id": session_id, "trace": trace_id}

    for attempt in range(1, _RELEASE_ATTEMPTS + 1):
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(text(statement), params)
                released = result.scalar_one_or_none()
                if turn_ended:
                    # Stamp this turn's halt with the moment the turn really
                    # ended, which is the only state a console may render as
                    # stopped - "applied" is the executor's own word for it.
                    # A halt still pending when its turn ends becomes expired.
                    #
                    # Runs whether or not the fence held: the fence protects a
                    # *successor's* lock, and this statement is keyed on the
                    # trace this handler owns, so it can only ever touch its
                    # own turn's row. After the session update, so the lock
                    # order is unchanged.
                    await close_halts_for_turn(
                        db,
                        namespace_key=namespace_key,
                        session_id=session_id,
                        trace_id=trace_id,
                    )
                await db.commit()
            if released is None:
                # The fence held: somebody else owns the lock now, most likely
                # having reclaimed it as stale. Leaving their state alone is the
                # entire point, so this is a success rather than a failure.
                _logger.info(
                    "Turn cleanup found the lock already reassigned; leaving it "
                    "alone. namespace=%s session_id=%s",
                    namespace_key,
                    session_id,
                )
            return
        except DBAPIError as exc:
            if _is_retryable(exc) and attempt < _RELEASE_ATTEMPTS:
                await asyncio.sleep(_RELEASE_RETRY_DELAY_SECONDS * attempt)
                continue
            _log_release_failure(namespace_key, session_id, exc, attempt)
            return
        except Exception as exc:  # noqa: BLE001 - see the docstring.
            _log_release_failure(namespace_key, session_id, exc, attempt)
            return


def _log_release_failure(
    namespace_key: str, session_id: int, exc: BaseException, attempts: int
) -> None:
    """Record a lost cleanup loudly enough to be found.

    Losing this leaves the session locked until the staleness window expires, so
    it carries the identifiers needed to go and look, and the exception class
    only - never its text, which on a database error can carry statement
    fragments.
    """
    _logger.error(
        "Could not clear turn state after %s attempt(s). The session stays "
        "locked until the staleness window expires. namespace=%s session_id=%s "
        "error=%s",
        attempts,
        namespace_key,
        session_id,
        type(exc).__name__,
    )


def _is_retryable(exc: DBAPIError) -> bool:
    """Whether Postgres threw this transaction away and would take it again."""
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return sqlstate in _RETRYABLE_SQLSTATES
