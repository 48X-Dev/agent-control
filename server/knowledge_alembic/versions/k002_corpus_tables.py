"""The corpus tables, their indexes, and the seeded sync lease.

Revision ID: k002
Revises: k001
Create Date: 2026-08-06

Written as SQL rather than as ``op.create_table`` calls because three things
here have no faithful Core spelling: a ``tsvector`` column the database
generates, two GIN indexes with operator classes, and a singleton row seeded by
the migration itself.

That last one is load-bearing. The lease is claimed by a single
``UPDATE ... WHERE lease_expires_at < now() RETURNING``, and an UPDATE never
inserts, so the row has to exist before any claimant. Seeding it here is what
lets the claim be one statement, which is the only shape that survives the
concurrency it exists to prevent - ``services/turn_locks.py`` works for exactly
this reason.
"""

from alembic import op

revision = "k002"
down_revision = "k001"
branch_labels = None
depends_on = None

SCHEMA_VERSION = 2


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sources (
            id                 integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind               text NOT NULL CHECK (kind IN ('drive_folder', 'github_repo')),
            ref                text NOT NULL,
            display_name       text NOT NULL,
            trust              text NOT NULL
                               CHECK (trust IN ('workspace', 'external_authors')),
            enabled            boolean NOT NULL DEFAULT true,
            cursor             jsonb,
            cursor_advanced_at timestamptz,
            last_verified_at   timestamptz,
            last_run_status    text CHECK (last_run_status IN ('ok', 'partial', 'failed')),
            last_run_error_code text,
            UNIQUE (kind, ref)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE documents (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_id          integer NOT NULL REFERENCES sources(id),
            external_id        text NOT NULL,
            path               text NOT NULL,
            title              text NOT NULL,
            source_mime        text,
            author_kind        text NOT NULL
                               CHECK (author_kind IN ('workspace', 'external', 'unknown')),
            content_sha256     char(64) NOT NULL,
            source_modified_at timestamptz,
            synced_at          timestamptz NOT NULL,
            conversion_status  text NOT NULL,
            bytes              bigint NOT NULL,
            tombstoned_at      timestamptz,
            tombstone_reason   text CHECK (tombstone_reason IN (
                                   'deleted', 'unshared', 'excluded', 'oversize', 'secret_file'
                               )),
            UNIQUE (source_id, external_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE chunks (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id  bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal      integer NOT NULL,
            heading_path text,
            body         text NOT NULL,
            chars        integer NOT NULL,
            body_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
            UNIQUE (document_id, ordinal)
        )
        """
    )

    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (body_tsv)")
    op.execute("CREATE INDEX ix_chunks_trgm ON chunks USING gin (body gin_trgm_ops)")

    # Retrieval's three access paths, in order: rank a search inside one source,
    # answer "what changed" by modified date over a window, and collapse two
    # copies of the same bytes reachable from two places. The first two are
    # partial on live documents because a tombstoned document is never a result.
    op.execute("CREATE INDEX ix_documents_source_id ON documents (source_id)")
    op.execute(
        """
        CREATE INDEX ix_documents_modified_at ON documents (source_modified_at DESC)
         WHERE tombstoned_at IS NULL
        """
    )
    op.execute("CREATE INDEX ix_documents_content_sha256 ON documents (content_sha256)")

    op.execute(
        """
        CREATE TABLE sync_lease (
            id               smallint PRIMARY KEY CHECK (id = 1),
            holder           text,
            lease_expires_at timestamptz NOT NULL DEFAULT '-infinity'
        )
        """
    )
    # The singleton, before any claimant. See the module docstring.
    op.execute("INSERT INTO sync_lease (id) VALUES (1)")

    op.execute(
        """
        CREATE TABLE sync_runs (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            holder          text NOT NULL,
            started_at      timestamptz NOT NULL,
            finished_at     timestamptz,
            status          text CHECK (status IN ('running', 'ok', 'partial', 'failed', 'lapsed')),
            files_seen      integer NOT NULL DEFAULT 0,
            files_converted integer NOT NULL DEFAULT 0,
            files_failed    integer NOT NULL DEFAULT 0,
            files_skipped   integer NOT NULL DEFAULT 0,
            secrets_skipped integer NOT NULL DEFAULT 0,
            bytes_fetched   bigint  NOT NULL DEFAULT 0,
            error_code      text
        )
        """
    )

    op.execute(
        f"UPDATE schema_meta SET version = {SCHEMA_VERSION}, updated_at = now() WHERE id = 1"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sync_runs")
    op.execute("DROP TABLE IF EXISTS sync_lease")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP TABLE IF EXISTS sources")
    op.execute("UPDATE schema_meta SET version = 1, updated_at = now() WHERE id = 1")
