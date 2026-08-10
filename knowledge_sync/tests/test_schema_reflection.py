"""The sync's write-side metadata against the database the migrations made.

One schema has three spellings in this repo: the migrations own it, the server
declares the read side, and the sync declares the write side it inserts
through. Nothing but this file stops the three drifting, and the drift is
invisible until a deployment: a column added to a migration and not to the
sync's metadata is an INSERT that omits it, and a column added to the metadata
and not to the migration is an INSERT that fails on a machine no test ran on.

Reflection is the arbiter throughout, never one hand-written list against
another, because a list somebody has to keep in step is the thing being tested.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest
import sqlalchemy as sa
from agent_control_knowledge_sync import schema as sync_schema
from agent_control_server.knowledge.schema import KNOWLEDGE_METADATA as SERVER_METADATA
from sqlalchemy.pool import NullPool

# Every table the sync writes to. Named here rather than derived, because the
# question this asks is whether the sync declared them at all.
WRITTEN_BY_THE_SYNC = frozenset(
    {"sources", "documents", "chunks", "sync_lease", "sync_runs", "schema_meta"}
)


def _sync_metadata() -> sa.MetaData:
    """The one MetaData the sync declares, whatever it chose to call it."""
    found = [value for value in vars(sync_schema).values() if isinstance(value, sa.MetaData)]
    assert len(found) == 1, f"expected one MetaData in {sync_schema.__name__}, found {len(found)}"
    return found[0]


SYNC_METADATA = _sync_metadata()
SYNC_TABLES = sorted(SYNC_METADATA.tables)


def _reflected(corpus: Any, table: str) -> list[dict[str, Any]]:
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with warnings.catch_warnings():
            # Reflection has no Python type for `tsquery`. Not drift, and not
            # this file's business: names and nullability are.
            warnings.simplefilter("ignore", sa.exc.SAWarning)
            return sa.inspect(engine).get_columns(table)
    finally:
        engine.dispose()


def test_the_sync_declares_every_table_it_writes() -> None:
    assert WRITTEN_BY_THE_SYNC <= set(SYNC_METADATA.tables)


def test_every_declared_table_exists_in_the_migrated_database(corpus: Any) -> None:
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        present = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert set(SYNC_METADATA.tables) <= present


@pytest.mark.parametrize("table", SYNC_TABLES)
def test_the_declared_columns_are_the_migrated_columns(corpus: Any, table: str) -> None:
    declared = {column.name for column in SYNC_METADATA.tables[table].columns}
    actual = {column["name"] for column in _reflected(corpus, table)}

    assert declared == actual


@pytest.mark.parametrize("table", SYNC_TABLES)
def test_the_declared_nullability_is_the_migrated_nullability(corpus: Any, table: str) -> None:
    """Names alone let a NOT NULL drift through, and that one fails at INSERT."""
    actual = {column["name"]: column["nullable"] for column in _reflected(corpus, table)}
    declared = {
        column.name: bool(column.nullable) for column in SYNC_METADATA.tables[table].columns
    }

    assert declared == actual


@pytest.mark.parametrize("table", SYNC_TABLES)
def test_the_declared_primary_key_is_the_migrated_primary_key(corpus: Any, table: str) -> None:
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        actual = sa.inspect(engine).get_pk_constraint(table)["constrained_columns"]
    finally:
        engine.dispose()
    declared = [column.name for column in SYNC_METADATA.tables[table].primary_key]

    assert sorted(declared) == sorted(actual)


def test_the_generated_tsvector_is_declared_generated() -> None:
    """A plain column here is every chunk INSERT failing, on the first real run.

    ``body_tsv`` is filled by the database. Declared as an ordinary column,
    SQLAlchemy includes it in the INSERT and Postgres refuses the statement,
    which is a failure mode no unit test with a stubbed session ever sees.
    """
    body_tsv = SYNC_METADATA.tables["chunks"].columns["body_tsv"]

    assert body_tsv.computed is not None
    assert body_tsv.computed.persisted


def test_the_sync_and_the_server_declare_the_same_shared_tables() -> None:
    """The write side and the read side of one row, kept honest against each other.

    Both are checked against the migrations above, so this is redundant until
    the day somebody adds a table to one and to the migrations and forgets the
    other. Then it is the only thing that speaks.
    """
    shared = set(SYNC_METADATA.tables) & set(SERVER_METADATA.tables)
    assert WRITTEN_BY_THE_SYNC <= shared

    for name in sorted(shared):
        sync_columns = {column.name for column in SYNC_METADATA.tables[name].columns}
        server_columns = {column.name for column in SERVER_METADATA.tables[name].columns}
        assert sync_columns == server_columns, name
