"""Files attached to a session: storing them, and reading them back.

This module never opens a document. The only thing it reads out of an uploaded
file is its first sixteen bytes, and the only decision that follows is whether
the type is one this deployment accepts. Anything that needs a parser belongs in
a process with its own memory limit and no database credentials.

**Who may attach a file lives next door**, in
:mod:`services.attachment_access`, because it is one question a reviewer will
want to read on its own and because it is the part that has to be right under
two auth providers that disagree about whether callers exist at all. **The
ceilings live next door too**, in :mod:`services.attachment_limits`, on the same
argument: this module stores and counts, and that one only refuses.

**Quotas are checked after retention runs, not before.** A namespace sitting at
its ceiling with a fortnight of reclaimable bytes should clear itself when
somebody pushes on it. A sweep after the check would only ever help the next
caller. The sweep commits in a transaction of its own so that a refusal after it
cannot undo it.

**Two writers can reach the same constraint at the same time**, so the insert is
wrapped rather than guarded. A ``SELECT`` then ``INSERT`` cannot make identical
bytes idempotent on its own: two uploads of one file both find nothing and both
insert. The unique constraint is what actually decides, and catching its
violation is how the loser gets the winner's key instead of a 500.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import cast
from uuid import uuid4

from agent_control_models.attachments import (
    Attachment,
    AttachmentOrigin,
    AttachmentStatus,
    AttachmentVariant,
    ListAttachmentsResponse,
)
from agent_control_models.errors import ErrorCode
from agent_control_models.files import is_mime_mismatch, normalize_display_name
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import ExecutorSettings
from ..errors import NotFoundError
from ..models import AgentSession, AgentSessionAttachment
from .attachment_blobs import AttachmentBlobStore, StoredBlob
from .attachment_limits import (
    enforce_storage_quotas,
    enforce_upload_rate,
    require_accepted_type,
)
from .attachment_retention import run_attachment_retention_committed
from .executor_metrics import (
    ATTACHMENT_UPLOAD_ACCEPTED,
    ATTACHMENT_UPLOAD_DEDUPLICATED,
    ATTACHMENT_UPLOAD_REJECTED,
    ATTACHMENT_UPLOADS,
)

UNNAMED_ATTACHMENT = "attachment"
"""Used when normalization leaves nothing renderable. A file whose entire name
was bidi overrides gets a boring one rather than an empty chip."""

TOMBSTONE_NOTICE = (
    "The bytes of this attachment were reclaimed by this server's retention "
    "policy. Its name, size and hashes are kept so the conversation can still "
    "be audited."
)

DELETE_NOTICE = (
    "Removed from Agent Control and from future turns. A model that already "
    "read this file has already read it, and the executor keeps its own copy "
    "of the conversation until the session itself is deleted."
)

CONTENT_UNIQUE_CONSTRAINT = "uq_agent_session_attachments_content"
SESSION_FOREIGN_KEY = "agent_session_attachments_session_fkey"


def _constraint_name(error: IntegrityError) -> str | None:
    """Name the constraint an ``IntegrityError`` came from.

    ``diag`` first because it is exact, the message text second because the
    driver does not always populate it. This is the shape ``endpoints/controls``
    already uses; discriminating on the wrong constraint would map a foreign key
    violation to a dedupe hit and return somebody else's attachment key.
    """
    named = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if named:
        return str(named)
    text = " ".join(part for part in (str(error.orig), str(error)) if part)
    for candidate in (CONTENT_UNIQUE_CONSTRAINT, SESSION_FOREIGN_KEY):
        if candidate in text:
            return candidate
    return None


class AgentAttachmentsService:
    """Reads and writes the attachment tables. Never calls an executor."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        settings: ExecutorSettings,
        blobs: AttachmentBlobStore,
    ) -> None:
        self._db = db
        self._settings = settings
        self._blobs = blobs

    async def get_or_404(
        self, *, namespace_key: str, session_id: int, attachment_key: str
    ) -> AgentSessionAttachment:
        """Load one attachment inside one session, or raise 404.

        Scoped by ``session_id`` as well as by key, so an attachment key from
        another session in the same namespace is a 404 rather than a read. The
        session was resolved and authorized before this ran; the attachment
        must belong to the session that was.
        """
        stmt = select(AgentSessionAttachment).where(
            AgentSessionAttachment.namespace_key == namespace_key,
            AgentSessionAttachment.session_id == session_id,
            AgentSessionAttachment.attachment_key == attachment_key,
        )
        row = cast(
            AgentSessionAttachment | None, (await self._db.execute(stmt)).scalars().first()
        )
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.ATTACHMENT_NOT_FOUND,
                detail=f"Attachment '{attachment_key}' not found on this session.",
                resource="Attachment",
                resource_id=attachment_key,
                hint="Verify the key, and that it belongs to this session.",
            )
        return row

    async def list_for_session(
        self,
        *,
        namespace_key: str,
        session_key: str,
        session_id: int,
        status: AttachmentStatus | None = None,
        origin: AttachmentOrigin | None = None,
    ) -> ListAttachmentsResponse:
        """List one session's attachments, oldest first."""
        stmt = (
            select(AgentSessionAttachment)
            .where(
                AgentSessionAttachment.namespace_key == namespace_key,
                AgentSessionAttachment.session_id == session_id,
            )
            .order_by(AgentSessionAttachment.created_at, AgentSessionAttachment.id)
        )
        if status is not None:
            stmt = stmt.where(AgentSessionAttachment.status == status.value)
        if origin is not None:
            stmt = stmt.where(AgentSessionAttachment.origin == origin.value)
        rows = list((await self._db.execute(stmt)).scalars().all())
        live_bytes = sum(
            row.size_bytes
            for row in rows
            if row.status != AttachmentStatus.TOMBSTONED.value
        )
        return ListAttachmentsResponse(
            attachments=[to_wire(row, session_key=session_key) for row in rows],
            count=len(rows),
            total_bytes=live_bytes,
        )

    async def create(
        self,
        *,
        namespace_key: str,
        session: AgentSession,
        caller_hash: str | None,
        declared_name: str,
        declared_mime: str,
        data: bytes,
        origin: AttachmentOrigin = AttachmentOrigin.OPERATOR_UPLOAD,
        origin_ref: str | None = None,
    ) -> tuple[AgentSessionAttachment, bool]:
        """Store one file against one session. Returns ``(row, deduplicated)``.

        Order of refusals, cheapest and most specific first: the rate limiter,
        then the type gate, then the storage quotas, and only then is anything
        written. The type gate runs before the quotas because a refused type
        should say so whatever the namespace's byte total happens to be.

        The metadata row and the blob are written in one transaction, metadata
        first, so a session deleted mid-flight either loses the race (the
        cascade takes both) or wins it (the foreign key refuses, and the caller
        gets a 404 rather than an orphaned blob).

        **A row that no longer stands for usable bytes never satisfies a
        dedupe.** A tombstone's bytes are gone, so returning it would answer 201
        to an upload that stored nothing, and the 410 on a tombstoned download
        tells the caller to re-upload precisely so this path runs. ``rejected``
        and ``failed`` are the same answer for a different reason: retry after a
        conversion failure is a fresh upload, not a pointer at the row that
        failed. All three are resurrected instead: same key, same
        ``created_at``, bytes back, status ``ready``. Minting a second row would
        break the audit chain the tombstone exists to keep.
        """
        enforce_upload_rate(
            settings=self._settings,
            namespace_key=namespace_key,
            caller_hash=caller_hash,
        )

        source_sha = hashlib.sha256(data).hexdigest()
        existing = await self._find_by_content(
            namespace_key=namespace_key,
            session_id=session.id,
            source_sha256=source_sha,
        )
        if existing is not None and _holds_bytes(existing):
            # Uploading the same file twice is a user action with an obvious
            # intent, so it is not a conflict. Returning the existing key also
            # means a retried upload after a dropped connection is idempotent.
            ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_DEDUPLICATED).inc()
            return existing, True

        sniffed = require_accepted_type(
            settings=self._settings, declared_mime=declared_mime, data=data
        )

        # In a transaction of its own, so that a quota refusal below cannot roll
        # back reclamation this upload's own pressure triggered. A namespace at
        # its ceiling clears itself even when the upload that pushed on it is
        # the one that gets refused.
        await run_attachment_retention_committed(
            namespace_key=namespace_key,
            orphan_ttl_hours=self._settings.attachment_orphan_ttl_hours,
            blob_ttl_days=self._settings.attachment_blob_ttl_days,
        )
        await enforce_storage_quotas(
            self._db,
            settings=self._settings,
            namespace_key=namespace_key,
            session_id=session.id,
            incoming_bytes=len(data),
        )

        if existing is not None:
            return await self._resurrect(existing, sniffed=sniffed, data=data), False

        display_name, was_normalized = normalize_display_name(declared_name)
        row = AgentSessionAttachment(
            namespace_key=namespace_key,
            session_id=session.id,
            attachment_key=uuid4().hex,
            display_name=display_name or UNNAMED_ATTACHMENT,
            display_name_normalized=was_normalized or display_name is None,
            original_name_sha256=hashlib.sha256(
                declared_name.encode("utf-8", "surrogatepass")
            ).hexdigest(),
            declared_mime=declared_mime[:128],
            sniffed_mime=sniffed,
            size_bytes=len(data),
            source_sha256=source_sha,
            # Nothing converts anything in this phase, so what was uploaded is
            # what would be delivered. Recorded rather than left null so the
            # delivery path's hash check has something to compare against.
            delivered_sha256=source_sha,
            delivered_mime=sniffed,
            delivered_size_bytes=len(data),
            status=AttachmentStatus.READY.value,
            origin=origin.value,
            origin_ref=origin_ref,
            created_by_hash=caller_hash,
        )
        try:
            # A savepoint, so that a constraint violation leaves the request's
            # transaction usable. Without one, the failed flush poisons the
            # session and the recovery below could not read anything.
            async with self._db.begin_nested():
                self._db.add(row)
                await self._db.flush()
                await self._blobs.put(
                    self._db,
                    namespace_key=namespace_key,
                    attachment_id=row.id,
                    variant=AttachmentVariant.ORIGINAL,
                    content_type=sniffed,
                    data=data,
                )
        except IntegrityError as exc:
            if row in self._db:
                self._db.expunge(row)
            return await self._resolve_write_conflict(
                exc,
                namespace_key=namespace_key,
                session=session,
                source_sha256=source_sha,
                sniffed=sniffed,
                data=data,
            )
        ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_ACCEPTED).inc()
        return row, False

    async def _resolve_write_conflict(
        self,
        exc: IntegrityError,
        *,
        namespace_key: str,
        session: AgentSession,
        source_sha256: str,
        sniffed: str,
        data: bytes,
    ) -> tuple[AgentSessionAttachment, bool]:
        """Turn a lost race into an answer, or re-raise it.

        Two races reach here and both are ordinary. Identical bytes uploaded
        twice at once: one insert wins, and the loser wants the winner's key
        rather than a 500. An upload against a session being deleted: the
        foreign key refuses, which is a 404 about the session and not a fault.
        Anything else is a defect and is left alone, because a bare ``except
        IntegrityError`` that answers 201 hides the next one.
        """
        constraint = _constraint_name(exc)

        if constraint == CONTENT_UNIQUE_CONSTRAINT:
            winner = await self._find_by_content(
                namespace_key=namespace_key,
                session_id=session.id,
                source_sha256=source_sha256,
            )
            if winner is not None and not _holds_bytes(winner):
                return await self._resurrect(winner, sniffed=sniffed, data=data), False
            if winner is not None:
                ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_DEDUPLICATED).inc()
                return winner, True

        if constraint == SESSION_FOREIGN_KEY:
            ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_REJECTED).inc()
            raise NotFoundError(
                error_code=ErrorCode.AGENT_SESSION_NOT_FOUND,
                detail="This session was deleted while the file was uploading.",
                resource="AgentSession",
                resource_id=session.session_key,
                hint="Open a new session and attach the file there.",
            ) from exc

        raise exc

    async def _resurrect(
        self, row: AgentSessionAttachment, *, sniffed: str, data: bytes
    ) -> AgentSessionAttachment:
        """Put bytes back behind a dead row, keeping the key and the history.

        The descriptive columns are not rewritten. They describe these same
        bytes - the sha256 is what matched - and leaving the original name
        keeps this consistent with the dedupe path, where the first name also
        wins. What changes is the status, the bytes and ``updated_at``.

        ``updated_at`` is not bookkeeping here. The orphan sweep reads the later
        of it and ``created_at``, so stamping it is what keeps a row whose bytes
        arrived a second ago from being deleted whole as a fortnight-old orphan
        by the next upload into the namespace.
        """
        await self._blobs.delete(
            self._db,
            namespace_key=row.namespace_key,
            attachment_id=row.id,
            variant=AttachmentVariant.ORIGINAL,
        )
        await self._blobs.put(
            self._db,
            namespace_key=row.namespace_key,
            attachment_id=row.id,
            variant=AttachmentVariant.ORIGINAL,
            content_type=sniffed,
            data=data,
        )
        row.status = AttachmentStatus.READY.value
        row.failure_code = None
        row.updated_at = dt.datetime.now(dt.UTC)
        await self._db.flush()
        ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_ACCEPTED).inc()
        return row

    async def open_variant(
        self, *, namespace_key: str, attachment_id: int, variant: AttachmentVariant
    ) -> StoredBlob | None:
        return await self._blobs.open(
            self._db,
            namespace_key=namespace_key,
            attachment_id=attachment_id,
            variant=variant,
        )

    async def tombstone(self, *, row: AgentSessionAttachment) -> None:
        """Delete the bytes and keep the record.

        Not a soft delete. Every blob goes; the metadata row survives carrying
        name, hashes, size and origin, because a transcript that can no longer
        say which documents a conversation saw is worse than one that says
        "this one, and its bytes are gone".
        """
        await self._blobs.delete(
            self._db, namespace_key=row.namespace_key, attachment_id=row.id
        )
        row.status = AttachmentStatus.TOMBSTONED.value
        row.updated_at = dt.datetime.now(dt.UTC)
        await self._db.flush()

    async def _find_by_content(
        self, *, namespace_key: str, session_id: int, source_sha256: str
    ) -> AgentSessionAttachment | None:
        stmt = select(AgentSessionAttachment).where(
            AgentSessionAttachment.namespace_key == namespace_key,
            AgentSessionAttachment.session_id == session_id,
            AgentSessionAttachment.source_sha256 == source_sha256,
        )
        return cast(
            AgentSessionAttachment | None,
            (await self._db.execute(stmt)).scalars().first(),
        )


