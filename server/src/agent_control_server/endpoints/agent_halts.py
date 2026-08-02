"""HTTP endpoints for stopping a running agent.

A halt is a human pressing stop, and every route here is shaped by one fact:
the stop lands at the agent's **next boundary**, before its next model call or
before its next tool runs. A tool that has already started finishes and its
side effect happens. Nothing in this stack changes that, and no message written
by these endpoints implies otherwise.

Creating a halt sits at ``agent_halts.write``, the same tier as starting a
turn. Run at AUTHENTICATED and stop at ADMIN is the one pairing that cannot be
defended: whoever can start a turn that spends money must be able to stop it.
What keeps that tier from being a way to end everyone else's work is creator
scoping, applied by the same predicate transcript reads use.

Claiming is machine side, under the token-bound ``agent_nudges.consume``.
There is deliberately no separate halt-consume operation: halts ride the nudge
claim at the model boundary, so restricting a second operation would guard
nothing while revoking it would silently disable half of halt delivery - which
reads to an operator as "stop sometimes doesn't work".
"""

from __future__ import annotations

from agent_control_models.halts import (
    AckHaltRequest,
    AckHaltResponse,
    ClaimHaltRequest,
    ClaimHaltResponse,
    CreateHaltRequest,
    CreateHaltResponse,
    HaltBoundary,
    HaltStatus,
    ListHaltsResponse,
)
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..config import executor_settings
from ..db import get_async_db
from ..services.caller_identity import hash_caller_id
from ..services.halts import HaltsService
from .agent_nudges import session_target_context

router = APIRouter(prefix="/agent-sessions", tags=["agent-halts"])


@router.post(
    "/{session_key}/halts",
    response_model=CreateHaltResponse,
    summary="Stop the turn this session is running",
    response_description="The halt bound to the live turn",
)
async def create_halt(
    session_key: str,
    request: CreateHaltRequest | None = None,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_HALTS_WRITE)),
) -> CreateHaltResponse:
    """Bind a stop to the turn now in flight.

    Say plainly what this does and does not do. It stops the agent at its next
    boundary: before the next model call, or before the next tool runs,
    whichever comes first. It does not interrupt a tool that is already
    executing - that tool finishes and whatever it was doing has already
    happened.

    The halt binds to the session's **liveness marker**, not to its turn lock.
    Those two stop being the same thing the moment a turn outlives this
    server's patience, and binding to the lock would make the button
    unpressable at exactly the moment somebody reaches for it, with the panel
    showing nothing in flight.

    What a halt created in that window is worth is a smaller thing than it
    looks, and it is worth saying here rather than discovering it. The executor
    ends an invocation when the request it arrived on is dropped, so a turn
    this server has already timed out on has most likely ended with it. Such a
    halt is accepted and recorded, it reaches no boundary, and the next turn's
    acquire ages it out. Nothing on this route promises otherwise.

    Failures:

    * **409** - this session is not running anything, so there is no turn to
      bind a stop to. A halt is never queued for a future turn.
    * **403** - the session was opened by a different caller.
    * **429** - this credential has started or stopped too many turns this
      minute; stops share the turn ceiling because start-stop-start is one
      loop.

    Pressing stop twice is one halt and answers 200 both times, with
    ``created`` false the second time. One turn has one stop.
    """
    del request  # A halt carries no operator text. See ``CreateHaltRequest``.
    service = HaltsService(db)
    halt, created = await service.create(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        max_per_minute=executor_settings.max_turns_per_minute,
    )
    await db.commit()
    return CreateHaltResponse(halt=halt, created=created)


@router.get(
    "/{session_key}/halts",
    response_model=ListHaltsResponse,
    summary="List the stops recorded against a session",
    response_description="Halts, newest first",
)
async def list_halts(
    session_key: str,
    status: HaltStatus | None = Query(None, description="Optional status filter."),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_SESSION_CONTENT_READ)
    ),
) -> ListHaltsResponse:
    """Return the stops on this session and where each one landed.

    Read under ``content_read`` rather than the metadata operation for one
    field: ``applied_tool_name`` names a tool the agent was about to run, and
    handing an agent's tool inventory to a caller who was refused the
    conversation is the small disclosure that split exists to prevent.

    Note what "applied" means and does not mean. It is the executor saying it
    blocked, which is an assertion by the party being stopped. The state a
    console may render as stopped is ``turn_ended_at``, which this server
    observes for itself when the turn's liveness marker clears.
    """
    service = HaltsService(db)
    halts = await service.list_halts(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        status=status,
    )
    return ListHaltsResponse(session_key=session_key, halts=halts)


@router.post(
    "/{session_key}/halts/claim",
    response_model=ClaimHaltResponse,
    summary="Ask whether this boundary should stop",
    response_description="The halt that was applied, if any",
)
async def claim_halt(
    session_key: str,
    request: ClaimHaltRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_NUDGES_CONSUME,
            context_builder=session_target_context,
        )
    ),
) -> ClaimHaltResponse:
    """Machine side. Called by the executor at a **tool** boundary.

    The model boundary does not use this route: it already claims nudges, and a
    stop and a queue are one decision at one instant, so the halt rides that
    response instead. This route exists because a tool boundary has no model
    request to mutate and therefore no queue to claim - and because without a
    check here, a stop pressed while the model was thinking would let the tool
    run and only block the call *after* it. That is the difference between
    stopping the agent before it sends the email and stopping it afterwards.

    Claim and apply are one statement. There is no window in which a halt is
    claimed but not applied, so there is nothing for a lost response to strand.
    """
    service = HaltsService(db)
    halt = await service.apply_at_boundary(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        boundary=HaltBoundary(request.boundary),
        tool_name=request.tool_name,
    )
    await db.commit()
    return ClaimHaltResponse(session_key=session_key, halt=halt)


@router.post(
    "/{session_key}/halts/ack",
    response_model=AckHaltResponse,
    summary="Record which tool an applied stop caught",
    response_description="The halt after the acknowledgement",
)
async def ack_halt(
    session_key: str,
    request: AckHaltRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_NUDGES_CONSUME,
            context_builder=session_target_context,
        )
    ),
) -> AckHaltResponse:
    """Machine side. Optional enrichment of a halt that already applied.

    The claim moved the row, so losing this call costs one word of transcript
    copy - "before running ``send_email``" degrading to "before its next step"
    - rather than the truth of the record. It can write one field and cannot
    change a status.

    ``applied_tool_name`` is the single field in this design carrying bytes
    chosen by a process running arbitrary agent code, on their way to an
    operator console. It is pattern-checked against a strict identifier at the
    model boundary and capped, and it renders as plain text.
    """
    service = HaltsService(db)
    halt = await service.enrich_applied(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        halt_id=request.id,
        applied_tool_name=request.applied_tool_name,
    )
    await db.commit()
    return AckHaltResponse(session_key=session_key, halt=halt)
