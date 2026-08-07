"""The curated synonym rewrite table.

Revision ID: k003
Revises: k002
Create Date: 2026-08-06

Full-text search misses synonymy, and before embeddings the cheapest real answer
is twenty rows of the company's own vocabulary applied with ``ts_rewrite`` at
query time. The sync never writes this table: it is operator-curated
configuration loaded from ``synonyms.yaml`` beside the source allowlist, and it
is inspectable in a way a vector similarity is not.

The two ``tsquery`` columns are stored rather than generated. ``to_tsquery``
raises on input it cannot parse, so a generated column would turn a typo in a
config file into a failed INSERT with a Postgres syntax error attached; the
loader builds them with ``plainto_tsquery`` and keeps the operator's own words
beside them for display.
"""

from alembic import op

revision = "k003"
down_revision = "k002"
branch_labels = None
depends_on = None

SCHEMA_VERSION = 3


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE synonyms (
            id           integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_term  text NOT NULL,
            target_terms text NOT NULL,
            source_query tsquery NOT NULL,
            target_query tsquery NOT NULL,
            UNIQUE (source_term)
        )
        """
    )
    op.execute(
        f"UPDATE schema_meta SET version = {SCHEMA_VERSION}, updated_at = now() WHERE id = 1"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS synonyms")
    op.execute("UPDATE schema_meta SET version = 2, updated_at = now() WHERE id = 1")
