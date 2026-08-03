"""HTTP endpoints for files attached to a chat session.

Five routes: upload one, list them, read one's metadata, download its bytes,
delete it. Nothing here reaches a model. Delivery is a separate concern and a
turn is what performs it.

Four decisions about this module are security-relevant and none of them is
obvious from the route table.

**The body is capped in middleware, not here.** No body limit exists anywhere
else in this server, so an unbounded POST is otherwise accepted by every
endpoint - and a check written into this handler would be too late, because
``UploadFile`` makes FastAPI parse the whole multipart body during dependency
solving, spooling it to a temp file, before the first line below runs.
:class:`~..middleware.AttachmentUploadBodyLimit` counts the body off the ASGI
receive channel and abandons the request past the cap, which is the only place
that decision can be made in time. The chunked read below is a second count
over bytes already known to be bounded, kept because it is what refuses a zero
byte upload and what would still hold if the middleware were ever unmounted.

**``X-Requested-With`` is required, and this route is the reason.** The console
authenticates by cookie, and ``multipart/form-data`` is the one content type a
cross-origin HTML form can send with no preflight. Today the only thing between
that and cross-origin file injection into a victim's session is ``samesite=lax``
on the session cookie. That holds, but nothing recorded the dependency, so
anyone loosening the cookie for an embedding reason would open it silently.
Requiring a custom header forces a preflight regardless of cookie policy, and a
server test asserts the cookie is still ``lax`` so the assumption fails loudly.

**Downloads are forced and never rendered.** ``application/octet-stream``,
``Content-Disposition: attachment`` and ``nosniff`` on every response, whatever
the file's own type says. An uploaded HTML file served inline from this origin
is stored cross-site scripting against the console, and the header is where that
control lives - not in the accepted-type gate, which a later phase may widen.

**Metadata and content are different operations.** Reads sit on
``agent_sessions.content_read``, the same operation as the transcript the file
appears in. Writes sit on ``agent_attachments.write``, at the tier that starts a
turn, because putting bytes in front of a model is driving the conversation.
"""

from __future__ import annotations

from typing import NoReturn
from urllib.parse import quote

from agent_control_models.attachments import (
    AttachmentOrigin,
    AttachmentStatus,
    AttachmentVariant,
    CreateAttachmentResponse,
    DeleteAttachmentResponse,
    GetAttachmentResponse,
    ListAttachmentsResponse,
)
from agent_control_models.errors import ErrorCode, ErrorReason
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..config import executor_settings
from ..db import get_async_db
from ..errors import APIError, BadRequestError, NotFoundError
from ..middleware import attachment_too_large
from ..models import AgentSession, AgentTurnAttachment
from ..services.agent_attachments import (
    DELETE_NOTICE,
    TOMBSTONE_NOTICE,
    AgentAttachmentsService,
    to_wire,
)
from ..services.agent_sessions import (
    AgentSessionsService,
    require_content_access,
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

router = APIRouter(prefix="/agent-sessions", tags=["agent-attachments"])

_UPLOAD_CHUNK_BYTES = 256 * 1024
_REQUESTED_WITH_HEADER = "X-Requested-With"
_DECLARED_NAME_MAX_LENGTH = 512
"""What the *form field* may carry, before normalization cuts it to 128. Larger
than the stored cap on purpose: a long name is normalized, not refused."""

ATTACHMENTS_DISABLED_MESSAGE = (
    "Attachments are not enabled on this server. Set "
    "AGENT_CONTROL_EXECUTOR_ATTACHMENTS_ENABLED=true to turn them on."
)


def _require_attachments_enabled() -> None:
    """Two switches, and both must be on.

    The executor gate is inherited rather than re-implemented: an attachment
    with nothing to deliver it to is a stored file and no feature, and the
    startup refusal that already covers an enabled executor on an
    unauthenticated server therefore covers this too. The second switch is so
    a deployment can run chat without accepting files.
    """
    require_executor_enabled(executor_settings)
    if not executor_settings.attachments_enabled:
        raise APIError(
            status_code=503,
            error_code=ErrorCode.EXECUTOR_UNAVAILABLE,
            reason=ErrorReason.SERVICE_UNAVAILABLE,
            detail=ATTACHMENTS_DISABLED_MESSAGE,
        )


def _service(db: AsyncSession) -> AgentAttachmentsService:
    return AgentAttachmentsService(
        db, settings=executor_settings, blobs=get_attachment_blob_store()
    )


async def _session_for_read(
    db: AsyncSession, *, namespace_key: str, session_key: str, principal: Principal
) -> AgentSession:
    row = await AgentSessionsService(db).get_row_or_404(
        namespace_key=namespace_key, session_key=session_key
    )
    require_content_access(
        row,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        for_turn=False,
    )
    return row


def _refuse_too_large(counted: int | None) -> NoReturn:
    ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_TOO_LARGE).inc()
    raise attachment_too_large(counted)


