"""Turning the keys a turn named into files it can carry, and recording both.

Two short database steps around a long executor call, and the split is the
point. :func:`load_for_turn` runs before anything leaves the process and reads
everything the delivery renderer needs into plain values, so no pooled
connection is held while the model thinks. :func:`record_bindings` runs after,
writing what the turn actually carried.

**A named key that is not deliverable is a refusal, never a silent drop.** The
operator is standing in front of the composer: telling them the file will not
be sent, before the model call is paid for, is strictly better than a turn that
ran without it and an agent that answered from the filename. That asymmetry
with the dispatch path is deliberate and section 3.10 of the plan argues it.

**Conversion is scheduled here and never awaited.** A file whose text is not in
the cache yet is delivered as a named line saying so, and the same call that
notices puts the work in the background queue - so the second turn carrying that
file has the text, and the first one never waits twenty seconds for OCR behind
a request the operator is watching.
"""

from __future__ import annotations

import logging

from agent_control_models.attachments import (
    AttachmentStatus,
    TurnAttachmentVerdict,
)
from agent_control_models.errors import ErrorCode, ErrorReason
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import ExecutorSettings
from ..errors import APIError, ConflictError, NotFoundError
from ..models import AgentSessionAttachment, AgentTurnAttachment
from .attachment_conversions import read_cached, schedule_conversion
from .attachment_delivery import DeliverableAttachment

_logger = logging.getLogger(__name__)


