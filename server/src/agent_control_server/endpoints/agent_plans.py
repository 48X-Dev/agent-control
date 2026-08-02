"""HTTP endpoints for the plan an agent declared for itself.

Two audiences on one resource, and the split is the whole authorization design.

A human **reads** the plan with an ordinary API key under
``agent_sessions.content_read``, the same operation that guards the transcript,
because step titles and notes are model-authored text about the conversation.

The agent **writes** it with a runtime token bound to one session, under
``agent_plans.write``. A long-lived API key in the executor would let any agent
rewrite any session's plan in the namespace; the session-bound token cannot
address a session it was not minted for, so an agent's account of its own work
stays its own.

One thing these routes deliberately do not offer: a number. There is no
percentage field, no completion ratio and no derived summary anywhere in the
responses. Steps done over steps declared is a percentage with extra steps, and
the moment a console renders it, an agent's claim has been laundered into a
measurement nobody took.
"""

from __future__ import annotations

from agent_control_models.plans import (
    DeclarePlanRequest,
    PlanResponse,
    UpdatePlanStepRequest,
)
from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..db import get_async_db
from ..services.caller_identity import hash_caller_id
from ..services.plans import PlansService
from .agent_nudges import session_target_context

router = APIRouter(prefix="/agent-sessions", tags=["agent-plans"])


@router.get(
    "/{session_key}/plan",
    response_model=PlanResponse,
    summary="Read the plan the agent declared for this session",
    response_description="The current revision, or null when none was declared",
)
async def get_plan(
    session_key: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_SESSION_CONTENT_READ)
    ),
) -> PlanResponse:
    """Return what the agent says it is doing, attributed to the agent.

    ``plan`` is null when nothing was declared, which is an ordinary answer and
    not a 404. Most agents never declare a plan, and the honest fallback is the
    session's own facts - how many turns have run, how long it has been open,
    and the trace of the last one - rather than a progress bar over nothing.

    Read it as a claim. The steps, their order and their statuses are all the
    agent's account of its own work; this server records the account and does
    not check it. The independent evidence is the trace for the turn, which is
    why a console renders the two together.
    """
    service = PlansService(db)
    return await service.read(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
    )


@router.put(
    "/{session_key}/plan",
    response_model=PlanResponse,
    summary="Declare a plan for this session",
    response_description="The plan as now recorded",
)
async def declare_plan(
    session_key: str,
    request: DeclarePlanRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_PLANS_WRITE,
            context_builder=session_target_context,
        )
    ),
) -> PlanResponse:
    """Machine side. Record what the agent intends to do, as a new revision.

    Every call writes a new revision rather than editing the last one. Agents
    replan, and a plan that changed is a different plan: recording it as one is
    what lets a console say "revised" instead of quietly showing steps nobody
    read, and it is what keeps a step update that was already in flight from
    landing on the wrong plan.

    Refusals: an unknown session is 404, a token bound to another session is
    403, and a session whose plan has already been re-declared to the ceiling is
    429 with the recorded plan left standing.
    """
    service = PlansService(db)
    response = await service.declare(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        steps=list(request.steps),
    )
    await db.commit()
    return response


@router.patch(
    "/{session_key}/plan/revisions/{plan_revision}/steps/{step_index}",
    response_model=PlanResponse,
    summary="Mark one step of the declared plan",
    response_description="The plan after the update",
)
async def update_plan_step(
    session_key: str,
    request: UpdatePlanStepRequest,
    plan_revision: int = Path(
        ..., ge=1, description="Revision the step belongs to. A stale one is 409."
    ),
    step_index: int = Path(..., ge=0, description="0-based step within that revision."),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_PLANS_WRITE,
            context_builder=session_target_context,
        )
    ),
) -> PlanResponse:
    """Machine side. Move one step of one revision.

    The revision is part of the path because it is part of the identity of the
    step, not a hint about it. An update that named no revision would have to be
    resolved against "the latest plan", and an agent that re-declared its plan
    while an update was in flight would then have a step of the *new* plan
    marked done because a step of the *old* one finished.

    Two refusals, neither of which writes anything:

    * **409** - the plan has been re-declared since, so this revision is no
      longer current.
    * **422** - this revision has no such step. Step 7 of a five-step plan is
      refused whole rather than applied to the nearest one.
    """
    service = PlansService(db)
    response = await service.mark_step(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        plan_revision=plan_revision,
        step_index=step_index,
        status=request.status,
        note=request.note,
    )
    await db.commit()
    return response
