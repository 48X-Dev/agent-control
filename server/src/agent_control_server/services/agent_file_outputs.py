"""What makes a stored file the agent's: its marker, its binding, its supersession.

Separated from the service next door on that module's own argument. It stores
and counts; this decides one question about one caller, and the question is
worth reading on its own.

Two rules do the work here. **A file is bound to its turn as it is stored**,
because an upload nothing carries is exactly what the orphan sweep reclaims, and
it would die quietly hours later on somebody else's upload. **One live draft per
step**, because a step with a five-turn ceiling would otherwise leave five
near-identical workbooks hanging off one ticket.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from agent_control_models.attachments import (
    AgentOutputKind,
    AttachmentOrigin,
    AttachmentStatus,
)
from agent_control_models.errors import ErrorCode
from sqlalchemy import select

from ..errors import BadRequestError, ConflictError, ForbiddenError
from ..models import AgentSession, AgentSessionAttachment
from .attachment_binding import record_bindings

if TYPE_CHECKING:
    from .agent_attachments import AgentAttachmentsService


def require_agent_turn(
    agent_output: AgentOutputKind | None,
    session: AgentSession,
    *,
    from_the_agent: bool,
) -> str | None:
    """Return the turn an agent's file binds to, or ``None`` for anyone else."""
    if agent_output is not None and not from_the_agent:
        raise ForbiddenError(
            error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
            detail="Only an agent's own session token may mark a file draft or final.",
            resource="AgentSession",
            resource_id=session.session_key,
            hint="Attach the file without agent_output.",
        )
    if not from_the_agent:
        return None
    if agent_output is None:
        raise BadRequestError(
            error_code=ErrorCode.VALIDATION_ERROR,
            detail="An agent's file has to say whether it is a draft or the final one.",
            hint="Send agent_output=draft while the work continues, or final when it is done.",
        )
    if session.in_flight_trace_id is None:
        raise ConflictError(
            error_code=ErrorCode.TURN_IN_FLIGHT,
            detail="This session has no turn running, so there is nothing to bind the file to.",
            resource="AgentSession",
            resource_id=session.session_key,
            hint="Produce the file from inside a turn.",
        )
    return session.in_flight_trace_id


async def record_agent_output(
    service: AgentAttachmentsService,
    *,
    row: AgentSessionAttachment,
    kind: AgentOutputKind,
    trace_id: str,
) -> None:
    """Mark the file, bind it to its turn, and end this step's earlier drafts."""
    if row.origin != AttachmentOrigin.AGENT.value:
        raise ConflictError(
            error_code=ErrorCode.ATTACHMENT_NOT_READY,
            detail="These bytes are already on this session as a file somebody else attached.",
            resource="Attachment",
            resource_id=row.attachment_key,
            hint="Change the contents, or use the file that is already here.",
        )
    row.agent_output_kind = kind.value
    row.updated_at = dt.datetime.now(dt.UTC)
    await service.db.flush()
    await record_bindings(
        service.db,
        namespace_key=row.namespace_key,
        session_id=row.session_id,
        trace_id=trace_id,
        attachment_keys=[row.attachment_key],
        included_keys=(row.attachment_key,),
    )
    await _supersede_drafts(service, row=row)


async def _supersede_drafts(
    service: AgentAttachmentsService, *, row: AgentSessionAttachment
) -> None:
    """This row's arrival ends every live draft the step had before it.

    Keyed on the session rather than the step because the dispatcher opens one
    session per step, so the two are the same set of rows.
    """
    stmt = select(AgentSessionAttachment).where(
        AgentSessionAttachment.namespace_key == row.namespace_key,
        AgentSessionAttachment.session_id == row.session_id,
        AgentSessionAttachment.id != row.id,
        AgentSessionAttachment.origin == AttachmentOrigin.AGENT.value,
        AgentSessionAttachment.agent_output_kind == AgentOutputKind.DRAFT.value,
        AgentSessionAttachment.status != AttachmentStatus.TOMBSTONED.value,
    )
    for superseded in (await service.db.execute(stmt)).scalars().all():
        await service.tombstone(row=superseded)
