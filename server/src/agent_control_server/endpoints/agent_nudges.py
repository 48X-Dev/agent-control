"""HTTP endpoints for nudging a running agent.

Two audiences, two credentials, on the same paths.

A human queues, lists and withdraws guidance with an ordinary API key under
``agent_nudges.write`` and ``agent_sessions.content_read``. The executor claims
and acknowledges it with a runtime token bound to one session under
``agent_nudges.consume``. That split is the whole authorization design: an
executor holding a long-lived key could claim and silently swallow nudges for
any session in the namespace, and a claim that swallows shows the human
"delivered" for a message nobody read.

The claim and acknowledge routes sit under the session path so the context
builder can pluck ``session_key`` straight out of the path parameters and hand
it to the token verifier, which refuses any token bound to a different session.
A token minted for session A physically cannot claim session B.
"""

from __future__ import annotations

from typing import Any

from agent_control_models.nudges import (
    AckNudgesRequest,
    AckNudgesResponse,
    CancelNudgeResponse,
    ClaimNudgesRequest,
    ClaimNudgesResponse,
    CreateNudgeRequest,
    CreateNudgeResponse,
    ListNudgesResponse,
    NudgeStatus,
)
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..db import get_async_db
from ..services.agent_sessions import RUNTIME_TOKEN_TARGET_TYPE
from ..services.caller_identity import hash_caller_id
from ..services.nudges import NudgesService

router = APIRouter(prefix="/agent-sessions", tags=["agent-nudges"])


async def session_target_context(request: Request) -> dict[str, Any]:
    """Surface the path's session key to the runtime-token verifier.

    Mirrors ``_exchange_context`` in ``endpoints/auth.py``. The verifier
    compares these against the token's own claims and refuses a mismatch, which
    is what makes the token the session identity rather than a bearer of
    namespace-wide power.
    """
    return {
        "target_type": RUNTIME_TOKEN_TARGET_TYPE,
        "target_id": request.path_params.get("session_key"),
    }


@router.post(
    "/{session_key}/nudges",
    response_model=CreateNudgeResponse,
    summary="Queue guidance for the agent's next model call",
    response_description="The queued nudge",
)
async def create_nudge(
    session_key: str,
    request: CreateNudgeRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_NUDGES_WRITE)),
) -> CreateNudgeResponse:
    """Queue one sentence for the agent to read at its next model call.

    Read the timing literally. Nothing here interrupts a running tool: a nudge
    queued while the agent is forty seconds into a tool call is delivered after
    that tool returns, not now. A panel that implied the agent stopped and read
    it would be exposed by the first long tool call.

    What the agent is shown is the text as typed, delimited and labelled as
    operator input, appended as a **user turn**. That matters beyond
    presentation: a user turn is what this deployment's controls evaluate, and
    the SDK additionally evaluates the body as its own step before injecting
    it. A denied nudge comes back ``rejected``, naming the control.

    Refusals: an unknown session is 404, somebody else's session is 403, and a
    session whose queue is already full is 429.
    """
    service = NudgesService(db)
    nudge = await service.create(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        body=request.body,
    )
    await db.commit()
    return CreateNudgeResponse(nudge=nudge)


@router.get(
    "/{session_key}/nudges",
    response_model=ListNudgesResponse,
    summary="List the nudges queued for a session",
    response_description="Nudges, newest first",
)
async def list_nudges(
    session_key: str,
    status: NudgeStatus | None = Query(
        None, description="Optional status filter."
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_SESSION_CONTENT_READ)
    ),
) -> ListNudgesResponse:
    """Return every nudge on this session and where each one got to.

    Read under ``content_read`` rather than ``agent_sessions.read`` because a
    nudge body is a human prompt: same sensitivity class as the transcript, and
    it would be odd to refuse someone the conversation and then hand them the
    operator's half of it.
    """
    service = NudgesService(db)
    nudges = await service.list_nudges(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        status=status,
    )
    return ListNudgesResponse(session_key=session_key, nudges=nudges)


@router.delete(
    "/{session_key}/nudges/{nudge_id}",
    response_model=CancelNudgeResponse,
    summary="Withdraw a queued nudge",
    response_description="The nudge after the attempt",
)
async def cancel_nudge(
    session_key: str,
    nudge_id: int,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_NUDGES_WRITE)),
) -> CancelNudgeResponse:
    """Take back a nudge nobody has claimed yet.

    A claimed nudge is a 409, not a best-effort cancel. Once an executor has
    taken it for a model call the text may already be inside a model request,
    and reporting a withdrawal that did not happen is the one failure mode this
    queue is built to avoid, arrived at from the other direction.
    """
    service = NudgesService(db)
    nudge, cancelled = await service.cancel(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        nudge_id=nudge_id,
    )
    await db.commit()
    return CancelNudgeResponse(cancelled=cancelled, nudge=nudge)


@router.post(
    "/{session_key}/nudges/claim",
    response_model=ClaimNudgesResponse,
    summary="Claim guidance, and any stop, for this model boundary",
    response_description="What this boundary should do",
)
async def claim_nudges(
    session_key: str,
    request: ClaimNudgesRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_NUDGES_CONSUME,
            context_builder=session_target_context,
        )
    ),
) -> ClaimNudgesResponse:
    """Machine side. Called by the executor at every model boundary.

    Answers one question - what should happen at this boundary - and answers it
    in one round trip on purpose. A stop and a queue are one decision at one
    instant, so ``halt`` rides this response rather than costing a second call
    on a path that already talks to this server once per model step.

    Precedence is decided here, not in the SDK: when a stop is bound to the
    turn in flight, the halt comes back, the nudge list is empty, and no nudge
    counter moves. Guidance injected into a request whose response is about to
    be replaced by a block would be recorded as delivered while no model read
    it.

    Claims are leases. An executor that dies holding one loses it when the
    lease lapses and the nudge is delivered again, because a duplicate sentence
    is harmless and a dropped one is not.
    """
    service = NudgesService(db)
    response = await service.claim(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        claimed_by=hash_caller_id(principal.caller_id),
        max_nudges=request.max_nudges,
    )
    await db.commit()
    return response


@router.post(
    "/{session_key}/nudges/ack",
    response_model=AckNudgesResponse,
    summary="Report what became of claimed nudges",
    response_description="The nudges after the acknowledgement",
)
async def ack_nudges(
    session_key: str,
    request: AckNudgesRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_NUDGES_CONSUME,
            context_builder=session_target_context,
        )
    ),
) -> AckNudgesResponse:
    """Machine side. Close the loop on nudges this executor claimed.

    Four outcomes, and only one of them means a model saw the text.
    ``applied`` records delivery and the turn it landed in. ``released`` puts
    the surplus over the per-call cap back on the queue untouched - no counter
    moves, because nothing was attempted. ``failed`` is an injection that was
    really tried and did not land, and is the only outcome that can eventually
    expire a nudge. ``rejected`` is a control denial and names the control.

    Acknowledgements for nudges that are no longer claimed are ignored rather
    than refused: an executor retrying an acknowledgement whose response was
    lost should not be punished for it.
    """
    service = NudgesService(db)
    nudges = await service.acknowledge(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        acks=list(request.acks),
    )
    await db.commit()
    return AckNudgesResponse(session_key=session_key, nudges=nudges)
