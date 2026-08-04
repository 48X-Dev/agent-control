"""Every gate an upload has to clear, and the refusal each one raises.

Split from :mod:`services.agent_attachments` for the same reason
:mod:`services.attachment_access` was: storing a file and deciding whether this
one may be stored are two questions, and a reviewer checking the arithmetic of a
quota should not have to read past an insert-conflict recovery to reach it. The
module that stores keeps storing and counting; the module here only refuses.

**What these ceilings bound, and what they do not.** They bound *stored* bytes
and *accepted* uploads. They do not bound bytes buffered by a request that is
then refused, and nothing here can: FastAPI parses the multipart body during
routing, spooling past a megabyte to a temp file, so by the time any function in
this module runs the body is already on disk. The only control that runs earlier
is :class:`~..middleware.AttachmentUploadBodyLimit`, which caps a single body and
applies no per-caller rate. So a caller who is over their per-minute allowance
still costs one ``attachment_max_bytes`` write to the temp filesystem per
request. That is the honest limit of a rate check that lives in a service.

Two of these ceilings count bytes that still exist, and tombstoned rows hold
none. Charging a namespace for bytes it no longer stores would make its ceiling
unreachable-by-deletion, and deletion is the one remedy an operator has.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import NoReturn

from agent_control_models.attachments import AttachmentStatus
from agent_control_models.errors import ErrorCode, ErrorReason
from agent_control_models.files import sniff_mime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import ExecutorSettings
from ..errors import APIError
from ..models import AgentSessionAttachment
from .attachment_converter_containers import refine_container_mime
from .attachment_quota import get_attachment_quota
from .executor_metrics import (
    ATTACHMENT_UPLOAD_QUOTA,
    ATTACHMENT_UPLOAD_RATE_LIMITED,
    ATTACHMENT_UPLOAD_REJECTED,
    ATTACHMENT_UPLOADS,
)


def require_accepted_type(
    *, settings: ExecutorSettings, declared_mime: str, data: bytes
) -> str:
    """Return the sniffed type, or refuse with both types named.

    The declared type decides nothing. A ``.pdf`` that is really a ZIP is
    refused as a ZIP, which is also what happens to every Office format, since
    OOXML and ODF are ZIP containers, so the sniff alone cannot tell a deck from
    an archive. ``refine_container_mime`` resolves the OOXML three structurally
    before this gate reads the type; anything still called ``application/zip``
    here is a container this deployment cannot open, and the message says to
    export a PDF rather than leaving the caller to work out why their file was
    called a zip.

    Plain text, markdown and CSV sniff as nothing at all, so they arrive here as
    ``unrecognized``. The hint names the remedy the plan names for them - paste
    the contents into the message - because a list of accepted types tells
    somebody holding a ``.md`` file what they cannot do and not what they can.
    """
    sniffed = refine_container_mime(data, sniff_mime(data))
    accepted = settings.attachment_accepted_mimes
    if sniffed is not None and sniffed in accepted:
        return sniffed
    ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_REJECTED).inc()
    named = sniffed or "unrecognized"
    if sniffed == "application/zip":
        hint = "Export it to PDF and attach that."
    else:
        hint = (
            f"This deployment accepts: {', '.join(sorted(accepted))}. If it is "
            "text, markdown or CSV, paste it into the message instead."
        )
    raise APIError(
        status_code=415,
        error_code=ErrorCode.ATTACHMENT_REJECTED,
        reason=ErrorReason.INVALID,
        detail=(
            f"This file's contents are {named}; the upload declared "
            f"{declared_mime or 'nothing'}. The contents decide."
        ),
        hint=hint,
    )


def refuse_quota(*, detail: str, hint: str) -> NoReturn:
    """The one 413 every storage ceiling raises."""
    ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_QUOTA).inc()
    raise APIError(
        status_code=413,
        error_code=ErrorCode.QUOTA_EXCEEDED,
        reason=ErrorReason.INVALID,
        detail=detail,
        hint=hint,
    )


def enforce_upload_rate(
    *, settings: ExecutorSettings, namespace_key: str, caller_hash: str | None
) -> None:
    """Refuse a credential that has uploaded too many files this minute."""
    quota = get_attachment_quota(
        max_per_minute=settings.attachment_uploads_per_minute
    )
    retry_after = quota.try_acquire(
        namespace_key=namespace_key, caller_hash=caller_hash
    )
    if retry_after is None:
        return
    ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_RATE_LIMITED).inc()
    raise APIError(
        status_code=429,
        error_code=ErrorCode.QUOTA_EXCEEDED,
        reason=ErrorReason.CONFLICT,
        detail=(
            f"This credential has uploaded "
            f"{settings.attachment_uploads_per_minute} attachments in "
            f"the last minute, which is its configured ceiling."
        ),
        hint=(
            f"Retry in about {retry_after:.0f} seconds, or raise "
            f"AGENT_CONTROL_EXECUTOR_ATTACHMENT_UPLOADS_PER_MINUTE."
        ),
        extra_details={"retry_after_seconds": math.ceil(retry_after)},
    )


async def enforce_storage_quotas(
    db: AsyncSession,
    *,
    settings: ExecutorSettings,
    namespace_key: str,
    session_id: int,
    incoming_bytes: int,
) -> None:
    """Refuse when this file would push a session or namespace over.

    Four ceilings, checked narrowest first so the message names the smallest
    thing the caller can act on: a session's file count, a session's bytes, the
    namespace's bytes, then the namespace's uploads this hour.
    """
    live = AgentSessionAttachment.status != AttachmentStatus.TOMBSTONED.value

    session_stats = (
        await db.execute(
            select(
                func.count(AgentSessionAttachment.id),
                func.coalesce(func.sum(AgentSessionAttachment.size_bytes), 0),
            ).where(
                AgentSessionAttachment.namespace_key == namespace_key,
                AgentSessionAttachment.session_id == session_id,
                live,
            )
        )
    ).one()
    session_count, session_bytes = int(session_stats[0]), int(session_stats[1])

    if session_count >= settings.attachment_max_per_session:
        refuse_quota(
            detail=(
                f"This session already holds {session_count} attachments, "
                f"which is its configured ceiling."
            ),
            hint=(
                "Delete one you are finished with, or raise "
                "AGENT_CONTROL_EXECUTOR_ATTACHMENT_MAX_PER_SESSION."
            ),
        )
    if session_bytes + incoming_bytes > settings.attachment_session_total_bytes:
        refuse_quota(
            detail=(
                f"This session holds {session_bytes} bytes of attachments and "
                f"this file adds {incoming_bytes}, past its ceiling of "
                f"{settings.attachment_session_total_bytes}."
            ),
            hint=(
                "Delete an attachment, or raise "
                "AGENT_CONTROL_EXECUTOR_ATTACHMENT_SESSION_TOTAL_BYTES."
            ),
        )

    namespace_bytes = int(
        (
            await db.execute(
                select(
                    func.coalesce(func.sum(AgentSessionAttachment.size_bytes), 0)
                ).where(AgentSessionAttachment.namespace_key == namespace_key, live)
            )
        ).scalar()
        or 0
    )
    if namespace_bytes + incoming_bytes > settings.attachment_namespace_total_bytes:
        refuse_quota(
            detail=(
                f"This namespace stores {namespace_bytes} bytes of attachments "
                f"and this file adds {incoming_bytes}, past its ceiling of "
                f"{settings.attachment_namespace_total_bytes}."
            ),
            hint=(
                "This upload already ran the sweep that reclaims bytes "
                f"{settings.attachment_blob_ttl_days} days after a file's "
                "last turn, and there was not enough to reclaim. Delete "
                "attachments, or raise "
                "AGENT_CONTROL_EXECUTOR_ATTACHMENT_NAMESPACE_TOTAL_BYTES."
            ),
        )

    hourly = int(
        (
            await db.execute(
                select(func.count(AgentSessionAttachment.id)).where(
                    AgentSessionAttachment.namespace_key == namespace_key,
                    AgentSessionAttachment.created_at
                    > dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                )
            )
        ).scalar()
        or 0
    )
    if hourly >= settings.attachment_uploads_per_namespace_hour:
        refuse_quota(
            detail=(
                f"This namespace has accepted {hourly} attachments in the last "
                f"hour, which is its configured ceiling."
            ),
            hint=(
                "Wait for the window to roll, or raise "
                "AGENT_CONTROL_EXECUTOR_ATTACHMENT_UPLOADS_PER_NAMESPACE_HOUR."
            ),
        )
