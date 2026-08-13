"""The route an agent stores its own file on.

A separate route from the console's upload rather than a second caller on it,
and the reason is mechanical: a route selects one operation, and these two need
different authorizers. The console arrives with a cookie under
``agent_attachments.write``, which the default authorizer serves. An agent
arrives with a session-bound runtime token under
``agent_attachments.write_self``, which only the runtime override reads - a
Bearer token presented to the default authorizer is a 401, and an API key
presented instead is a 403 at ADMIN.

The binding is the provenance. ``origin=agent`` is written for a caller whose
token names the session in the path, and the verifier already refused a token
naming any other session, so an agent cannot store a file into a conversation
it is not running inside and nobody else can store one that claims a model
wrote it.
"""

from __future__ import annotations

from agent_control_models.attachments import AgentOutputKind, CreateAttachmentResponse
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..db import get_async_db
from .agent_nudges import session_target_context
from .attachment_uploads import DECLARED_NAME_MAX_LENGTH, store_upload

router = APIRouter(prefix="/agent-sessions", tags=["agent-attachments"])


@router.post(
    "/{session_key}/attachments/agent-output",
    response_model=CreateAttachmentResponse,
    status_code=201,
    summary="Store a file the agent produced against its own session",
    response_description="The stored attachment",
)
async def create_agent_output(
    request: Request,
    session_key: str,
    file: UploadFile = File(..., description="The file itself."),
    declared_name: str = Form(
        "",
        max_length=DECLARED_NAME_MAX_LENGTH,
        description=(
            "What to call this file. Normalized server-side and never stored "
            "verbatim. Falls back to the part's own filename when omitted."
        ),
    ),
    agent_output: AgentOutputKind = Form(
        ...,
        description="Whether this is the agent's working draft or its deliverable.",
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(
            Operation.AGENT_ATTACHMENTS_WRITE_SELF,
            context_builder=session_target_context,
        )
    ),
) -> CreateAttachmentResponse:
    """Machine side. Store one file the agent wrote, marked and bound to its turn.

    ``agent_output`` is required here and refused on the console's route: a
    file with no marker would be a deliverable nothing can tell from working
    state. A session with no turn running is a 409 - there is nothing to bind
    to, and an unbound agent file is what the orphan sweep reclaims.
    """
    return await store_upload(
        request,
        session_key=session_key,
        file=file,
        declared_name=declared_name,
        agent_output=agent_output,
        db=db,
        principal=principal,
    )
