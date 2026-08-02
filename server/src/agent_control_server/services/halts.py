"""Stopping a turn: creation, delivery at a boundary, and expiry.

A halt is a latch bound to one turn, and almost everything below exists to keep
it that way.

**Creation is one conditional statement.** Read the session, decide it is
running, then insert is a race with the turn ending in between, and the row it
writes is a stop bound to a turn that no longer exists. The insert selects the
live trace out of ``agent_sessions`` in the same statement, so a turn that ends
first produces zero rows rather than a misbound halt.

**Delivery is claim-and-apply in one statement, joined against the session.**
The join requires ``target_trace_id = agent_sessions.in_flight_trace_id``, which
is what makes a halt unclaimable outside its own turn *by construction*. Without
it a halt whose executor died would sit in the table and be claimed by the first
model call of the next turn - silently killing a turn the human deliberately
started afterwards, under a transcript marker blaming an operator.

**Expiry is an event, and the event is the next acquire.** There is no sweeper
in this codebase and inventing one for this would be a background job with a
database query in it. A turn that ends stamps its own halt from the release
path; a replica that dies stamps nothing, so the next acquire expires anything
still pending for a different trace. That closes the replica-death case by
construction rather than by assertion.

**Lock order, obeyed here and everywhere: ``agent_sessions`` first.** Every
statement below either locks the session row before touching a halt row or
reaches the halt through a join that reads the session first. Reverse it in one
place and Postgres deadlocks in exactly the race this design exists for - a
halt claimed at the instant its turn ends - and the abort lands inside the one
code path that guarantees the turn lock gets cleared.
"""

from __future__ import annotations

import logging
from typing import cast