def unique_keys(attachment_keys: list[str]) -> list[str]:
    """The same keys with repeats removed, first mention deciding the order.

    A key named twice is one file, and delivering it twice would put the same
    document in front of the model under a count line saying two files - the
    one sentence this whole path asks an agent to rely on, stating something
    that is not true. It would also pay for the text twice inside a budget that
    is already turning genuinely different files away.

    Silently rather than as a 422, because "specification.pdf, specification.pdf
    and the screenshot" is an obvious intent and there is nothing to tell the
    caller that they would not already know. The ledger agrees with the message
    either way: :func:`record_bindings` writes one row per distinct key, so
    without this it would record one file for a message that carried two.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for key in attachment_keys:
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


async def load_for_turn(
    db: AsyncSession,
    *,
    namespace_key: str,
    session_id: int,
    attachment_keys: list[str],
    settings: ExecutorSettings,
) -> list[DeliverableAttachment]:
    """Resolve the keys this turn named, in the order the caller gave them.

    The caller's order is kept rather than the table's, because a person who
    attached a specification and then a screenshot meant the model to read them
    in that order. Repeats are the caller's to remove, with :func:`unique_keys`,
    before either this or :func:`record_bindings` sees them: deduplicating in
    only one of the two is how a message that carried a file twice gets recorded
    as having carried it once.

    Refuses on: a key this session does not own (404), a key whose attachment is
    not ``ready`` (409), and a set whose bytes exceed the per-turn ceiling
    (413). The byte ceiling is checked here and not at upload because it bounds
    resident memory during one delivery, which is a different question from how
    much a session may store.
    """
    if not attachment_keys:
        return []

    rows = await _rows_for(
        db,
        namespace_key=namespace_key,
        session_id=session_id,
        attachment_keys=attachment_keys,
    )
    ordered = [_require(rows, key) for key in attachment_keys]
    _require_turn_bytes(ordered, settings=settings)

    deliverables: list[DeliverableAttachment] = []
    for row in ordered:
        cached = await read_cached(db, namespace_key=namespace_key, source_sha256=row.source_sha256)
        if cached is None or not cached.is_finished:
            schedule_conversion(
                namespace_key=namespace_key,
                attachment_id=row.id,
                source_sha256=row.source_sha256,
                declared_mime=row.sniffed_mime,
            )
        deliverables.append(
            DeliverableAttachment(
                attachment_key=row.attachment_key,
                display_name=row.display_name,
                sniffed_mime=row.sniffed_mime,
                size_bytes=row.size_bytes,
                conversion=cached,
            )
        )
    return deliverables


async def record_bindings(
    db: AsyncSession,
    *,
    namespace_key: str,
    session_id: int,
    trace_id: str,
    attachment_keys: list[str],
    included_keys: tuple[str, ...],
) -> None:
    """Write what this turn carried, and what happened to each file.

    Idempotent by the composite primary key, so a retry writes the same rows
    rather than a second set. The verdict is ``sent`` only for the files whose
    contents actually went; a file that was named to the model but whose text
    was not included is recorded ``blocked`` with the reason, because "the model
    was told this file exists" and "the model read this file" are different
    facts and a transcript that conflated them would be answering the wrong
    question a year later.
    """
    if not attachment_keys:
        return
    rows = await _rows_for(
        db,
        namespace_key=namespace_key,
        session_id=session_id,
        attachment_keys=attachment_keys,
    )
    included = set(included_keys)
    values = []
    for position, key in enumerate(attachment_keys):
        row = rows.get(key)
        if row is None:
            continue
        sent = key in included
        values.append(
            {
                "namespace_key": namespace_key,
                "session_id": session_id,
                "trace_id": trace_id,
                "attachment_id": row.id,
                "position": position,
                "verdict": (
                    TurnAttachmentVerdict.SENT.value
                    if sent
                    else TurnAttachmentVerdict.BLOCKED.value
                ),
                "blocked_reason": None if sent else _NOT_INCLUDED_REASON,
            }
        )
    if not values:
        return
    stmt = pg_insert(AgentTurnAttachment).values(values)
    await db.execute(
        stmt.on_conflict_do_nothing(
            index_elements=["namespace_key", "session_id", "trace_id", "attachment_id"]
        )
    )


_NOT_INCLUDED_REASON = "Named to the agent; its contents were not included."


async def _rows_for(
    db: AsyncSession,
    *,
    namespace_key: str,
    session_id: int,
    attachment_keys: list[str],
) -> dict[str, AgentSessionAttachment]:
    stmt = select(AgentSessionAttachment).where(
        AgentSessionAttachment.namespace_key == namespace_key,
        AgentSessionAttachment.session_id == session_id,
        AgentSessionAttachment.attachment_key.in_(attachment_keys),
    )
    return {row.attachment_key: row for row in (await db.execute(stmt)).scalars().all()}


def _require(rows: dict[str, AgentSessionAttachment], key: str) -> AgentSessionAttachment:
    row = rows.get(key)
    if row is None:
        raise NotFoundError(
            error_code=ErrorCode.ATTACHMENT_NOT_FOUND,
            detail=f"Attachment '{key}' is not on this session.",
            resource="Attachment",
            resource_id=key,
            hint="Attach the file to this session before naming it on a turn.",
        )
    if row.status != AttachmentStatus.READY.value:
        raise ConflictError(
            error_code=ErrorCode.ATTACHMENT_NOT_READY,
            detail=(
                f"Attachment '{key}' is '{row.status}' and cannot be sent. "
                "Nothing was sent to the agent."
            ),
            resource="Attachment",
            resource_id=key,
            hint=(
                "A tombstoned or failed attachment has to be uploaded again. "
                "Remove it from the message to send the rest."
            ),
        )
    return row


def _require_turn_bytes(rows: list[AgentSessionAttachment], *, settings: ExecutorSettings) -> None:
    total = sum(row.size_bytes for row in rows)
    if total <= settings.attachment_turn_total_bytes:
        return
    raise APIError(
        status_code=413,
        error_code=ErrorCode.ATTACHMENT_TOO_LARGE,
        reason=ErrorReason.INVALID,
        detail=(
            f"These {len(rows)} files are {total} bytes together and one turn "
            f"carries at most {settings.attachment_turn_total_bytes}."
        ),
        hint="Send fewer files with this message.",
    )
