"""What the issue channel writes into the corpus, keyed so a replay is free.

Split from ``github_issues.py`` at that module's size ceiling, along the seam
``drive_client.py`` and ``ingest.py`` already use for Drive: what a source says
on one side, what the corpus stores on the other. Nothing here talks to GitHub.

The channel gets its own ``sources`` row, ``owner/name#issues``, rather than
sharing the repo's. ``sources.trust`` is one value per row, and section 7 puts
issue text at ``external_authors`` while the same repo's files are
``workspace``; one row cannot be both, and the ceiling that tier buys is only
worth having if it applies to the text it was chosen for.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from agent_control_models.knowledge import chunk_and_scrub
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .allowlist import RepoRef
from .lease import SessionFactory
from .schema import chunks, documents, sources

__all__ = [
    "CURSOR_KEY",
    "ISSUES_REF_SUFFIX",
    "SOURCE_TRUST",
    "IssueDocument",
    "StoreOutcome",
    "advance_cursor",
    "ensure_source",
    "store_document",
]

SOURCE_KIND = "github_repo"
SOURCE_TRUST = "external_authors"
ISSUES_REF_SUFFIX = "#issues"
CURSOR_KEY = "issues_since"

# Issue text arrives as the chunker's own input, so it is stored under the
# status 4.2 reserves for text that reached the corpus without a converter.
CONVERSION_STATUS = "exported"
DOCUMENT_MIME = "text/markdown"

_CONFLICT_KEYS = ("source_id", "external_id")


@dataclass(frozen=True, slots=True)
class IssueDocument:
    """One issue, PR, review summary or commit subject, as the corpus stores it."""

    external_id: str
    path: str
    title: str
    text: str
    author_kind: str
    source_modified_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoreOutcome:
    """What one ``store_document`` call did."""

    unchanged: bool
    chunks_written: int
    secrets_skipped: int


_INSERT_SOURCE = pg_insert(sources).values(
    kind=SOURCE_KIND,
    ref=sa.bindparam("ref"),
    display_name=sa.bindparam("display_name"),
    trust=SOURCE_TRUST,
)
_ENSURE_SOURCE = _INSERT_SOURCE.on_conflict_do_update(
    index_elements=[sources.c.kind, sources.c.ref],
    set_={"display_name": _INSERT_SOURCE.excluded.display_name},
).returning(sources.c.id, sources.c.cursor[CURSOR_KEY].astext.label("since"))

_CURSOR_JSON = sa.func.jsonb_build_object(
    sa.cast(sa.literal(CURSOR_KEY), sa.Text), sa.cast(sa.bindparam("since"), sa.Text)
)


async def ensure_source(sessions: SessionFactory, repo: RepoRef) -> tuple[int, str | None]:
    """The channel's own source row, at ``external_authors``, and its stored cursor."""
    params = {
        "ref": f"{repo.full_name}{ISSUES_REF_SUFFIX}",
        "display_name": f"{repo.full_name} issues",
    }
    async with sessions() as session:
        row = (await session.execute(_ENSURE_SOURCE, params)).one()
        await session.commit()
    return int(row.id), row.since


async def advance_cursor(sessions: SessionFactory, source_id: int, started: datetime) -> None:
    """Stamp the cursor and the verification clock together, after the writes commit.

    The cursor is the instant taken *before* the read, so an issue edited while
    the run was in flight is re-read next time rather than missed. Without the
    verification stamp this source reads as never verified and takes the whole
    corpus's staleness line down with it (section 10).
    """
    async with sessions() as session:
        await session.execute(
            sa.update(sources)
            .where(sources.c.id == sa.bindparam("source_id"))
            .values(
                cursor=_CURSOR_JSON,
                cursor_advanced_at=sa.func.now(),
                last_verified_at=sa.func.now(),
                last_run_status="ok",
                last_run_error_code=None,
            ),
            {"source_id": source_id, "since": started.isoformat()},
        )
        await session.commit()


async def store_document(
    sessions: SessionFactory, source_id: int, document: IssueDocument
) -> StoreOutcome:
    """Upsert one document and replace its chunks; an unchanged one writes nothing."""
    digest = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
    scrubbed = chunk_and_scrub(document.text)
    async with sessions() as session, session.begin():
        existing = (
            await session.execute(
                sa.select(
                    documents.c.content_sha256, documents.c.author_kind, documents.c.tombstoned_at
                ).where(
                    documents.c.source_id == source_id,
                    documents.c.external_id == document.external_id,
                )
            )
        ).first()
        if _is_unchanged(existing, digest, document.author_kind):
            return StoreOutcome(True, 0, 0)
        document_id = await _upsert(session, source_id, document, digest)
        await session.execute(sa.delete(chunks).where(chunks.c.document_id == document_id))
        if scrubbed.chunks:
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
                    for chunk in scrubbed.chunks
                ],
            )
    return StoreOutcome(False, len(scrubbed.chunks), scrubbed.secrets_skipped)


def _is_unchanged(existing: Any, digest: str, author_kind: str) -> bool:
    """``author_kind`` joins the hash: a membership change leaves the text identical."""
    return (
        existing is not None
        and existing.tombstoned_at is None
        and existing.content_sha256 == digest
        and existing.author_kind == author_kind
    )


async def _upsert(session: Any, source_id: int, document: IssueDocument, digest: str) -> Any:
    values = {
        "source_id": source_id,
        "external_id": document.external_id,
        "path": document.path,
        "title": document.title,
        "source_mime": DOCUMENT_MIME,
        "author_kind": document.author_kind,
        "content_sha256": digest,
        "source_modified_at": document.source_modified_at,
        "synced_at": datetime.now(UTC),
        "conversion_status": CONVERSION_STATUS,
        "bytes": len(document.text.encode("utf-8")),
        "tombstoned_at": None,
        "tombstone_reason": None,
    }
    stmt = pg_insert(documents).values(**values)
    upsert = stmt.on_conflict_do_update(
        index_elements=[documents.c.source_id, documents.c.external_id],
        set_={name: stmt.excluded[name] for name in values if name not in _CONFLICT_KEYS},
    ).returning(documents.c.id)
    return (await session.execute(upsert)).scalar_one()