from agent_control_models.errors import ErrorCode, ErrorReason
from agent_control_models.halts import (
    Halt,
    HaltBoundary,
    HaltMode,
    HaltStatus,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import APIError, NotFoundError
from ..models import AgentSession, AgentSessionHalt
from .agent_sessions import AgentSessionsService, require_content_access
from .executor_metrics import (
    HALT_DELIVERY_LAG,
    HALT_REJECT_NOT_IN_FLIGHT,
    HALT_REJECT_QUOTA,
    HALTS_REJECTED,
    HALTS_TOTAL,
)
from .turn_quota import get_turn_quota

_logger = logging.getLogger(__name__)


def _halt_of(row: AgentSessionHalt, *, session_key: str) -> Halt:
    return Halt(
        id=row.id,
        session_key=session_key,
        target_trace_id=row.target_trace_id,
        mode=HaltMode(row.mode),
        status=HaltStatus(row.status),
        created_at=row.created_at,
        applied_at=row.applied_at,
        applied_at_boundary=(
            HaltBoundary(row.applied_at_boundary)
            if row.applied_at_boundary is not None
            else None
        ),
        applied_tool_name=row.applied_tool_name,
        turn_ended_at=row.turn_ended_at,
    )


class HaltsService:
    """Reads and writes ``agent_session_halts`` within one namespace."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        namespace_key: str,
        session_key: str,
        caller_hash: str | None,
        is_admin: bool,
        max_per_minute: int,
    ) -> tuple[Halt, bool]:
        """Bind a stop to whatever turn this session is running.

        Refusals in order: an unknown session is a 404, somebody else's session
        is a 403, a caller stopping turns faster than they could start them is
        a 429, and a session running nothing is a 409. Only then is a row
        written.

        Returns the halt and whether this call created it. A repeat press
        answers 200 with the existing row: one turn has one halt, and telling
        somebody their second click failed invites a third.
        """
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(session, caller_hash=caller_hash, is_admin=is_admin)
        _enforce_quota(
            namespace_key=namespace_key,
            caller_hash=caller_hash,
            max_per_minute=max_per_minute,
        )

        inserted = await self._db.execute(
            text(
                "INSERT INTO agent_session_halts "
                "       (namespace_key, session_id, target_trace_id, mode, "
                "        status, created_by_hash) "
                "SELECT s.namespace_key, s.id, s.in_flight_trace_id, "
                "       'graceful', 'pending', :hash "
                "  FROM agent_sessions s "
                " WHERE s.namespace_key = :ns "
                "   AND s.session_key = :key "
                "   AND s.in_flight_trace_id IS NOT NULL "
                "ON CONFLICT ON CONSTRAINT uq_agent_session_halts_turn "
                "DO NOTHING "
                "RETURNING id"
            ),
            {"ns": namespace_key, "key": session_key, "hash": caller_hash},
        )
        halt_id = inserted.scalar_one_or_none()
        if halt_id is not None:
            row = await self._require_row(namespace_key=namespace_key, halt_id=int(halt_id))
            _log_halt_created(
                namespace_key=namespace_key,
                agent_name=session.agent_name,
                session_key=session_key,
                caller_hash=caller_hash,
                created=True,
            )
            return _halt_of(row, session_key=session_key), True

        # Zero rows is ambiguous: either a halt already exists for this turn,
        # or nothing is running. Disambiguated by reading, in the same
        # transaction, rather than by guessing from the client's point of view.
        existing = await self._find_for_live_turn(
            namespace_key=namespace_key, session_key=session_key
        )
        if existing is not None:
            _log_halt_created(
                namespace_key=namespace_key,
                agent_name=session.agent_name,
                session_key=session_key,
                caller_hash=caller_hash,
                created=False,
            )
            return _halt_of(existing, session_key=session_key), False

        HALTS_REJECTED.labels(reason=HALT_REJECT_NOT_IN_FLIGHT).inc()
        raise APIError(
            status_code=409,
            error_code=ErrorCode.TURN_NOT_IN_FLIGHT,
            reason=ErrorReason.CONFLICT,
            detail=(
                "This session is not running a turn, so there is nothing to "
                "stop. A stop is bound to one turn and cannot be queued for "
                "the next one."
            ),
            hint=(
                "Send the message you want instead; a session with no turn in "
                "flight accepts one immediately."
            ),
            resource="AgentSession",
            resource_id=session_key,
        )

    async def list_halts(
        self,
        *,
        namespace_key: str,
        session_key: str,
        caller_hash: str | None,
        is_admin: bool,
        status: HaltStatus | str | None = None,
    ) -> list[Halt]:
        """Halts recorded against one session, newest first."""
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(session, caller_hash=caller_hash, is_admin=is_admin)

        stmt = (
            select(AgentSessionHalt)
            .where(
                AgentSessionHalt.namespace_key == namespace_key,
                AgentSessionHalt.session_id == session.id,
            )
            .order_by(AgentSessionHalt.id.desc())
        )
        if status is not None:
            stmt = stmt.where(AgentSessionHalt.status == HaltStatus(status).value)
        result = await self._db.execute(stmt)
        return [
            _halt_of(row, session_key=session_key) for row in result.scalars().all()
        ]

    async def apply_at_boundary(
        self,
        *,
        namespace_key: str,
        session_key: str,
        boundary: HaltBoundary,
        tool_name: str | None,
        lock_session: bool = True,
    ) -> Halt | None:
        """Claim and apply the stop for the turn now in flight, if there is one.

        One statement, so there is no window between claiming and applying.
        Splitting them would let a lost acknowledgement sweep the row to
        ``expired`` after the agent genuinely stopped, and the console would
        then tell an operator the stop never landed on an agent that is already
        stopped.

        The join against ``agent_sessions`` is the enforcement of "a halt is
        unclaimable outside its own turn". It is not a convenience.

        Runs inside the caller's transaction. ``lock_session`` is false only
        when that caller has already taken the session row - the nudge claim
        does, on the model boundary, and re-taking it would be a second round
        trip on the path of every model call. Every other caller leaves it
        true, because the lock order this codebase obeys everywhere is
        ``agent_sessions`` first and an ``UPDATE ... FROM`` takes no lock on
        the table it joins.
        """
        if lock_session:
            await self._db.execute(
                text(
                    "SELECT id FROM agent_sessions "
                    " WHERE namespace_key = :ns AND session_key = :key "
                    "   FOR UPDATE"
                ),
                {"ns": namespace_key, "key": session_key},
            )

        applied = await self._db.execute(
            text(
                "UPDATE agent_session_halts h "
                "   SET status = 'applied', "
                "       applied_at = now(), "
                "       applied_at_boundary = :boundary, "
                "       applied_tool_name = COALESCE(:tool, h.applied_tool_name) "
                "  FROM agent_sessions s "
                " WHERE h.namespace_key = :ns "
                "   AND s.namespace_key = :ns "
                "   AND s.session_key = :key "
                "   AND h.session_id = s.id "
                "   AND h.status = 'pending' "
                "   AND h.target_trace_id = s.in_flight_trace_id "
                "RETURNING h.id"
            ),
            {
                "ns": namespace_key,
                "key": session_key,
                "boundary": boundary.value,
                "tool": tool_name,
            },
        )
        halt_id = applied.scalar_one_or_none()
        if halt_id is None:
            return None

        row = await self._require_row(namespace_key=namespace_key, halt_id=int(halt_id))
        HALTS_TOTAL.labels(mode=row.mode, boundary=boundary.value).inc()
        if row.mode == HaltMode.GRACEFUL.value and row.applied_at is not None:
            HALT_DELIVERY_LAG.observe(
                max(0.0, (row.applied_at - row.created_at).total_seconds())
            )
        return _halt_of(row, session_key=session_key)

    async def enrich_applied(
        self,
        *,
        namespace_key: str,
        session_key: str,
        halt_id: int,
        applied_tool_name: str | None,
    ) -> Halt:
        """Record the tool an applied halt stopped, after the fact.

        Optional enrichment: the claim already moved the row, so losing this
        costs one word of transcript copy rather than the truth of the record.
        Only an applied halt for this session can be enriched, and only its
        tool name can be written - nothing here can change a status.
        """
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        row = await self._require_row(namespace_key=namespace_key, halt_id=halt_id)
        if row.session_id != session.id:
            raise NotFoundError(
                error_code=ErrorCode.HALT_NOT_FOUND,
                detail=f"Halt {halt_id} does not belong to this session.",
                resource="AgentSessionHalt",
                resource_id=str(halt_id),
                hint="Acknowledge the halt returned by this session's claim.",
            )
        if applied_tool_name is not None and row.status == HaltStatus.APPLIED.value:
            row.applied_tool_name = applied_tool_name
            await self._db.flush()
        return _halt_of(row, session_key=session_key)

    async def _find_for_live_turn(
        self, *, namespace_key: str, session_key: str
    ) -> AgentSessionHalt | None:
        stmt = (
            select(AgentSessionHalt)
            .join(
                AgentSession,
                (AgentSession.id == AgentSessionHalt.session_id)
                & (AgentSession.namespace_key == AgentSessionHalt.namespace_key),
            )
            .where(
                AgentSessionHalt.namespace_key == namespace_key,
                AgentSession.session_key == session_key,
                AgentSessionHalt.target_trace_id == AgentSession.in_flight_trace_id,
            )
        )
        result = await self._db.execute(stmt)
        return cast(AgentSessionHalt | None, result.scalars().first())

    async def _require_row(
        self, *, namespace_key: str, halt_id: int
    ) -> AgentSessionHalt:
        stmt = select(AgentSessionHalt).where(
            AgentSessionHalt.namespace_key == namespace_key,
            AgentSessionHalt.id == halt_id,
        )
        result = await self._db.execute(stmt)
        row = cast(AgentSessionHalt | None, result.scalars().first())
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.HALT_NOT_FOUND,
                detail=f"Halt {halt_id} not found.",
                resource="AgentSessionHalt",
                resource_id=str(halt_id),
                hint="Verify the halt id and that it belongs to this namespace.",
            )
        return row


def _enforce_quota(
    *, namespace_key: str, caller_hash: str | None, max_per_minute: int
) -> None:
    """Refuse a caller stopping turns faster than the turn ceiling allows.

    Deliberately the same bucket as ``POST /turns``. A stop is cheap to serve
    and expensive to ignore, and the pair of endpoints is one loop: start,
    stop, start. Two ceilings would let the cheaper half of the loop run at a
    rate the expensive half is refused at.
    """
    quota = get_turn_quota(max_per_minute=max_per_minute)
    retry_after = quota.try_acquire(
        namespace_key=namespace_key, caller_hash=caller_hash
    )
    if retry_after is None:
        return
    HALTS_REJECTED.labels(reason=HALT_REJECT_QUOTA).inc()
    raise APIError(
        status_code=429,
        error_code=ErrorCode.QUOTA_EXCEEDED,
        reason=ErrorReason.CONFLICT,
        detail=(
            f"This credential has started or stopped {max_per_minute} turns in "
            f"the last minute, which is its configured ceiling."
        ),
        hint=(
            f"Retry in about {retry_after:.0f} seconds, or raise "
            f"AGENT_CONTROL_EXECUTOR_MAX_TURNS_PER_MINUTE."
        ),
    )


def _log_halt_created(
    *,
    namespace_key: str,
    agent_name: str,
    session_key: str,
    caller_hash: str | None,
    created: bool,
) -> None:
    """Record who stopped what, at WARNING, carrying no content.

    The named exemption to this codebase's "content is never logged above
    DEBUG" rule, and it earns it: a halt carries no operator text at all, and
    an availability-affecting action whose only actor field hashes to the same
    value for every browser caller has no audit trail otherwise.
    """
    _logger.warning(
        "Operator halt %s. namespace=%s agent=%s session_key_prefix=%s "
        "mode=graceful caller_hash=%s",
        "created" if created else "already recorded",
        namespace_key,
        agent_name,
        session_key[:8],
        caller_hash or "-",
    )
