"""Reading and writing the plan an agent declared for itself.

Everything this service stores is a claim by the agent, and everything it
returns is presented as one. That is the whole point of the phase: an executor
emits events, events are not progress, and a percentage synthesised from them is
a number that moves and means nothing. The only progress signal with an author
is the agent saying "here is my plan" and later "step 2 is done", so that is
what is recorded, attributed, and rendered next to the trace that can be checked
against it.

Three rules hold every write here together.

**A re-declared plan is a new revision, never an edit.** Agents replan. If
declaring wrote over the old rows, a console would show different steps than the
ones a person read a minute ago with nothing saying why, and any step update
still in flight would land on the wrong plan.

**A step update names its revision, and a stale one is refused.** Guessing
"they must mean the latest plan" is precisely how a step of the new plan gets
marked done because a step of the old one finished.

**A refused update writes nothing at all.** Every check runs before the first
mutation, inside the caller's transaction, so a 422 for a step that does not
exist cannot leave a neighbouring step marked.

Lock order, obeyed here as everywhere else in this codebase: ``agent_sessions``
first. Both writes take the session row before touching a plan row, which
serialises revision allocation per session and makes the staleness check exact
rather than nearly exact.
"""

from __future__ import annotations

import logging
from typing import cast

from agent_control_models.errors import ErrorCode, ErrorReason
from agent_control_models.plans import (
    PLAN_MAX_REVISIONS,
    Plan,
    PlanResponse,
    PlanStep,
    PlanStepStatus,
)
from sqlalchemy import distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import APIError, NotFoundError
from ..models import AgentSession, AgentSessionPlanStep
from .agent_sessions import AgentSessionsService, require_content_access

_logger = logging.getLogger(__name__)


def _step_of(row: AgentSessionPlanStep) -> PlanStep:
    return PlanStep(
        index=row.step_index,
        title=row.title,
        status=PlanStepStatus(row.status),
        note=row.note,
        updated_at=row.updated_at,
    )


