"""Converted markdown, keyed on the bytes and on what converted them.

Revision ID: k004
Revises: k003
Create Date: 2026-08-11
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "k004"
down_revision = "k003"
branch_labels = None
depends_on = None

SCHEMA_VERSION = 4

_READER_ROLE_ENV = "AGENT_KNOWLEDGE_READ_ROLE"
_DEFAULT_READER_ROLE = "knowledge_read"

# k001 grants the reader SELECT on every later table. This one holds converted
# text before the deny-list ran over it, so the grant comes straight back off.
_REVOKE_READER = """
DO $$
DECLARE
    reader text := current_setting('knowledge_migration.read_role');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader) THEN
        RETURN;
    END IF;
    EXECUTE format('REVOKE ALL ON conversion_cache FROM %I', reader);
END
$$;
"""


def _set_reader_role() -> None:
    op.get_bind().execute(
        sa.text("SELECT set_config('knowledge_migration.read_role', :role, false)"),
        {"role": os.environ.get(_READER_ROLE_ENV) or _DEFAULT_READER_ROLE},
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE conversion_cache (
            key          text PRIMARY KEY,
            status       text NOT NULL,
            error_code   text,
            body         text NOT NULL,
            stored_at    timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # The eviction sweep's only access path: one range delete per run.
    op.execute("CREATE INDEX ix_conversion_cache_last_used_at ON conversion_cache (last_used_at)")

    _set_reader_role()
    op.execute(_REVOKE_READER)

    op.execute(
        f"UPDATE schema_meta SET version = {SCHEMA_VERSION}, updated_at = now() WHERE id = 1"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conversion_cache")
    op.execute("UPDATE schema_meta SET version = 3, updated_at = now() WHERE id = 1")
