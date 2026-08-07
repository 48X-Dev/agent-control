"""Alembic environment for ``agent_knowledge``, the company-knowledge corpus.

Two things this deliberately does not do.

It does not import ``agent_control_server``. These migrations are run by
whichever process owns the corpus schema - today a human or a test fixture,
from Phase 2 the sync container - and that container carries the shared models
package, not the server. An import of the control plane here would make the
corpus schema undeployable without the thing it is separated from.

It does not set ``target_metadata``, so ``--autogenerate`` produces nothing.
The corpus schema is hand-written SQL: a ``tsvector`` column generated in the
database, two GIN indexes with operator classes, a seeded singleton row.
Autogenerate cannot express those faithfully and would propose dropping them on
every run, which is a worse failure than typing the DDL out once.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

DEFAULT_URL = "postgresql+psycopg://knowledge_sync:knowledge_local@localhost:5432/agent_knowledge"
VERSION_TABLE = config.get_main_option("version_table", "knowledge_alembic_version")


def _get_migration_url() -> str:
    """The sync role's DSN. These migrations create tables; the reader cannot."""
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return os.environ.get("AGENT_KNOWLEDGE_DB_URL") or DEFAULT_URL


def run_migrations_offline() -> None:
    context.configure(
        url=_get_migration_url(),
        literal_binds=True,
        version_table=VERSION_TABLE,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_get_migration_url(), future=True, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            version_table=VERSION_TABLE,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