async def _read_capped(file: UploadFile) -> bytes:
    """Read the part in chunks, and refuse an empty one.

    The cap is enforced upstream, on the receive channel, before any of these
    bytes existed as a buffer. This count is the second of two and exists so
    that unmounting the middleware degrades the guarantee rather than removing
    it. The zero-byte refusal has no upstream equivalent and lives only here:
    a multipart body can be well-formed, correctly sized and carry an empty
    part.
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


@router.post(
    "/{session_key}/attachments",
    response_model=CreateAttachmentResponse,
    status_code=201,
    summary="Attach a file to a chat session",
    response_description="The stored attachment",
)
async def create_attachment(
    request: Request,
    session_key: str,
    file: UploadFile = File(..., description="The file itself."),
    declared_name: str = Form(
        "",
        max_length=_DECLARED_NAME_MAX_LENGTH,
        description=(
            "What to call this file. Normalized server-side and never stored "
            "verbatim. Falls back to the part's own filename when omitted."
        ),
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_ATTACHMENTS_WRITE)
    ),
) -> CreateAttachmentResponse:
    """Store one file against one session. Nothing is delivered to a model here.

    The refusals, in the order they are made:

    * **400** - no ``X-Requested-With``, or no bytes.
    * **403** - somebody else's session, a dispatch task's session, or a session
      a provider that resolves callers left unattributed.
    * **404** - no such session in this namespace.
    * **413** - past the byte cap, past a session or namespace quota, or no
      ``Content-Length``. The byte cap is refused in middleware, before the
      body is buffered and therefore before any of the rest of this list.
    * **415** - the contents are not a type this deployment accepts. Both the
      declared type and the sniffed one are named, because the declared type
      decides nothing and a caller cannot act on being told only that.
    * **429** - this credential has uploaded too many files this minute.

    Uploading the same bytes to the same session twice returns the existing
    attachment with ``deduplicated: true`` rather than a conflict. That is what
    someone who pressed the button twice, or whose connection dropped after the
    write, actually wants.
    """
    _require_attachments_enabled()

    if not request.headers.get(_REQUESTED_WITH_HEADER):
        raise BadRequestError(
            error_code=ErrorCode.VALIDATION_ERROR,
            detail=f"This route requires the {_REQUESTED_WITH_HEADER} header.",
            hint=(
                "Send X-Requested-With: XMLHttpRequest. It forces a preflight, "
                "which is what keeps a cross-origin HTML form from posting a "
                "file into your session."
            ),
        )

    session = await AgentSessionsService(db).get_row_or_404(
        namespace_key=principal.namespace_key, session_key=session_key
    )
    caller_hash = hash_caller_id(principal.caller_id)
    authorize_attachment_write(
        session, caller_hash=caller_hash, is_admin=principal.is_admin
    )

    data = await _read_capped(file)
    row, deduplicated = await _service(db).create(
        namespace_key=principal.namespace_key,
        session=session,
        caller_hash=caller_hash,
        declared_name=declared_name or file.filename or "",
        declared_mime=file.content_type or "",
        data=data,
    )
    attachment = to_wire(row, session_key=session_key)
    attachment_id = row.id
    source_sha256 = row.source_sha256
    sniffed_mime = row.sniffed_mime
    await db.commit()

    # Reading the file is queued here and never waited for. Conversion is tens
    # of seconds on this deployment's corpus and an upload that blocked on it
    # would hold a connection and a browser for the length of an OCR run;
    # starting it now is what makes the text usually present by the time
    # somebody presses send. A turn that arrives first says so and schedules it
    # again, so nothing here is load-bearing.
    schedule_conversion(
        namespace_key=principal.namespace_key,
        attachment_id=attachment_id,
        source_sha256=source_sha256,
        declared_mime=sniffed_mime,
    )
    return CreateAttachmentResponse(attachment=attachment, deduplicated=deduplicated)


@router.get(
    "/{session_key}/attachments",
    response_model=ListAttachmentsResponse,
    summary="List a session's attachments",
    response_description="Every attachment on this session",
)
async def list_attachments(
    session_key: str,
    status: AttachmentStatus | None = Query(None, description="Optional status filter."),
    origin: AttachmentOrigin | None = Query(
        None,
        description=(
            "Optional origin filter. ``linear`` is what answers 'what did the "
            "tracker put in this conversation' in one query."
        ),
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_SESSION_CONTENT_READ)
    ),
) -> ListAttachmentsResponse:
    """List this session's attachments, oldest first. No bytes in the response."""
    _require_attachments_enabled()
    session = await _session_for_read(
        db,
        namespace_key=principal.namespace_key,
        session_key=session_key,
        principal=principal,
    )
    return await _service(db).list_for_session(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        session_id=session.id,
        status=status,
        origin=origin,
    )


