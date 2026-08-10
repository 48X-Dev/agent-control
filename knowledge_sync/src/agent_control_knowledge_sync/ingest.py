"""Write one Drive item into the corpus, idempotently.

A failed conversion still gets its row with zero chunks: unfindable on purpose.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from agent_control_models.knowledge import (
    Chunk,
    ScrubbedChunks,
    chunk_and_scrub,
    is_secret_filename,
    normalize_index_name,
    normalize_index_path,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .convert import INDEXABLE_STATUSES, Converted, RawConverter, convert_document
from .drive_client import DriveItem, FetchedContent
from .ingest_guard import AGENT_OUTPUT_REFUSAL, AgentOutputGuard
from .schema import chunks, documents

__all__ = [
    "AUTHOR_KIND_UNKNOWN",
    "REFUSAL_TOMBSTONES",
    "IngestOutcome",
    "IngestRefusal",
    "Ingestor",
    "TombstoneReason",
]

# Drive's API carries no owner for a file the reader can only see, so the
# honest value is 'unknown' and trust comes from the source's own tier.
AUTHOR_KIND_UNKNOWN = "unknown"

_CONFLICT_KEYS = ("source_id", "external_id")
_NOTHING_INDEXED = ScrubbedChunks(chunks=[], secrets_skipped=0)

# What ``_metadata`` writes, which is what a skip has to read back to compare.
_METADATA_COLUMNS = ("path", "title", "source_mime", "source_modified_at", "bytes")

logger = logging.getLogger(__name__)


class IngestRefusal(StrEnum):
    """Every reason a document lands in the corpus with no chunks of its own."""

    TRASHED = "trashed"
    SHORTCUT = "shortcut"
    SECRET_FILE = "secret_file"
    AGENT_OUTPUT = AGENT_OUTPUT_REFUSAL
    CONVERSION_FAILED = "conversion_failed"
    EMPTY = "empty"
    ALL_CHUNKS_SCRUBBED = "all_chunks_scrubbed"


class TombstoneReason(StrEnum):
    """The closed enum ``documents.tombstone_reason`` accepts."""

    DELETED = "deleted"
    UNSHARED = "unshared"
    EXCLUDED = "excluded"
    OVERSIZE = "oversize"
    SECRET_FILE = "secret_file"


REFUSAL_TOMBSTONES: dict[str, TombstoneReason] = {
    "oversize": TombstoneReason.OVERSIZE,
    "export_too_large": TombstoneReason.OVERSIZE,
    "unreadable": TombstoneReason.UNSHARED,
    "shortcut_unreadable": TombstoneReason.UNSHARED,
}
"""Fetch refusals that must bury what the corpus already holds, and their reasons.

A refusal alone is a counter. A document indexed last week and refused today
keeps every stale chunk searchable until something tombstones it, which is 4.4's
whole argument and 5.4's ``tombstone_reason='oversize'`` in particular.
"""


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What one ``ingest`` call did to the corpus."""

    document_id: str | None
    chunks_written: int
    skipped_unchanged: bool
    refusal_code: str | None
    metadata_refreshed: bool = False


