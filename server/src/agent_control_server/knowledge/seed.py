"""Populate a corpus without a sync, for tests and for a local afternoon.

**This is the only thing in the package that writes, it is never reachable from
a request, and it takes its DSN as an argument with no default.** The reader's
credential cannot run it - ``knowledge_read`` holds SELECT - so calling it
requires deliberately handing it the sync role, which is the point: retrieval
has to be provable before a sync exists, and the way to do that is to write the
corpus on purpose rather than to relax the reader.

It is also an executable statement of the write contract Phase 2 has to honour:
convert, normalize the names at index time, chunk on headings and scrub in one
step, hash the content, insert. A sync that produced different rows than this
would be producing rows retrieval was never tested against.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
from agent_control_models.knowledge import (
    chunk_and_scrub,
    is_secret_filename,
    normalize_index_name,
    normalize_index_path,
)

from .schema import chunks, documents, sources, synonyms


@dataclass(frozen=True)
class SeedDocument:
    """One document as it would arrive from the converter: markdown plus facts."""

    path: str
    body: str
    title: str | None = None
    external_id: str | None = None
    author_kind: str = "workspace"
    source_mime: str | None = "text/markdown"
    conversion_status: str = "exported"
    source_modified_at: datetime | None = None
    tombstoned_at: datetime | None = None
    tombstone_reason: str | None = None


@dataclass
class SeedResult:
    """What the seed actually wrote, so a test can assert the scrub ran."""

    documents_written: int = 0
    chunks_written: int = 0
    secrets_skipped: int = 0
    files_skipped: int = 0
    source_ids: dict[str, int] = field(default_factory=dict)


def seed_corpus(
    sync_url: str,
    *,
    source_kind: str = "drive_folder",
    source_ref: str = "seed-folder",
    source_name: str = "Seed Folder",
    trust: str = "workspace",
    enabled: bool = True,
    last_verified_at: datetime | None = None,
    last_run_status: str | None = "ok",
    docs: Sequence[SeedDocument] = (),
) -> SeedResult:
    """Write one source and its documents. Idempotent on ``(kind, ref)``."""

    engine = sa.create_engine(sync_url, future=True, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            source_id = _upsert_source(
                conn,
                kind=source_kind,
                ref=source_ref,
                display_name=source_name,
                trust=trust,
                enabled=enabled,
                last_verified_at=last_verified_at or datetime.now(UTC),
                last_run_status=last_run_status,
            )
            result = SeedResult(source_ids={source_ref: source_id})
            for index, doc in enumerate(docs):
                _write_document(conn, source_id=source_id, index=index, doc=doc, result=result)
        return result
    finally:
        engine.dispose()


def seed_synonyms(sync_url: str, rows: Sequence[tuple[str, str]] = ()) -> int:
    """Load the curated rewrite table, replacing whatever was there.

    Configuration, not corpus. The sync never writes this table; it is the
    operator's own vocabulary, owned by whoever owns the source allowlist, and
    this is the loader a Phase 2 reload path will call with the same two
    columns. Replace rather than merge, because a rewrite an operator deleted
    from the file must stop being applied.

    ``plainto_tsquery`` rather than ``to_tsquery`` for both sides: the input is
    a person's words in a config file, and ``to_tsquery`` would turn a typo
    into a Postgres syntax error attached to a failed load.

    The stored target is ``source | target``, never the target alone.
    ``ts_rewrite`` substitutes, so a bare target would *redirect* "laptop" to
    "hardware provisioning" and stop matching the documents that say laptop -
    a synonym table that loses the word it was asked about. Widening is the
    only direction that can help.
    """

    engine = sa.create_engine(sync_url, future=True, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(synonyms.delete())
            for source_term, target_terms in rows:
                conn.execute(
                    sa.text(
                        "INSERT INTO synonyms"
                        " (source_term, target_terms, source_query, target_query)"
                        " VALUES (:source_term, :target_terms,"
                        " plainto_tsquery('english', :source_term),"
                        " plainto_tsquery('english', :source_term)"
                        " || plainto_tsquery('english', :target_terms))"
                    ),
                    {"source_term": source_term, "target_terms": target_terms},
                )
        return len(rows)
    finally:
        engine.dispose()


def _upsert_source(
    conn: sa.Connection,
    *,
    kind: str,
    ref: str,
    display_name: str,
    trust: str,
    enabled: bool,
    last_verified_at: datetime,
    last_run_status: str | None,
) -> int:
    values = {
        "kind": kind,
        "ref": ref,
        "display_name": display_name,
        "trust": trust,
        "enabled": enabled,
        "last_verified_at": last_verified_at,
        "last_run_status": last_run_status,
    }
    existing = conn.execute(
        sa.select(sources.c.id).where(sources.c.kind == kind, sources.c.ref == ref)
    ).scalar_one_or_none()
    if existing is not None:
        conn.execute(sources.update().where(sources.c.id == existing).values(**values))
        return int(existing)
    inserted = conn.execute(sources.insert().values(**values).returning(sources.c.id)).scalar_one()
    return int(inserted)


def _write_document(
    conn: sa.Connection,
    *,
    source_id: int,
    index: int,
    doc: SeedDocument,
    result: SeedResult,
) -> None:
    path = normalize_index_path(doc.path) or f"document-{index}"
    title = normalize_index_name(doc.title or path.rsplit("/", 1)[-1]) or f"document-{index}"

    # A file that is a credential by name never gets chunked, whatever it holds.
    if is_secret_filename(path):
        result.files_skipped += 1
        _insert_document(
            conn,
            source_id=source_id,
            doc=doc,
            index=index,
            path=path,
            title=title,
            tombstoned_at=datetime.now(UTC),
            tombstone_reason="secret_file",
        )
        result.documents_written += 1
        return

    document_id = _insert_document(
        conn,
        source_id=source_id,
        doc=doc,
        index=index,
        path=path,
        title=title,
        tombstoned_at=doc.tombstoned_at,
        tombstone_reason=doc.tombstone_reason,
    )
    result.documents_written += 1

    if doc.tombstoned_at is not None:
        # A tombstone keeps its row and loses its chunks; that is what makes it
        # unsearchable the moment the sweep runs.
        return

    # Chunk and scrub together, never one then the other: a credential lying
    # across a hard split matches neither piece, and the sync has to write what
    # this writes.
    scrubbed = chunk_and_scrub(doc.body)
    result.secrets_skipped += scrubbed.secrets_skipped
    for ordinal, chunk in enumerate(scrubbed.chunks):
        conn.execute(
            chunks.insert().values(
                document_id=document_id,
                ordinal=ordinal,
                heading_path=chunk.heading_path,
                body=chunk.body,
                chars=chunk.chars,
            )
        )
        result.chunks_written += 1


def _insert_document(
    conn: sa.Connection,
    *,
    source_id: int,
    doc: SeedDocument,
    index: int,
    path: str,
    title: str,
    tombstoned_at: datetime | None,
    tombstone_reason: str | None,
) -> int:
    now = datetime.now(UTC)
    inserted = conn.execute(
        documents.insert()
        .values(
            source_id=source_id,
            external_id=doc.external_id or f"seed-{index}",
            path=path,
            title=title,
            source_mime=doc.source_mime,
            author_kind=doc.author_kind,
            content_sha256=hashlib.sha256(doc.body.encode("utf-8")).hexdigest(),
            source_modified_at=doc.source_modified_at or now,
            synced_at=now,
            conversion_status=doc.conversion_status,
            bytes=len(doc.body.encode("utf-8")),
            tombstoned_at=tombstoned_at,
            tombstone_reason=tombstone_reason,
        )
        .returning(documents.c.id)
    ).scalar_one()
    return int(inserted)
