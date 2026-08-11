"""Core table definitions for ``agent_knowledge``, for querying and for seeding.

These mirror ``server/knowledge_alembic/versions/`` and do not create anything.
The migrations are the source of truth: they own a generated ``tsvector``
column, two GIN indexes with operator classes and a seeded singleton, none of
which Core can express faithfully, so autogenerate is off and this file is
hand-kept in step. ``test_knowledge_store.py`` reflects the migrated database
and compares it against this metadata, which is what stops the two drifting
quietly.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSQUERY, TSVECTOR

# The corpus schema versions this server understands. A version outside this set
# means the sync has moved ahead of (or behind) the reader, and every search
# answers ``knowledge_unavailable`` rather than guessing at the row shape.
# 4 adds the sync's `conversion_cache`, which nothing on this side reads, so a
# corpus at either version answers searches out of the same rows.
SUPPORTED_SCHEMA_VERSIONS = frozenset({3, 4})

KNOWLEDGE_METADATA = sa.MetaData()

schema_meta = sa.Table(
    "schema_meta",
    KNOWLEDGE_METADATA,
    sa.Column("id", sa.SmallInteger, primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

sources = sa.Table(
    "sources",
    KNOWLEDGE_METADATA,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("ref", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("trust", sa.Text, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("cursor", JSONB),
    sa.Column("cursor_advanced_at", sa.DateTime(timezone=True)),
    sa.Column("last_verified_at", sa.DateTime(timezone=True)),
    sa.Column("last_run_status", sa.Text),
    sa.Column("last_run_error_code", sa.Text),
)

documents = sa.Table(
    "documents",
    KNOWLEDGE_METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id"), nullable=False),
    sa.Column("external_id", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("source_mime", sa.Text),
    sa.Column("author_kind", sa.Text, nullable=False),
    sa.Column("content_sha256", sa.CHAR(64), nullable=False),
    sa.Column("source_modified_at", sa.DateTime(timezone=True)),
    sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("conversion_status", sa.Text, nullable=False),
    sa.Column("bytes", sa.BigInteger, nullable=False),
    sa.Column("tombstoned_at", sa.DateTime(timezone=True)),
    sa.Column("tombstone_reason", sa.Text),
)

chunks = sa.Table(
    "chunks",
    KNOWLEDGE_METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column(
        "document_id",
        sa.BigInteger,
        sa.ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("heading_path", sa.Text),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("chars", sa.Integer, nullable=False),
    # Declared Computed so it is excluded from INSERTs; the database fills it.
    sa.Column(
        "body_tsv",
        TSVECTOR,
        sa.Computed("to_tsvector('english', body)", persisted=True),
    ),
)

sync_lease = sa.Table(
    "sync_lease",
    KNOWLEDGE_METADATA,
    sa.Column("id", sa.SmallInteger, primary_key=True),
    sa.Column("holder", sa.Text),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
)

sync_runs = sa.Table(
    "sync_runs",
    KNOWLEDGE_METADATA,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("holder", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text),
    sa.Column("files_seen", sa.Integer, nullable=False),
    sa.Column("files_converted", sa.Integer, nullable=False),
    sa.Column("files_failed", sa.Integer, nullable=False),
    sa.Column("files_skipped", sa.Integer, nullable=False),
    sa.Column("secrets_skipped", sa.Integer, nullable=False),
    sa.Column("bytes_fetched", sa.BigInteger, nullable=False),
    sa.Column("error_code", sa.Text),
)

synonyms = sa.Table(
    "synonyms",
    KNOWLEDGE_METADATA,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source_term", sa.Text, nullable=False),
    sa.Column("target_terms", sa.Text, nullable=False),
    sa.Column("source_query", TSQUERY, nullable=False),
    sa.Column("target_query", TSQUERY, nullable=False),
)
