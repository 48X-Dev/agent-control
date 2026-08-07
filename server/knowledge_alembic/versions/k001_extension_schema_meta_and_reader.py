"""pg_trgm, the schema_meta marker, and the reader's privileges.

Revision ID: k001
Revises:
Create Date: 2026-08-06

The reader's grants ship in this revision rather than in the provisioning
script alone, and that is the whole point of it. The init script creates roles
and the database; tables arrive here, created by the sync role, and a table's
creator grants nothing to anyone implicitly. Without these three statements
``knowledge_read`` connects and sees nothing, every search refuses
``knowledge_unavailable`` forever, and the failure reads as an empty corpus
rather than as a missing GRANT.

``ALTER DEFAULT PRIVILEGES`` covers every table later revisions create; the
catch-up ``GRANT SELECT ON ALL TABLES`` covers anything that already exists,
because default privileges are forward-looking only.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "k001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_VERSION = 1

_READER_ROLE_ENV = "AGENT_KNOWLEDGE_READ_ROLE"
_DEFAULT_READER_ROLE = "knowledge_read"

# The role name is carried through a GUC and quoted with %I rather than pasted
# into the statement: it comes from the environment, and an identifier built by
# string concatenation is an injection whatever the deployment intended.
_GRANT_READER = """
DO $$
DECLARE
    reader text := current_setting('knowledge_migration.read_role');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader) THEN
        RAISE EXCEPTION
            'role % does not exist; run server/scripts/knowledge_db_init.sql against '
            'this instance before migrating the corpus', reader;
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', reader);
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO %I', reader
    );
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', reader);
END
$$;
"""

_REVOKE_READER = """
DO $$
DECLARE
    reader text := current_setting('knowledge_migration.read_role');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader) THEN
        RETURN;
    END IF;
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM %I', reader
    );
    EXECUTE format('REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM %I', reader);
    EXECUTE format('REVOKE USAGE ON SCHEMA public FROM %I', reader);
END
$$;
"""


def _set_reader_role() -> None:
    op.get_bind().execute(
        sa.text("SELECT set_config('knowledge_migration.read_role', :role, false)"),
        {"role": os.environ.get(_READER_ROLE_ENV) or _DEFAULT_READER_ROLE},
    )


def upgrade() -> None:
    # pg_trgm backstops short and misspelled queries where websearch_to_tsquery
    # returns nothing. Trusted since Postgres 13, so the database owner can
    # install it without a superuser in the loop.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE TABLE schema_meta (
            id         smallint PRIMARY KEY CHECK (id = 1),
            version    integer NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(f"INSERT INTO schema_meta (id, version) VALUES (1, {SCHEMA_VERSION})")

    _set_reader_role()
    op.execute(_GRANT_READER)


def downgrade() -> None:
    _set_reader_role()
    op.execute(_REVOKE_READER)
    op.execute("DROP TABLE IF EXISTS schema_meta")
    # pg_trgm is left installed. Dropping an extension a neighbouring object
    # might depend on is not this revision's business.