@router.get(
    "/{session_key}/attachments/{attachment_key}",
    response_model=GetAttachmentResponse,
    summary="Read one attachment's metadata",
    response_description="The requested attachment",
)
async def get_attachment(
    session_key: str,
    attachment_key: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_SESSION_CONTENT_READ)
    ),
) -> GetAttachmentResponse:
    """Read one attachment. A key from another session is a 404, as is an unknown one."""
    _require_attachments_enabled()
    session = await _session_for_read(
        db,
        namespace_key=principal.namespace_key,
        session_key=session_key,
        principal=principal,
    )
    row = await _service(db).get_or_404(
        namespace_key=principal.namespace_key,
        session_id=session.id,
        attachment_key=attachment_key,
    )
    return GetAttachmentResponse(attachment=to_wire(row, session_key=session_key))


@router.get(
    "/{session_key}/attachments/{attachment_key}/content",
    summary="Download an attachment's bytes",
    response_description="The bytes, as a forced download",
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_attachment(
    session_key: str,
    attachment_key: str,
    variant: AttachmentVariant = Query(
        AttachmentVariant.ORIGINAL,
        description="Which artifact to download. Only ``original`` exists today.",
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_SESSION_CONTENT_READ)
    ),
) -> Response:
    """Return the bytes as an attachment, never as something a browser renders.

    A tombstoned attachment answers 410 with a written notice rather than 404.
    The file is not missing and the link is not broken: its bytes were reclaimed
    on a schedule, which is a different thing to say and the only one that
    stops somebody hunting for a bug.

    **The body is materialized, not streamed**, and the cost is stated here
    rather than left to be discovered under load: each concurrent download holds
    up to ``attachment_max_bytes`` resident in this process. Chunking it means a
    server-side cursor over ``substring(data ...)`` behind a
    ``StreamingResponse``, which is a change to the blob store's interface and
    not to this handler, so it is named as a limit rather than half-done here.
    """
    _require_attachments_enabled()
    session = await _session_for_read(
        db,
        namespace_key=principal.namespace_key,
        session_key=session_key,
        principal=principal,
    )
    service = _service(db)
    row = await service.get_or_404(
        namespace_key=principal.namespace_key,
        session_id=session.id,
        attachment_key=attachment_key,
    )
    if row.status == AttachmentStatus.TOMBSTONED.value:
        raise APIError(
            status_code=410,
            error_code=ErrorCode.ATTACHMENT_NOT_FOUND,
            reason=ErrorReason.NOT_FOUND,
            detail=TOMBSTONE_NOTICE,
            hint="Upload the file again if a turn still needs it.",
        )
    blob = await service.open_variant(
        namespace_key=principal.namespace_key,
        attachment_id=row.id,
        variant=variant,
    )
    if blob is None:
        raise NotFoundError(
            error_code=ErrorCode.ATTACHMENT_NOT_FOUND,
            detail=f"This attachment has no '{variant.value}' artifact.",
            resource="Attachment",
            resource_id=attachment_key,
            hint="Download the 'original' variant.",
        )
    return Response(
        content=blob.data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(row.display_name),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


def _content_disposition(display_name: str) -> str:
    """Build a header that survives a hostile filename.

    Two forms, per RFC 6266: a quoted ASCII fallback for old clients and a
    percent-encoded ``filename*`` for everything else. ``quote`` with an empty
    safe set is what keeps a quote, a semicolon or a CRLF in the name from
    ending the parameter and starting a header of the attacker's choosing.
    Normalization already removed those characters on the way in; this is the
    second of the two places, because a header injection reachable through a
    single normalization bug is not a risk worth carrying for one line of code.
    """
    # ``str.isalnum`` is Unicode-aware, so an accented letter passes it and then
    # fails to encode: headers are latin-1 and the response never leaves the
    # process. The ASCII test is the load-bearing half of this condition.
    ascii_fallback = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "._- ") else "_"
        for ch in display_name
    ) or "attachment"
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(display_name, safe='')}"
    )


