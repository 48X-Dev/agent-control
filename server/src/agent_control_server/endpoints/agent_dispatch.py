"""HTTP routes for the fleet's ceilings and its stop levels.

Four levels, ordered by increasing **authority** rather than by increasing
desperation, and level 3 is the authoritative one. Three of them are here; the
fourth is a runbook, because nothing in an API kills a tool that is already
executing.

**Level 1, stop new work.** ``POST /agent-dispatch/pause``. Import refuses,
claim refuses, and every dispatch-origin turn refuses inside ``_acquire_turn``.
Effect within one step, and it does not depend on the dispatcher cooperating -
which is the whole reason that check lives on the turn path rather than in the
loop.

**Level 2, stop what is running.** ``POST /agent-dispatch/halt-fleet``.
Best-effort, and the console must say so: a halt lands only at a boundary the
executor reaches, the executor swallows a failed post and runs the tool anyway,
and nothing here stops a tool already executing.

**Level 3, refuse everything.** ``POST /agent-dispatch/halt-executors``. One
flag refuses every new session and every new turn in the namespace. **It stops
human chat too**, and the UI copy has to say so where the button is: that is
usually what an operator wants and always what they should be told.

**Level 4, kill the processes.** ``docker compose stop agent-executor-*`` or the
deployment's equivalent. It is a documented level rather than "not our problem"
because a genuinely stuck fleet needs it and an operator should not be inventing
it during an incident.

Every route here is a request about one row, or one statement over sessions.
None of them starts anything, and none of them holds a connection across an
executor call.
"""

from __future__ import annotations

from agent_control_models.dispatch import (
    DispatchStateResponse,
    GetDispatchStateResponse,
    HaltExecutorsRequest,
    HaltFleetResponse,
    PauseDispatchRequest,
)
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..config import dispatch_settings
from ..db import get_async_db
from ..services.agent_dispatch_state import (
    read_snapshot,
    set_executors_halted,
    set_paused,
)
from ..services.caller_identity import hash_caller_id
from ..services.halt_fleet import halt_fleet

router = APIRouter(prefix="/agent-dispatch", tags=["agent-dispatch"])


@router.get(
    "",
    response_model=GetDispatchStateResponse,
    summary="Read this namespace's dispatch ceilings and stop state",
    response_description="Both switches, and what is left of the hour",
)
async def get_dispatch_state(
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_READ)),
) -> GetDispatchStateResponse:
    """What a banner renders and what a confirm warns about. Advisory.

    A namespace nobody has dispatched in has no row, and this answers with the
    deployment's defaults rather than creating one: a read that writes cannot be
    served from a replica, and a console polls this.

    The numbers here are not the ceiling. The ceiling is the refusal inside
    ``_acquire_turn`` and the count inside the import transaction, which is
    stated in three places on purpose - a preview that reports a budget is
    exactly the shape of thing a later reader turns into the enforcement point.
    """
    state = await read_snapshot(
        db, namespace_key=principal.namespace_key, settings=dispatch_settings
    )
    return GetDispatchStateResponse(state=state)


@router.post(
    "/pause",
    response_model=DispatchStateResponse,
    summary="Level 1: stop new dispatch work",
    response_description="The state after the pause",
)
async def pause_dispatch(
    request: PauseDispatchRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_DISPATCH_PAUSE)),
) -> DispatchStateResponse:
    """Stop new work. Running turns keep running, and the banner must say so.

    Idempotent. Pressing pause on a paused namespace overwrites the reason and
    the credential tag, which is what an operator escalating an incident
    expects, and answers 200 rather than a conflict: telling somebody their
    second press failed invites a third.
    """
    state = await set_paused(
        db,
        namespace_key=principal.namespace_key,
        paused=True,
        caller_hash=hash_caller_id(principal.caller_id),
        reason=request.reason,
        settings=dispatch_settings,
    )
    await db.commit()
    return DispatchStateResponse(state=state)


@router.post(
    "/resume",
    response_model=DispatchStateResponse,
    summary="Level 1, cleared: allow new dispatch work again",
    response_description="The state after the resume",
)
async def resume_dispatch(
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_DISPATCH_PAUSE)),
) -> DispatchStateResponse:
    """Clear the pause, and the reason with it.

    Deliberately does **not** clear the executor halt. They are two flags rather
    than one enum precisely so an operator who escalated from a pause to a halt
    can step back down one level without the other silently going with it.
    """
    state = await set_paused(
        db,
        namespace_key=principal.namespace_key,
        paused=False,
        caller_hash=hash_caller_id(principal.caller_id),
        reason=None,
        settings=dispatch_settings,
    )
    await db.commit()
    return DispatchStateResponse(state=state)


@router.post(
    "/halt-fleet",
    response_model=HaltFleetResponse,
    summary="Level 2: bind a stop to every turn running in this namespace",
    response_description="What the statement saw and what it wrote",
)
async def halt_fleet_route(
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_HALTS_WRITE_ALL)),
) -> HaltFleetResponse:
    """One statement, one transaction, and it cannot half-succeed.

    Not a loop over the single-session halt route: that path enforces a
    per-caller quota of thirty a minute, so a loop over more than thirty
    sessions would 429 partway through under exactly the condition that
    motivates a fleet stop. A safety control that degrades as the incident grows
    is worse than none, because it is trusted.

    **The response is a record of what was requested, not of anything that
    stopped.** Halt delivery is best-effort at the executor and fails open when
    the control plane is unreachable. The console must not render this as a
    stop, and no level short of killing the process reaches a tool that is
    already executing.
    """
    result = await halt_fleet(
        db,
        namespace_key=principal.namespace_key,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()
    return result


@router.post(
    "/halt-executors",
    response_model=DispatchStateResponse,
    summary="Level 3: refuse every new session and every new turn",
    response_description="The state after the halt",
)
async def halt_executors(
    request: HaltExecutorsRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_DISPATCH_PAUSE)),
) -> DispatchStateResponse:
    """The authoritative stop, and the one whose copy has to be honest.

    It refuses every new session and every new turn in this namespace, **human
    chat included**. Turns already running are not stopped by it; that is level
    2, and level 2 is a request.

    A flag rather than a sweep that disables every ``agent_runtimes`` binding.
    Bindings already disabled for unrelated reasons would be indistinguishable
    afterwards, so recovery would turn on things somebody deliberately turned
    off. An emergency stop that destroys the state you need to recover from it
    makes operators reluctant to press it, which is the worst property an
    emergency stop can have.
    """
    state = await set_executors_halted(
        db,
        namespace_key=principal.namespace_key,
        halted=True,
        caller_hash=hash_caller_id(principal.caller_id),
        reason=request.reason,
        settings=dispatch_settings,
    )
    await db.commit()
    return DispatchStateResponse(state=state)


@router.post(
    "/release-executors",
    response_model=DispatchStateResponse,
    summary="Level 3, cleared: allow sessions and turns again",
    response_description="The state after the release",
)
async def release_executors(
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_DISPATCH_PAUSE)),
) -> DispatchStateResponse:
    """Clear the halt. A pause set separately stays set, and the response says so."""
    state = await set_executors_halted(
        db,
        namespace_key=principal.namespace_key,
        halted=False,
        caller_hash=hash_caller_id(principal.caller_id),
        reason=None,
        settings=dispatch_settings,
    )
    await db.commit()
    return DispatchStateResponse(state=state)
