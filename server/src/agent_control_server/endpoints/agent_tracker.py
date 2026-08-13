"""The one route an agent can use to write outside this system.

An agent asked to save something puts it on the issue its own task came from.
Three properties are the whole authorization design, and each is a refusal of
something that would otherwise be easy.

**The issue is resolved, never named.** The request body has no issue field.
The server reads the session, follows it to its task, and takes the task's
``source_ref``. A model cannot address a ticket, so a prompt injection arriving
in fetched web content cannot redirect the write, and the reach of a stolen
session token is one issue that token's session was already working on.

**It comments and cannot close.** ``WritebackKind.STATUS_CHANGE`` is the kind
that moves an issue's state and it goes nowhere without ``agent_tasks.approve``,
which no session token carries. This route posts text and that is all it can
ever do. The split is the same one the queue already draws.

**Sending is inline and reported.** Step comments queue because a task can die
between steps and the comment still has to arrive. This one is sent while the
caller waits, because an agent that was told to save something needs to say
whether it saved, and "queued" is not an answer to that.
"""

from __future__ import annotations

import logging

from agent_control_models.errors import ErrorCode
from agent_control_models.sessions import (
    SaveTrackerCommentRequest,
    SaveTrackerCommentResponse,
)
from agent_control_models.tasks import TaskSourceKind
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..db import get_async_db
from ..errors import ConflictError, NotFoundError, ServiceUnavailableError
from ..models import AgentSession, AgentTask
from ..services.linear_writeback_compose import (
    compose_agent_comment_body,
)
from ..services.linear_writeback_runtime import WritebackRuntime, get_writeback_runtime
from .agent_nudges import session_target_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent-sessions", tags=["agent-tracker"])


@router.post(
    "/{session_key}/tracker-comment",
    response_model=SaveTrackerCommentResponse,
    summary="Post a comment on the issue this session's task came from",
    response_description="The comment that was created",
)
async def save_tracker_comment(
    session_key: str,
    request: SaveTrackerCommentRequest,
    db: AsyncSession = Depends(get_async_db),
    runtime: WritebackRuntime = Depends(get_writeback_runtime),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_TRACKER_COMMENT,
            context_builder=session_target_context,
        )
    ),
) -> SaveTrackerCommentResponse:
    """Machine side. Save text an agent was asked to save.

    Refusals, in order of specificity, and each says something the agent can
    repeat to the person who asked: an unknown session is 404, a session with
    no task or a task with nothing to write to is 409
    ``SESSION_HAS_NO_TRACKER_ISSUE``, a deployment with write-back off is 409
    ``LINEAR_WRITE_DISABLED``, and a tracker that will not answer is 503.

    A dry-run task refuses here for the reason it refuses everywhere else: a
    dry run that comments has recorded work that never happened.
    """
    session = await db.scalar(
        select(AgentSession).where(
            AgentSession.namespace_key == principal.namespace_key,
            AgentSession.session_key == session_key,
        )
    )
    if session is None:
        raise NotFoundError(
            error_code=ErrorCode.AGENT_SESSION_NOT_FOUND,
            detail="No session with that key in this namespace.",
            resource="AgentSession",
            resource_id=session_key,
        )

    task = (
        await db.scalar(select(AgentTask).where(AgentTask.id == session.agent_task_id))
        if session.agent_task_id is not None
        else None
    )
    _refuse_when_there_is_nothing_to_comment_on(task, session_key=session_key)
    assert task is not None  # narrowed by the refusal above

    if not runtime.can_write:
        raise ConflictError(
            error_code=ErrorCode.LINEAR_WRITE_DISABLED,
            detail="Write-back to the tracker is disabled on this deployment.",
            resource="AgentSession",
            resource_id=session_key,
            hint="Set AGENT_CONTROL_LINEAR_WRITE_ENABLED=true and restart the server.",
        )
    client = runtime.client
    assert client is not None  # can_write is exactly this being set

    body = compose_agent_comment_body(
        task_key=task.task_key,
        agent_name=session.agent_name,
        text=request.text,
    )
    try:
        comment_id = await client.create_comment(issue_id=task.source_ref, body=body)
    except Exception as exc:  # noqa: BLE001 - reported to the agent, never raised at it
        logger.warning("Saving an agent comment to the tracker failed.", exc_info=True)
        raise ServiceUnavailableError(
            error_code=ErrorCode.LINEAR_UNAVAILABLE,
            detail="The tracker did not accept the comment. Nothing was posted.",
            resource="AgentSession",
            resource_id=session_key,
        ) from exc

    return SaveTrackerCommentResponse(
        issue_ref=task.source_ref,
        issue_url=task.source_url,
        comment_id=comment_id,
    )


def _refuse_when_there_is_nothing_to_comment_on(
    task: AgentTask | None, *, session_key: str
) -> None:
    """One error code, three causes, and the detail names which one it was."""

    if task is None:
        reason = (
            "This session was opened as a chat rather than for a task, so there "
            "is no issue to comment on."
        )
    elif task.source_kind != TaskSourceKind.LINEAR.value:
        reason = (
            f"This session's task came from {task.source_kind!r}, which has no "
            "tracker to write to."
        )
    elif task.dry_run:
        reason = (
            "This session's task is a dry run. A dry run that comments records "
            "work that never happened."
        )
    else:
        return
    raise ConflictError(
        error_code=ErrorCode.SESSION_HAS_NO_TRACKER_ISSUE,
        detail=reason,
        resource="AgentSession",
        resource_id=session_key,
    )
