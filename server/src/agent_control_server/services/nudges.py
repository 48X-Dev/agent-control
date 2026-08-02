"""The nudge queue: writing guidance, claiming it, and recording what happened.

The queue is per session and is drained at model boundaries by the executor
that is running the agent. Four rules shape the code below, and each of them is
there because the obvious alternative fails in a way a person would notice.

**Claiming skips locked rows.** ``SELECT ... FOR UPDATE SKIP LOCKED`` is what
lets two processes serving one agent drain one queue without either blocking or
double-delivering. Without ``SKIP LOCKED`` a second claim waits on the first,
which turns a model boundary into a lock queue.

**A claim is a lease, not a handoff.** ``claim_expires_at`` is what redelivers a
nudge whose executor died between claiming and injecting. Delivery is
at-least-once on purpose: a duplicate nudge means a model sees one sentence
twice, which is harmless, and a dropped nudge means a human believes an agent
was told something it never heard, which is the failure that destroys trust in
the feature.

**Only a failed injection can expire a nudge.** ``claim_count`` moves on every
claim, ``injection_attempts`` only when an injection was attempted and failed,
and expiry keys on the second. The surplus over the per-call cap goes back to
``pending`` untouched, so ten queued nudges do not report seven as undelivered
after three claim cycles.

**A halt beats the queue, and the queue is not consumed.** When a stop is bound
to the turn in flight, the claim returns the halt and zero nudges, and no
counter moves. A nudge injected into a request whose response is about to be
replaced by a block would be marked applied while no model ever read it.

Lock order, as everywhere else: ``agent_sessions`` first, then its children.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import cast

from agent_control_models.errors import ErrorCode, ErrorReason
from agent_control_models.halts import HaltBoundary, HaltMode
from agent_control_models.nudges import (
    MAX_PENDING_NUDGES_PER_SESSION,
    NUDGE_CLAIM_TTL_SECONDS,
    NUDGE_MAX_INJECTION_ATTEMPTS,
    NUDGE_MAX_PER_MODEL_CALL,
    ClaimedHalt,
    ClaimedNudge,
    ClaimNudgesResponse,
    Nudge,
    NudgeAck,
    NudgeAckOutcome,
    NudgeStatus,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import APIError, NotFoundError
from ..models import AgentSession, AgentSessionNudge
from .agent_sessions import AgentSessionsService, require_content_access
from .executor_metrics import (
    NUDGE_CLAIM_CLAIMED,
    NUDGE_CLAIM_EMPTY,
    NUDGE_CLAIMS,
    NUDGE_DELIVERY_LAG,
)
from .halts import HaltsService

_logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = (NudgeStatus.PENDING.value, NudgeStatus.CLAIMED.value)


def _nudge_of(row: AgentSessionNudge, *, session_key: str) -> Nudge:
    return Nudge(
        id=row.id,
        session_key=session_key,
        body=row.body,
        status=NudgeStatus(row.status),
        created_at=row.created_at,
        claimed_at=row.claimed_at,
        claim_expires_at=row.claim_expires_at,
        applied_at=row.applied_at,
        applied_trace_id=row.applied_trace_id,
        claim_count=row.claim_count,
        injection_attempts=row.injection_attempts,
        rejected_by_control=row.rejected_by_control,
    )


class NudgesService:
    """Reads and writes ``agent_session_nudges`` within one namespace."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # -- human side ------------------------------------------------------

    async def create(
        self,
        *,
        namespace_key: str,
        session_key: str,
        caller_hash: str | None,
        is_admin: bool,
        body: str,
    ) -> Nudge:
        """Queue one piece of guidance for the agent's next model call.

        Deliberately does *not* require a turn to be in flight. A nudge queued
        between turns applies to the next one, which is how "tell it this
        before it starts" works without new machinery, and the UI says
        "queued, will apply on the agent's next step" rather than showing a
        spinner.

        The queue is capped per session. Past the cap the answer is a 429: at
        three injections per model call, a queue longer than this is not
        guidance, it is a backlog, and every entry in it is billed on the model
        call that finally carries it.
        """
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(session, caller_hash=caller_hash, is_admin=is_admin)

        pending = await self._db.execute(
            select(func.count())
            .select_from(AgentSessionNudge)
            .where(
                AgentSessionNudge.namespace_key == namespace_key,
                AgentSessionNudge.session_id == session.id,
                AgentSessionNudge.status.in_(_ACTIVE_STATUSES),
            )
        )
        queued = int(pending.scalar_one())
        if queued >= MAX_PENDING_NUDGES_PER_SESSION:
            raise APIError(
                status_code=429,
                error_code=ErrorCode.QUOTA_EXCEEDED,
                reason=ErrorReason.CONFLICT,
                detail=(
                    f"This session already has {queued} nudges waiting, which "
                    f"is its ceiling. At most "
                    f"{NUDGE_MAX_PER_MODEL_CALL} are injected per model call, "
                    f"so a longer queue delivers later rather than sooner."
                ),
                hint="Cancel a queued nudge, or wait for the queue to drain.",
            )

        row = AgentSessionNudge(
            namespace_key=namespace_key,
            session_id=session.id,
            body=body,
            status=NudgeStatus.PENDING.value,
            created_by_hash=caller_hash,
        )
        self._db.add(row)
        await self._db.flush()
        await self._db.refresh(row)
        return _nudge_of(row, session_key=session_key)

    async def list_nudges(
        self,
        *,
        namespace_key: str,
        session_key: str,
        caller_hash: str | None,
        is_admin: bool,
        status: NudgeStatus | str | None = None,
    ) -> list[Nudge]:
        """Every nudge queued for one session, newest first."""
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(session, caller_hash=caller_hash, is_admin=is_admin)

        stmt = (
            select(AgentSessionNudge)
            .where(
                AgentSessionNudge.namespace_key == namespace_key,
                AgentSessionNudge.session_id == session.id,
            )
            .order_by(AgentSessionNudge.id.desc())
        )
        if status is not None:
            stmt = stmt.where(AgentSessionNudge.status == NudgeStatus(status).value)
        result = await self._db.execute(stmt)
        return [
            _nudge_of(row, session_key=session_key) for row in result.scalars().all()
        ]

    async def cancel(
        self,
        *,
        namespace_key: str,
        session_key: str,
        caller_hash: str | None,
        is_admin: bool,
        nudge_id: int,
    ) -> tuple[Nudge, bool]:
        """Withdraw a nudge nobody has claimed yet.

        A claimed nudge cannot be withdrawn, and that is a 409 rather than a
        best-effort attempt: the text may already be inside a model request,
        and reporting a withdrawal that did not happen is worse than refusing
        one that could not.
        """
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(session, caller_hash=caller_hash, is_admin=is_admin)

        row = await self._require_row(
            namespace_key=namespace_key, session_id=session.id, nudge_id=nudge_id
        )
        if row.status != NudgeStatus.PENDING.value:
            if row.status == NudgeStatus.CLAIMED.value:
                raise APIError(
                    status_code=409,
                    error_code=ErrorCode.TURN_IN_FLIGHT,
                    reason=ErrorReason.CONFLICT,
                    detail=(
                        "An executor has already taken this nudge for a model "
                        "call, so it can no longer be withdrawn."
                    ),
                    hint=(
                        "Its outcome will appear here once the model call "
                        "finishes."
                    ),
                    resource="AgentSessionNudge",
                    resource_id=str(nudge_id),
                )
            return _nudge_of(row, session_key=session_key), False

        row.status = NudgeStatus.CANCELLED.value
        await self._db.flush()
        return _nudge_of(row, session_key=session_key), True

    # -- machine side ----------------------------------------------------

    async def claim(
        self,
        *,
        namespace_key: str,
        session_key: str,
        claimed_by: str | None,
        max_nudges: int,
    ) -> ClaimNudgesResponse:
        """Answer one model boundary: stop here, or say these things.

        The whole decision is one transaction, and it takes the session row
        first. Precedence is enforced here rather than in the SDK, because a
        halt created between a claim and its injection would otherwise mark
        guidance as delivered that no model ever read.

        Runs inside the caller's transaction; the caller commits.
        """
        session = await self._lock_session(
            namespace_key=namespace_key, session_key=session_key
        )

        halt = await HaltsService(self._db).apply_at_boundary(
            namespace_key=namespace_key,
            session_key=session_key,
            boundary=HaltBoundary.MODEL,
            tool_name=None,
            # ``_lock_session`` above already holds the session row, and the
            # lock order is what this argument exists to keep honest.
            lock_session=False,
        )
        if halt is not None:
            NUDGE_CLAIMS.labels(result=NUDGE_CLAIM_EMPTY).inc()
            return ClaimNudgesResponse(
                session_key=session_key,
                nudges=[],
                halt=ClaimedHalt(
                    id=halt.id,
                    target_trace_id=halt.target_trace_id,
                    # Shared models serialize enums to their values, so this
                    # normalizes from either form rather than assuming one.
                    mode=HaltMode(halt.mode).value,
                ),
                claim_expires_at=None,
            )

        wanted = max(1, min(max_nudges, NUDGE_MAX_PER_MODEL_CALL))
        claimed = await self._db.execute(
            text(
                "WITH candidates AS ( "
                "    SELECT id "
                "      FROM agent_session_nudges "
                "     WHERE namespace_key = :ns "
                "       AND session_id = :sid "
                "       AND (status = 'pending' "
                "            OR (status = 'claimed' "
                "                AND claim_expires_at IS NOT NULL "
                "                AND claim_expires_at < now())) "
                "     ORDER BY created_at, id "
                "     LIMIT :limit "
                "       FOR UPDATE SKIP LOCKED "
                ") "
                "UPDATE agent_session_nudges n "
                "   SET status = 'claimed', "
                "       claimed_at = now(), "
                "       claimed_by = :by, "
                "       claim_expires_at = now() "
                "                          + (:ttl * interval '1 second'), "
                "       claim_count = n.claim_count + 1 "
                "  FROM candidates c "
                " WHERE n.id = c.id "
                "RETURNING n.id, n.body, n.created_at, n.claim_expires_at"
            ),
            {
                "ns": namespace_key,
                "sid": session.id,
                "limit": wanted,
                "by": claimed_by,
                "ttl": float(NUDGE_CLAIM_TTL_SECONDS),
            },
        )
        rows = claimed.fetchall()
        NUDGE_CLAIMS.labels(
            result=NUDGE_CLAIM_CLAIMED if rows else NUDGE_CLAIM_EMPTY
        ).inc()
        # Ordered again on the way out: the UPDATE ... FROM makes no promise
        # about row order, and "oldest first" is a contract the SDK relies on
        # when it decides which three of a queue to inject.
        ordered = sorted(rows, key=lambda row: (row[2], row[0]))
        return ClaimNudgesResponse(
            session_key=session_key,
            nudges=[
                ClaimedNudge(id=row[0], body=row[1], created_at=row[2])
                for row in ordered
            ],
            halt=None,
            claim_expires_at=ordered[0][3] if ordered else None,
        )

    async def acknowledge(
        self,
        *,
        namespace_key: str,
        session_key: str,
        acks: list[NudgeAck],
    ) -> list[Nudge]:
        """Record what became of claimed nudges.

        Only a claimed nudge can be acknowledged, and only into a terminal
        state or back to ``pending``. An acknowledgement for a nudge in any
        other state is ignored rather than rejected: the executor may be
        replaying an acknowledgement whose response was lost, and failing that
        retry would leave the row leased until its TTL for no benefit.
        """
        session = await self._lock_session(
            namespace_key=namespace_key, session_key=session_key
        )

        updated: list[Nudge] = []
        for ack in acks:
            row = await self._find_row(
                namespace_key=namespace_key, session_id=session.id, nudge_id=ack.id
            )
            if row is None or row.status != NudgeStatus.CLAIMED.value:
                continue
            self._apply_ack(row, ack)
            updated.append(_nudge_of(row, session_key=session_key))
        await self._db.flush()
        return updated

    def _apply_ack(self, row: AgentSessionNudge, ack: NudgeAck) -> None:
        """Move one claimed row into the state its acknowledgement describes."""
        now = dt.datetime.now(tz=dt.UTC)
        outcome = NudgeAckOutcome(ack.outcome)

        if outcome is NudgeAckOutcome.APPLIED:
            row.status = NudgeStatus.APPLIED.value
            row.applied_at = now
            row.applied_trace_id = ack.trace_id
            row.claim_expires_at = None
            NUDGE_DELIVERY_LAG.observe(
                max(0.0, (now - row.created_at).total_seconds())
            )
            return

        if outcome is NudgeAckOutcome.RELEASED:
            # The surplus over the per-call cap, or a claim a halt superseded.
            # Neither counter moves: nothing was attempted, so nothing should
            # count against this nudge's life.
            row.status = NudgeStatus.PENDING.value
            row.claimed_at = None
            row.claim_expires_at = None
            return

        if outcome is NudgeAckOutcome.REJECTED:
            row.status = NudgeStatus.REJECTED.value
            row.rejected_by_control = ack.rejected_by_control
            row.claim_expires_at = None
            return

        # FAILED: an injection was really attempted and did not land. This is
        # the only counter that can end a nudge's life.
        row.injection_attempts = int(row.injection_attempts) + 1
        row.claimed_at = None
        row.claim_expires_at = None
        row.status = (
            NudgeStatus.EXPIRED.value
            if row.injection_attempts >= NUDGE_MAX_INJECTION_ATTEMPTS
            else NudgeStatus.PENDING.value
        )

    # -- internals -------------------------------------------------------

    async def _lock_session(
        self, *, namespace_key: str, session_key: str
    ) -> AgentSession:
        """Load the session and take its row lock, in that order, always.

        Every transaction touching both tables locks this row first. Reverse it
        anywhere and the deadlock lands in the race this design exists for: a
        halt claimed at the instant its turn ends.
        """
        row = await AgentSessionsService(self._db).get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        await self._db.execute(
            text(
                "SELECT id FROM agent_sessions "
                " WHERE namespace_key = :ns AND id = :id "
                "   FOR UPDATE"
            ),
            {"ns": namespace_key, "id": row.id},
        )
        return row

    async def _find_row(
        self, *, namespace_key: str, session_id: int, nudge_id: int
    ) -> AgentSessionNudge | None:
        result = await self._db.execute(
            select(AgentSessionNudge).where(
                AgentSessionNudge.namespace_key == namespace_key,
                AgentSessionNudge.session_id == session_id,
                AgentSessionNudge.id == nudge_id,
            )
        )
        return cast(AgentSessionNudge | None, result.scalars().first())

    async def _require_row(
        self, *, namespace_key: str, session_id: int, nudge_id: int
    ) -> AgentSessionNudge:
        row = await self._find_row(
            namespace_key=namespace_key, session_id=session_id, nudge_id=nudge_id
        )
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.NUDGE_NOT_FOUND,
                detail=f"Nudge {nudge_id} not found on this session.",
                resource="AgentSessionNudge",
                resource_id=str(nudge_id),
                hint="Verify the nudge id and that it belongs to this session.",
            )
        return row
