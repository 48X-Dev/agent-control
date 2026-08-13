"""Pushing a step's deliverables to the tracker, and recording that it happened.

Runs at delivery rather than at enqueue, and the ordering is the whole design.
A pointer line describes an upload, so composing it in the step's transaction
would describe one that has not been attempted. Adding it after the control
evaluated the body would mean the control passed on text nobody posts. So the
upload happens first, the body is recomposed with the pointers and persisted,
and only then is that body evaluated and sent: what a control sees is what the
tracker gets.

``linear_asset_url`` is written here and nowhere else. Until it is set the row
is the only copy of the file and the blob sweep steps over it; once the tracker
holds a copy it is a cache like every other row and the TTL resumes.
"""

from __future__ import annotations

import datetime as dt
import logging

from agent_control_models.attachments import (
    AgentOutputKind,
    AttachmentOrigin,
    AttachmentStatus,
    AttachmentVariant,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import executor_settings
from ..models import AgentSession, AgentSessionAttachment
from .agent_attachments import AgentAttachmentsService
from .attachment_blobs import get_attachment_blob_store
from .linear_writeback_files import AgentFile, AgentFileDelivery, render_file_lines
from .linear_writeback_runtime import WritebackRuntime

_logger = logging.getLogger(__name__)


async def push_step_files(
    db: AsyncSession,
    runtime: WritebackRuntime,
    *,
    namespace_key: str,
    session_key: str | None,
    issue_id: str,
    agent_name: str,
    step_index: int,
) -> list[str]:
    """Deliver this step's finals to the issue and return the comment's pointers.

    An empty list means there was nothing to send, which is the ordinary case
    and must leave the queued body exactly as the step composed it.
    """
    rows = await _final_files(db, namespace_key=namespace_key, session_key=session_key)
    if not rows:
        return []

    deliveries: list[AgentFileDelivery] = []
    for row in rows:
        content = await _blob_bytes(db, namespace_key=namespace_key, row=row)
        if content is None:
            _logger.warning("An agent file had no bytes to deliver; skipping it.")
            continue
        delivery = await runtime.deliver_agent_file(
            issue_id=issue_id,
            file=AgentFile(
                title=row.display_name,
                filename=row.display_name,
                content_type=row.delivered_mime or row.sniffed_mime,
                content=content,
                subtitle=f"{agent_name}, step {step_index + 1}",
                asset_url=row.linear_asset_url,
            ),
        )
        if delivery.asset_url is not None and row.linear_asset_url != delivery.asset_url:
            row.linear_asset_url = delivery.asset_url
            row.updated_at = dt.datetime.now(dt.UTC)
        deliveries.append(delivery)

    await db.flush()
    return render_file_lines(deliveries)


async def _final_files(
    db: AsyncSession, *, namespace_key: str, session_key: str | None
) -> list[AgentSessionAttachment]:
    """This step's deliverables: agent-origin, final, still holding bytes.

    Keyed on the session because the dispatcher opens one session per step, so
    a session's finals are that step's finals. Drafts are excluded by the
    marker rather than by age: a draft never leaves this system.
    """
    if not session_key:
        return []
    stmt = (
        select(AgentSessionAttachment)
        .join(AgentSession, AgentSession.id == AgentSessionAttachment.session_id)
        .where(
            AgentSession.namespace_key == namespace_key,
            AgentSession.session_key == session_key,
            AgentSessionAttachment.origin == AttachmentOrigin.AGENT.value,
            AgentSessionAttachment.agent_output_kind == AgentOutputKind.FINAL.value,
            AgentSessionAttachment.status != AttachmentStatus.TOMBSTONED.value,
        )
        .order_by(AgentSessionAttachment.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _blob_bytes(
    db: AsyncSession, *, namespace_key: str, row: AgentSessionAttachment
) -> bytes | None:
    service = AgentAttachmentsService(
        db, settings=executor_settings, blobs=get_attachment_blob_store()
    )
    blob = await service.open_variant(
        namespace_key=namespace_key,
        attachment_id=row.id,
        variant=AttachmentVariant.ORIGINAL,
    )
    return None if blob is None else blob.data
