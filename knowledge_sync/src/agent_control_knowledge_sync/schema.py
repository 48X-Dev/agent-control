"""Write-side table metadata for ``agent_knowledge``; the migrations are the source of truth.

Autogenerate is off: the generated ``tsvector`` and GIN opclasses have no Core spelling.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

# 4 adds `conversion_cache`, which no reader touches, so a k003 corpus is still
# a corpus both halves understand: the sync simply converts everything.
SUPPORTED_SCHEMA_VERSIONS = frozenset({3, 4})

SYNC_METADATA = sa.MetaData()

schema_meta = sa.Table(
    "schema_meta",
    SYNC_METADATA,
    sa.Column("id", sa.SmallInteger, primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

sources = sa.Table(
    "sources",
    SYNC_METADATA,
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
    sa.UniqueConstraint("kind", "ref"),
)

documents = sa.Table(
    "documents",
    SYNC_METADATA,
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
    sa.UniqueConstraint("source_id", "external_id"),
)

chunks = sa.Table(
    "chunks",
    SYNC_METADATA,
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
    # Computed so it never appears in an INSERT; the database fills it.
    sa.Column(
        "body_tsv",
        TSVECTOR,
        sa.Computed("to_tsvector('english', body)", persisted=True),
    ),
    sa.UniqueConstraint("document_id", "ordinal"),
)

conversion_cache = sa.Table(
    "conversion_cache",
    SYNC_METADATA,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
)

sync_lease = sa.Table(
    "sync_lease",
    SYNC_METADATA,
    sa.Column("id", sa.SmallInteger, primary_key=True),
    sa.Column("holder", sa.Text),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
)

sync_runs = sa.Table(
    "sync_runs",
    SYNC_METADATA,
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
