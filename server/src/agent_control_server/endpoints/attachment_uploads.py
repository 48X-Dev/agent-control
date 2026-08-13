"""The upload body both attachment routes run, and the checks around it.

Route-free on purpose. Two endpoints store a file against a session - a person
with a cookie on ``agent_attachments.write``, an agent with its own session
token on ``agent_attachments.write_self`` - and an endpoint selects exactly one
operation, so they cannot be one route. Everything they do after the
authorizer agrees is identical and lives here once.
"""

from __future__ import annotations

from typing import NoReturn

from agent_control_models.attachments import (
    AgentOutputKind,
    AttachmentOrigin,
    CreateAttachmentResponse,
)
from agent_control_models.errors import ErrorCode, ErrorReason
from fastapi import Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Principal
from ..config import executor_settings
from ..errors import APIError, BadRequestError
from ..middleware import attachment_too_large
from ..services.agent_attachments import AgentAttachmentsService, to_wire
from ..services.agent_file_outputs import record_agent_output, require_agent_turn
from ..services.agent_sessions import (
    RUNTIME_TOKEN_TARGET_TYPE,
    AgentSessionsService,
    require_executor_enabled,
)
from ..services.attachment_access import authorize_attachment_write
from ..services.attachment_blobs import get_attachment_blob_store
from ..services.attachment_conversions import schedule_conversion
from ..services.caller_identity import hash_caller_id
from ..services.executor_metrics import (
    ATTACHMENT_UPLOAD_REJECTED,
    ATTACHMENT_UPLOAD_TOO_LARGE,
    ATTACHMENT_UPLOADS,
)

_UPLOAD_CHUNK_BYTES = 256 * 1024
REQUESTED_WITH_HEADER = "X-Requested-With"
DECLARED_NAME_MAX_LENGTH = 512
"""What the *form field* may carry, before normalization cuts it to 128. Larger
than the stored cap on purpose: a long name is normalized, not refused."""

ATTACHMENTS_DISABLED_MESSAGE = (
    "Attachments are not enabled on this server. Set "
    "AGENT_CONTROL_EXECUTOR_ATTACHMENTS_ENABLED=true to turn them on."
)


def require_attachments_enabled() -> None:
    """Two switches, and both must be on.

    The executor gate is inherited rather than re-implemented: an attachment
    with nothing to deliver it to is a stored file and no feature.
    """
    require_executor_enabled(executor_settings)
    if not executor_settings.attachments_enabled:
        raise APIError(
            status_code=503,
            error_code=ErrorCode.EXECUTOR_UNAVAILABLE,
            reason=ErrorReason.SERVICE_UNAVAILABLE,
            detail=ATTACHMENTS_DISABLED_MESSAGE,
        )


def attachment_service(db: AsyncSession) -> AgentAttachmentsService:
    return AgentAttachmentsService(
        db, settings=executor_settings, blobs=get_attachment_blob_store()
    )


def is_the_sessions_own_token(principal: Principal, *, session_key: str) -> bool:
    """Whether the caller is the agent running inside this very session.

    Read off the token's own binding rather than off anything the request
    carries, because it decides provenance and provenance must not be forgeable.
    """
    return principal.target_type == RUNTIME_TOKEN_TARGET_TYPE and principal.target_id == session_key


def require_xhr(request: Request) -> None:
    """Force a preflight, which is what stops a cross-origin form posting a file."""
    if request.headers.get(REQUESTED_WITH_HEADER):
        return
    raise BadRequestError(
        error_code=ErrorCode.VALIDATION_ERROR,
        detail=f"This route requires the {REQUESTED_WITH_HEADER} header.",
        hint=(
            "Send X-Requested-With: XMLHttpRequest. It forces a preflight, "
            "which is what keeps a cross-origin HTML form from posting a "
            "file into your session."
        ),
    )


def _refuse_too_large(counted: int | None) -> NoReturn:
    ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_TOO_LARGE).inc()
    raise attachment_too_large(counted)


async def read_capped(file: UploadFile) -> bytes:
    """Read the part in chunks, and refuse an empty one.

    The cap is enforced upstream, on the receive channel, so this count is the
    second of two. The zero-byte refusal has no upstream equivalent and lives
    only here: a multipart body can be well-formed, correctly sized and empty.
    """
    cap = executor_settings.attachment_max_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            _refuse_too_large(total)
        chunks.append(chunk)
    if total == 0:
        ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_REJECTED).inc()
        raise BadRequestError(
            error_code=ErrorCode.VALIDATION_ERROR,
            detail="This upload carried no bytes.",
            hint="An empty file is never what anyone meant. Attach the real one.",
        )
    return b"".join(chunks)


async def store_upload(
    request: Request,
    *,
    session_key: str,
    file: UploadFile,
    declared_name: str,
    agent_output: AgentOutputKind | None,
    db: AsyncSession,
    principal: Principal,
) -> CreateAttachmentResponse:
    """Store one file against one session. Nothing is delivered to a model here.

    Two callers, and the only thing that differs between them is what the
    token says: an agent's own session token stores ``origin=agent``, marks the
    file draft or final, and binds it to the turn that produced it before this
    returns, because an unbound upload is what the orphan sweep reclaims.
    """
    require_attachments_enabled()
    require_xhr(request)

    session = await AgentSessionsService(db).get_row_or_404(
        namespace_key=principal.namespace_key, session_key=session_key
    )
    creator_hash = hash_caller_id(principal.caller_id)
    from_the_agent = is_the_sessions_own_token(principal, session_key=session_key)
    authorize_attachment_write(
        session,
        caller_hash=creator_hash,
        is_admin=principal.is_admin,
        session_bound_token=from_the_agent,
    )
    trace_id = require_agent_turn(agent_output, session, from_the_agent=from_the_agent)

    # A session token's caller is whoever *created* the session, so every
    # session one dispatcher opens would otherwise share one upload bucket.
    # The token's target is the session and the verifier refused to let the
    # caller choose it, which makes it the only unforgeable per-session key.
    rate_hash = (
        hash_caller_id(principal.target_id) if from_the_agent else creator_hash
    )

    data = await read_capped(file)
    service = attachment_service(db)
    row, deduplicated = await service.create(
        namespace_key=principal.namespace_key,
        session=session,
        caller_hash=creator_hash,
        rate_limit_hash=rate_hash,
        declared_name=declared_name or file.filename or "",
        declared_mime=file.content_type or "",
        data=data,
        origin=AttachmentOrigin.AGENT if from_the_agent else AttachmentOrigin.OPERATOR_UPLOAD,
        agent_output=agent_output,
    )
    if agent_output is not None and trace_id is not None:
        await record_agent_output(service, row=row, kind=agent_output, trace_id=trace_id)
    attachment = to_wire(row, session_key=session_key)
    attachment_id = row.id
    source_sha256 = row.source_sha256
    sniffed_mime = row.sniffed_mime
    await db.commit()

    # Reading the file is queued here and never waited for. Conversion is tens
    # of seconds on this deployment's corpus and an upload that blocked on it
    # would hold a connection and a browser for the length of an OCR run.
    schedule_conversion(
        namespace_key=principal.namespace_key,
        attachment_id=attachment_id,
        source_sha256=source_sha256,
        declared_mime=sniffed_mime,
    )
    return CreateAttachmentResponse(attachment=attachment, deduplicated=deduplicated)