class PlansService:
    """Reads and writes ``agent_session_plan_steps`` within one namespace."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def read(
        self,
        *,
        namespace_key: str,
        session_key: str,
        caller_hash: str | None,
        is_admin: bool,
    ) -> PlanResponse:
        """The current plan for one session, or the fact that there is none.

        No plan is an ordinary answer rather than a 404. An agent that never
        declared one is the common case, and a console showing this has to fall
        back to what is actually known about the session - turns run, how long
        it has been open, the last turn's trace - instead of to a progress bar
        with nothing behind it.

        Read under ``content_read`` because step titles and notes are text a
        model wrote about a conversation, which is the same sensitivity class as
        the transcript itself.
        """
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(session, caller_hash=caller_hash, is_admin=is_admin)
        plan = await self._current_plan(session=session, session_key=session_key)
        return PlanResponse(session_key=session_key, plan=plan)

    async def declare(
        self,
        *,
        namespace_key: str,
        session_key: str,
        steps: list[str],
    ) -> PlanResponse:
        """Record a new plan revision for this session.

        Machine side: the caller is the agent itself, holding a runtime token
        bound to this session, so there is no creator scoping to apply - the
        token *is* the session identity and cannot address another one.

        The session row is locked first, which serialises two declarations
        racing for the same revision number. Without it both would compute the
        same next revision and one would fail on the primary key, turning an
        agent replanning twice quickly into a 500.
        """
        session = await self._locked_session(
            namespace_key=namespace_key, session_key=session_key
        )
        current = await self._max_revision(session=session)
        revision = (current or 0) + 1
        if revision > PLAN_MAX_REVISIONS:
            raise APIError(
                status_code=429,
                error_code=ErrorCode.QUOTA_EXCEEDED,
                reason=ErrorReason.CONFLICT,
                detail=(
                    f"This session has already had {PLAN_MAX_REVISIONS} plans "
                    f"declared for it, which is the ceiling. The plan already "
                    f"recorded stands."
                ),
                hint=(
                    "An agent re-declaring its plan this often is usually "
                    "looping rather than replanning; the transcript and the "
                    "trace for the turn show which."
                ),
                resource="AgentSession",
                resource_id=session_key,
            )

        for index, title in enumerate(steps):
            self._db.add(
                AgentSessionPlanStep(
                    namespace_key=namespace_key,
                    session_id=session.id,
                    plan_revision=revision,
                    step_index=index,
                    title=title,
                    status=PlanStepStatus.PENDING.value,
                )
            )
        await self._db.flush()

        _logger.debug(
            "Agent declared a plan. namespace=%s agent=%s revision=%s steps=%s",
            namespace_key,
            session.agent_name,
            revision,
            len(steps),
        )
        plan = await self._current_plan(session=session, session_key=session_key)
        return PlanResponse(session_key=session_key, plan=plan)

    async def mark_step(
        self,
        *,
        namespace_key: str,
        session_key: str,
        plan_revision: int,
        step_index: int,
        status: PlanStepStatus | str,
        note: str | None,
    ) -> PlanResponse:
        """Mark one step of one revision, or refuse without writing anything.

        Two refusals, and they are different failures rather than two flavours
        of "bad request".

        A revision that is not the current one is a **409**: the request was
        well formed and would have been right a moment earlier, and the agent
        needs to know its plan moved rather than have the update quietly
        applied to a plan it has not read.

        A step index the plan does not have is a **422**: step 7 of a five-step
        plan is not a step, and marking the nearest one instead would put a tick
        against work nobody described. Both run before any mutation, so neither
        leaves a partial write behind.
        """
        session = await self._locked_session(
            namespace_key=namespace_key, session_key=session_key
        )
        current = await self._max_revision(session=session)
        if current is None:
            raise NotFoundError(
                error_code=ErrorCode.PLAN_NOT_FOUND,
                detail=(
                    "No plan has been declared for this session, so there is no "
                    "step to mark."
                ),
                resource="AgentSessionPlan",
                resource_id=session_key,
                hint="Declare a plan before marking any of its steps.",
            )
        if plan_revision != current:
            raise APIError(
                status_code=409,
                error_code=ErrorCode.PLAN_REVISION_STALE,
                reason=ErrorReason.CONFLICT,
                detail=(
                    f"This session's plan is at revision {current}, and the "
                    f"update named revision {plan_revision}. Nothing was "
                    f"written."
                ),
                hint=(
                    f"Re-read the plan and mark steps of revision {current}, or "
                    f"declare a new plan if the work has changed."
                ),
                resource="AgentSessionPlan",
                resource_id=session_key,
            )

        rows = await self._revision_rows(session=session, revision=current)
        by_index = {row.step_index: row for row in rows}
        row = by_index.get(step_index)
        if row is None:
            raise APIError(
                status_code=422,
                error_code=ErrorCode.PLAN_STEP_OUT_OF_RANGE,
                reason=ErrorReason.UNPROCESSABLE_ENTITY,
                detail=(
                    f"Revision {current} of this plan has {len(rows)} steps, "
                    f"indexed 0 to {max(len(rows) - 1, 0)}, so step "
                    f"{step_index} does not exist. Nothing was written."
                ),
                hint=(
                    "Mark a step the declared plan actually has, or declare a "
                    "new plan that has it."
                ),
                resource="AgentSessionPlan",
                resource_id=session_key,
            )

        # The shared base model serializes enums to their values, so what
        # arrives here is a plain string. Normalizing through the enum keeps
        # both callers honest and keeps an unknown status out of the column.
        row.status = PlanStepStatus(status).value
        if note is not None:
            row.note = note
        row.updated_at = func.now()
        await self._db.flush()
        await self._db.refresh(row)

        plan = await self._current_plan(session=session, session_key=session_key)
        return PlanResponse(session_key=session_key, plan=plan)

    # -- internals -------------------------------------------------------

    async def _locked_session(
        self, *, namespace_key: str, session_key: str
    ) -> AgentSession:
        """Load the session and hold its row for the rest of the transaction.

        ``agent_sessions`` first, always. Taking the plan rows first and the
        session row second would invert the order every other writer in this
        codebase uses, and the deadlock would land inside a transaction an agent
        is waiting on mid-turn.
        """
        sessions = AgentSessionsService(self._db)
        session = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        await self._db.execute(
            text(
                "SELECT id FROM agent_sessions "
                " WHERE namespace_key = :ns AND session_key = :key "
                "   FOR UPDATE"
            ),
            {"ns": namespace_key, "key": session_key},
        )
        return session

    async def _max_revision(self, *, session: AgentSession) -> int | None:
        result = await self._db.execute(
            select(func.max(AgentSessionPlanStep.plan_revision)).where(
                AgentSessionPlanStep.namespace_key == session.namespace_key,
                AgentSessionPlanStep.session_id == session.id,
            )
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def _revision_count(self, *, session: AgentSession) -> int:
        result = await self._db.execute(
            select(func.count(distinct(AgentSessionPlanStep.plan_revision))).where(
                AgentSessionPlanStep.namespace_key == session.namespace_key,
                AgentSessionPlanStep.session_id == session.id,
            )
        )
        return int(result.scalar_one() or 0)

    async def _revision_rows(
        self, *, session: AgentSession, revision: int
    ) -> list[AgentSessionPlanStep]:
        result = await self._db.execute(
            select(AgentSessionPlanStep)
            .where(
                AgentSessionPlanStep.namespace_key == session.namespace_key,
                AgentSessionPlanStep.session_id == session.id,
                AgentSessionPlanStep.plan_revision == revision,
            )
            .order_by(AgentSessionPlanStep.step_index)
        )
        return list(cast(list[AgentSessionPlanStep], result.scalars().all()))

    async def _current_plan(
        self, *, session: AgentSession, session_key: str
    ) -> Plan | None:
        """The highest revision, with the count of how many there have been.

        The count is carried rather than the history. A console needs to say
        "revised twice" so a person knows the steps in front of them replaced
        something; it does not need the superseded steps, which are kept in the
        table for anyone reading the record rather than watching the panel.
        """
        revision = await self._max_revision(session=session)
        if revision is None:
            return None
        rows = await self._revision_rows(session=session, revision=revision)
        if not rows:
            return None
        # ``declared_at`` is its own column rather than the earliest
        # ``updated_at``: the two agree only until the last step is marked, and
        # after that the earliest update is later than the declaration.
        return Plan(
            session_key=session_key,
            revision=revision,
            revision_count=await self._revision_count(session=session),
            steps=[_step_of(row) for row in rows],
            declared_at=min(row.declared_at for row in rows),
            last_updated_at=max(row.updated_at for row in rows),
        )
