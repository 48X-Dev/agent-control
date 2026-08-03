"""Where an attachment's bytes live, behind a seam rather than an abstraction.

``AttachmentBlobStore`` is a Protocol with four methods and one implementation.
That is deliberate and it is the same call ``ExecutorClient`` makes: the seam
exists so that an object-store implementation is a new file rather than a
refactor, and it is not built now because nothing in this repository speaks to
an object store - no boto3, no google-cloud-storage, no MinIO in either compose
file. Adding one to the quick start is a bigger operational change than a
``bytea`` column when the per-file cap is twenty megabytes and the bytes are
reclaimed on a timer.

The cost of ``bytea``, stated rather than glossed: a large row TOASTs out of
line and lands in ``pg_dump`` and every base backup. That is why quotas ship in
the same phase as the table and why the blob TTL sweep is not optional.

Every method takes ``namespace_key`` and every statement filters on it. Not
because the caller might forget - because the composite foreign key means a
statement without it would be *valid SQL against another namespace's rows*.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, cast

from agent_control_models.attachments import AttachmentVariant
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AgentSessionAttachment, AgentSessionAttachmentBlob


@dataclass(frozen=True)
class StoredBlob:
    """One artifact's bytes and what they are, as read back out."""

    content_type: str
    size_bytes: int
    sha256: str
    data: bytes


class AttachmentBlobStore(Protocol):
    """The four operations anything storing attachment bytes has to support."""

    async def put(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        attachment_id: int,
        variant: AttachmentVariant,
        content_type: str,
        data: bytes,
    ) -> str:
        """Write one artifact and return its SHA-256."""
        ...

    async def open(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        attachment_id: int,
        variant: AttachmentVariant,
    ) -> StoredBlob | None:
        """Read one artifact back, or ``None`` when its bytes are gone."""
        ...

    async def delete(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        attachment_id: int,
        variant: AttachmentVariant | None = None,
    ) -> int:
        """Delete one variant, or every variant when none is named."""
        ...

    async def delete_for_session(
        self, db: AsyncSession, *, namespace_key: str, session_id: int
    ) -> int:
        """Delete every blob belonging to one session's attachments.

        Unused by the Postgres implementation's own callers, because the
        composite cascade from ``agent_sessions`` already does this atomically.
        It is on the Protocol because an object store has no cascade: an
        implementation that stored bytes outside this database would leak every
        one of them on session delete without it, and discovering that after
        writing the implementation is discovering it in production.
        """
        ...


class PostgresAttachmentBlobStore:
    """The only implementation: a ``bytea`` column in this server's database."""

    async def put(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        attachment_id: int,
        variant: AttachmentVariant,
        content_type: str,
        data: bytes,
    ) -> str:
        digest = hashlib.sha256(data).hexdigest()
        db.add(
            AgentSessionAttachmentBlob(
                namespace_key=namespace_key,
                attachment_id=attachment_id,
                variant=variant.value,
                content_type=content_type,
                size_bytes=len(data),
                sha256=digest,
                data=data,
            )
        )
        await db.flush()
        return digest

    async def open(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        attachment_id: int,
        variant: AttachmentVariant,
    ) -> StoredBlob | None:
        stmt = select(
            AgentSessionAttachmentBlob.content_type,
            AgentSessionAttachmentBlob.size_bytes,
            AgentSessionAttachmentBlob.sha256,
            AgentSessionAttachmentBlob.data,
        ).where(
            AgentSessionAttachmentBlob.namespace_key == namespace_key,
            AgentSessionAttachmentBlob.attachment_id == attachment_id,
            AgentSessionAttachmentBlob.variant == variant.value,
        )
        row = (await db.execute(stmt)).first()
        if row is None:
            return None
        return StoredBlob(
            content_type=row[0], size_bytes=row[1], sha256=row[2], data=bytes(row[3])
        )

    async def delete(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        attachment_id: int,
        variant: AttachmentVariant | None = None,
    ) -> int:
        stmt = delete(AgentSessionAttachmentBlob).where(
            AgentSessionAttachmentBlob.namespace_key == namespace_key,
            AgentSessionAttachmentBlob.attachment_id == attachment_id,
        )
        if variant is not None:
            stmt = stmt.where(AgentSessionAttachmentBlob.variant == variant.value)
        return int(cast(CursorResult, await db.execute(stmt)).rowcount or 0)

    async def delete_for_session(
        self, db: AsyncSession, *, namespace_key: str, session_id: int
    ) -> int:
        """Delete blobs by subquery over the attachments of one session.

        A subquery rather than two round trips: the set has to be the same set
        at delete time as it was at select time, and a session under upload
        while this runs would otherwise leave bytes behind.
        """
        owned = select(AgentSessionAttachment.id).where(
            AgentSessionAttachment.namespace_key == namespace_key,
            AgentSessionAttachment.session_id == session_id,
        )
        stmt = delete(AgentSessionAttachmentBlob).where(
            AgentSessionAttachmentBlob.namespace_key == namespace_key,
            AgentSessionAttachmentBlob.attachment_id.in_(owned),
        )
        return int(cast(CursorResult, await db.execute(stmt)).rowcount or 0)


_blob_store: AttachmentBlobStore = PostgresAttachmentBlobStore()


def get_attachment_blob_store() -> AttachmentBlobStore:
    """Return the process-wide blob store."""
    return _blob_store