class Ingestor:
    """Writes one source's documents and chunks."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source_id: int,
        *,
        converter: RawConverter | None = None,
        guard: AgentOutputGuard | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._source_id = source_id
        self._converter = converter
        self._guard = guard
        self._secrets_skipped = 0

    @property
    def secrets_skipped(self) -> int:
        """Chunks the deny-list took since this ingestor was built."""

        return self._secrets_skipped

    async def ingest(self, item: DriveItem, content: FetchedContent) -> IngestOutcome:
        """Convert, chunk, scrub and store one item; skip it if nothing changed."""

        refusal = await self._refuse_before_conversion(item)
        if refusal is not None:
            logger.info("document %s refused before conversion: %s", item.id, refusal)
            return IngestOutcome(None, 0, False, refusal)

        converted = convert_document(
            content.data, declared_mime=content.media_type, converter=self._converter
        )
        digest = _content_sha256(converted.text)
        scrubbed = chunk_and_scrub(converted.text) if converted.indexable else _NOTHING_INDEXED

        async with self._session_factory() as session, session.begin():
            existing = await self._existing(session, item.id)
            if _is_unchanged(existing, digest):
                refreshed = await self._refresh_metadata(session, existing, item, content)
                return IngestOutcome(str(existing.id), 0, True, None, refreshed)
            document_id = await self._write_document(session, item, content, converted, digest)
            await self._replace_chunks(session, document_id, scrubbed.chunks)

        self._secrets_skipped += scrubbed.secrets_skipped
        refused = _refusal_for(converted, scrubbed.chunks)
        if refused is not None:
            logger.info("document %s stored with no chunks: %s", item.id, refused)
        return IngestOutcome(
            document_id=str(document_id),
            chunks_written=len(scrubbed.chunks),
            skipped_unchanged=False,
            refusal_code=refused,
        )

    async def refuse_fetch(self, external_id: str, code: str) -> bool:
        """Bury what the corpus holds for a document the fetch refused, when the code says to."""

        reason = REFUSAL_TOMBSTONES.get(code)
        if reason is None:
            return False
        return await self.tombstone(external_id, reason=reason)

    async def tombstone(
        self,
        external_id: str,
        *,
        reason: str = TombstoneReason.DELETED,
    ) -> bool:
        """Mark a document removed and drop its chunks, keeping the row as history."""

        async with self._session_factory() as session, session.begin():
            stmt = (
                sa.update(documents)
                .where(
                    documents.c.source_id == self._source_id,
                    documents.c.external_id == external_id,
                    documents.c.tombstoned_at.is_(None),
                )
                .values(tombstoned_at=_now(), tombstone_reason=reason)
                .returning(documents.c.id)
            )
            document_id = (await session.execute(stmt)).scalar_one_or_none()
            if document_id is None:
                return False
            await session.execute(sa.delete(chunks).where(chunks.c.document_id == document_id))
            logger.info("document %s tombstoned: %s", external_id, reason)
            return True

    async def _refuse_before_conversion(self, item: DriveItem) -> str | None:
        """Refuse the items that must never be converted, cheapest check first."""

        if item.trashed:
            await self.tombstone(item.id, reason=TombstoneReason.DELETED)
            return IngestRefusal.TRASHED
        if item.shortcut_target_id:
            return IngestRefusal.SHORTCUT
        if is_secret_filename(item.name):
            await self.tombstone(item.id, reason=TombstoneReason.SECRET_FILE)
            return IngestRefusal.SECRET_FILE
        if self._guard is not None and await self._guard.refuses(item.id):
            await self.tombstone(item.id, reason=TombstoneReason.EXCLUDED)
            return IngestRefusal.AGENT_OUTPUT
        return None

    async def _existing(self, session: AsyncSession, external_id: str) -> Any:
        stmt = sa.select(
            documents.c.id,
            documents.c.content_sha256,
            documents.c.tombstoned_at,
            *(documents.c[name] for name in _METADATA_COLUMNS),
        ).where(
            documents.c.source_id == self._source_id,
            documents.c.external_id == external_id,
        )
        return (await session.execute(stmt)).first()

    async def _refresh_metadata(
        self,
        session: AsyncSession,
        existing: Any,
        item: DriveItem,
        content: FetchedContent,
    ) -> bool:
        """A rename moves the citation without rewriting the chunks."""

        wanted = _metadata(item, content)
        drifted = {
            name: value for name, value in wanted.items() if getattr(existing, name) != value
        }
        if not drifted:
            return False
        await session.execute(
            sa.update(documents).where(documents.c.id == existing.id).values(**drifted)
        )
        logger.info("document %s metadata refreshed: %s", item.id, ", ".join(sorted(drifted)))
        return True

    async def _write_document(
        self,
        session: AsyncSession,
        item: DriveItem,
        content: FetchedContent,
        converted: Converted,
        digest: str,
    ) -> Any:
        values = {
            "source_id": self._source_id,
            "external_id": item.id,
            **_metadata(item, content),
            "author_kind": AUTHOR_KIND_UNKNOWN,
            "content_sha256": digest,
            "synced_at": _now(),
            "conversion_status": converted.status,
            "tombstoned_at": None,
            "tombstone_reason": None,
        }
        stmt = pg_insert(documents).values(**values)
        upsert = stmt.on_conflict_do_update(
            index_elements=[documents.c.source_id, documents.c.external_id],
            set_={name: stmt.excluded[name] for name in values if name not in _CONFLICT_KEYS},
        ).returning(documents.c.id)
        return (await session.execute(upsert)).scalar_one()

    async def _replace_chunks(
        self,
        session: AsyncSession,
        document_id: Any,
        written: list[Chunk],
    ) -> None:
        await session.execute(sa.delete(chunks).where(chunks.c.document_id == document_id))
        if not written:
            return
        await session.execute(
            sa.insert(chunks),
            [
                {
                    "document_id": document_id,
                    "ordinal": chunk.ordinal,
                    "heading_path": chunk.heading_path,
                    "body": chunk.body,
                    "chars": chunk.chars,
                }
                for chunk in written
            ],
        )


def _metadata(item: DriveItem, content: FetchedContent) -> dict[str, Any]:
    """The columns that change when a file is renamed, moved or touched but not edited."""

    return {
        "path": _index_path(item),
        "title": normalize_index_name(item.name) or item.id,
        "source_mime": item.mime_type,
        "source_modified_at": item.modified_time,
        "bytes": item.size if item.size is not None else len(content.data),
    }


def _index_path(item: DriveItem) -> str:
    """The citation: the folders under the corpus root, then the file's own name."""

    return normalize_index_path("/".join((*item.folder_path, item.name))) or item.id


def _is_unchanged(existing: Any, digest: str) -> bool:
    """A stored row matches only when it is live and hashes the same."""

    return (
        existing is not None
        and existing.tombstoned_at is None
        and existing.content_sha256 == digest
    )


def _refusal_for(converted: Converted, written: list[Chunk]) -> str | None:
    if converted.status not in INDEXABLE_STATUSES:
        return IngestRefusal.CONVERSION_FAILED
    if not converted.text.strip():
        return IngestRefusal.EMPTY
    if not written:
        return IngestRefusal.ALL_CHUNKS_SCRUBBED
    return None


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)