_USABLE_STATUSES = frozenset(
    {
        AttachmentStatus.PENDING.value,
        AttachmentStatus.CONVERTING.value,
        AttachmentStatus.READY.value,
    }
)
"""Statuses whose row still stands for bytes a caller can use.

A tombstone has none: they were reclaimed. ``rejected`` and ``failed`` are the
converter's two dead ends, and section 9 of the plan says retry after a
conversion failure is a fresh upload. Answering 201 ``deduplicated`` with a key
pointing at either of those would hand back a row that will never convert and
call it success. Nothing in this phase writes them - the converter is a later
one - which is exactly why the rule belongs here now rather than after the
first bug report.
"""


def _holds_bytes(row: AgentSessionAttachment) -> bool:
    """Whether this row can answer an upload of the same content as-is."""
    return row.status in _USABLE_STATUSES


def to_wire(row: AgentSessionAttachment, *, session_key: str) -> Attachment:
    """Build the response model. ``created_by_hash`` is never serialized."""
    return Attachment(
        attachment_key=row.attachment_key,
        session_key=session_key,
        display_name=row.display_name,
        display_name_normalized=row.display_name_normalized,
        declared_mime=row.declared_mime,
        sniffed_mime=row.sniffed_mime,
        mime_mismatch=is_mime_mismatch(row.declared_mime, row.sniffed_mime),
        size_bytes=row.size_bytes,
        source_sha256=row.source_sha256,
        delivered_sha256=row.delivered_sha256,
        delivered_mime=row.delivered_mime,
        delivered_size_bytes=row.delivered_size_bytes,
        status=AttachmentStatus(row.status),
        failure_code=row.failure_code,
        page_count=row.page_count,
        estimated_tokens=row.estimated_tokens,
        converted_from=row.converted_from,
        origin=AttachmentOrigin(row.origin),
        origin_ref=row.origin_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