@router.delete(
    "/{session_key}/attachments/{attachment_key}",
    response_model=DeleteAttachmentResponse,
    summary="Delete an attachment's bytes",
    response_description="Deletion confirmation",
)
async def delete_attachment(
    session_key: str,
    attachment_key: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_ATTACHMENTS_WRITE)
    ),
) -> DeleteAttachmentResponse:
    """Remove the bytes and keep the record.

    A 409 while the attachment is bound to the turn currently in flight: the
    model is reading it, and deleting it out from under a running invocation
    would leave a transcript nothing can explain.

    Otherwise every blob goes and the row is tombstoned, retaining name, hashes,
    size and origin so the conversation can still be audited. The response says
    plainly what this did not do: the executor's own copy of the conversation is
    removed only by deleting the session, and a model that already read the file
    has already read it.
    """
    _require_attachments_enabled()
    session = await AgentSessionsService(db).get_row_or_404(
        namespace_key=principal.namespace_key, session_key=session_key
    )
    authorize_attachment_write(
        session,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
    )
    service = _service(db)
    row = await service.get_or_404(
        namespace_key=principal.namespace_key,
        session_id=session.id,
        attachment_key=attachment_key,
    )
    if session.in_flight_trace_id is not None:
        bound = (
            await db.execute(
                select(AgentTurnAttachment.attachment_id).where(
                    AgentTurnAttachment.namespace_key == principal.namespace_key,
                    AgentTurnAttachment.session_id == session.id,
                    AgentTurnAttachment.trace_id == session.in_flight_trace_id,
                    AgentTurnAttachment.attachment_id == row.id,
                )
            )
        ).first()
        if bound is not None:
            raise APIError(
                status_code=409,
                error_code=ErrorCode.TURN_IN_FLIGHT,
                reason=ErrorReason.CONFLICT,
                detail="This attachment is being read by the turn currently running.",
                hint="Wait for the turn to finish, or stop it, then delete the file.",
            )
    await service.tombstone(row=row)
    await db.commit()
    return DeleteAttachmentResponse(
        deleted=True,
        attachment_key=attachment_key,
        notice=DELETE_NOTICE,
    )
