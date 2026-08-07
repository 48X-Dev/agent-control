"""The chain walked against a real database, one revision at a time.

``tests/test_knowledge_migrations.py`` reads the files. This asks what the
files actually do to a corpus, and three of the answers are load-bearing.

The marker row has to move with the schema, because it is the only thing the
reader consults before trusting a row's shape. The reader's SELECT has to
arrive from migration 001 and leave again on its downgrade, because a grant
that quietly outlives the schema it was for is a reader with privileges nobody
granted deliberately. And a migration run against an instance that never got
the provisioning script has to stop and say so: without the roles, every
statement still succeeds, the corpus fills up, and the reader sees an empty
mirror forever.

Every test here moves the schema, so the autouse fixture puts it back. The next
test in this module truncates before it runs and would fail against a database
left at base.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from agent_control_server.knowledge.schema import SUPPORTED_SCHEMA_VERSIONS
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.pool import NullPool

from tests.knowledge_provisioning import ALEMBIC_DIR, ALEMBIC_INI, Corpus, migrate

CORPUS_TABLES = ("sources", "documents", "chunks", "sync_lease", "sync_runs", "synonyms")


def _config(sync_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR).replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
    return config


def upgrade_to(sync_url: str, target: str) -> None:
    alembic_command.upgrade(_config(sync_url), target)


def downgrade_to(sync_url: str, target: str) -> None:
    alembic_command.downgrade(_config(sync_url), target)


def head_revision() -> str:
    head = ScriptDirectory.from_config(_config("postgresql+psycopg://x:y@localhost/z")).get_current_head()
    assert head is not None
    return head


def scalar(url: str, sql: str) -> Any:
    engine = sa.create_engine(url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(sql)).scalar()
    finally:
        engine.dispose()


def execute(url: str, sql: str) -> None:
    engine = sa.create_engine(url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(sql))
    finally:
        engine.dispose()


def table_exists(url: str, name: str) -> bool:
    return scalar(url, f"SELECT to_regclass('public.{name}')::text") is not None


def schema_version(url: str) -> int | None:
    if not table_exists(url, "schema_meta"):
        return None
    return scalar(url, "SELECT version FROM schema_meta WHERE id = 1")


@pytest.fixture(autouse=True)
def restored_to_head(corpus: Corpus) -> Iterator[None]:
    """Leave the schema whole however the test ended."""
    try:
        yield
    finally:
        migrate(corpus.sync_url)


# --- The marker row moves with the schema -----------------------------------


def test_each_revision_leaves_the_version_it_declares(corpus: Corpus) -> None:
    """Applied one at a time, not just as a batch that lands on the right number.

    A partial upgrade is the normal state during a deployment, and a reader
    querying mid-migration must see a version it can decide about rather than
    the head's number over the previous head's tables.
    """
    downgrade_to(corpus.sync_url, "base")

    upgrade_to(corpus.sync_url, "k001")
    assert schema_version(corpus.sync_url) == 1
    assert not table_exists(corpus.sync_url, "sources")

    upgrade_to(corpus.sync_url, "k002")
    assert schema_version(corpus.sync_url) == 2
    assert scalar(corpus.sync_url, "SELECT count(*) FROM sync_lease") == 1
    assert not table_exists(corpus.sync_url, "synonyms")

    upgrade_to(corpus.sync_url, "k003")
    assert schema_version(corpus.sync_url) == 3
    assert table_exists(corpus.sync_url, "synonyms")
    assert schema_version(corpus.sync_url) in SUPPORTED_SCHEMA_VERSIONS


def test_a_downgrade_puts_the_version_back_where_it_found_it(corpus: Corpus) -> None:
    """Otherwise a rollback leaves the marker claiming a schema that is gone."""
    assert schema_version(corpus.sync_url) == max(SUPPORTED_SCHEMA_VERSIONS)

    downgrade_to(corpus.sync_url, "k002")
    assert schema_version(corpus.sync_url) == 2

    downgrade_to(corpus.sync_url, "k001")
    assert schema_version(corpus.sync_url) == 1


def test_the_chain_goes_all_the_way_back_and_comes_all_the_way_forward(corpus: Corpus) -> None:
    """Nothing left behind, and the seeded singleton comes back with it.

    The lease row is seeded by the migration because the claim is one UPDATE
    and an UPDATE never inserts. A re-upgrade that forgot to re-seed it would
    leave a sync that can never take the lease, and the symptom is a corpus
    that silently stops being refreshed.
    """
    downgrade_to(corpus.sync_url, "base")

    for table in (*CORPUS_TABLES, "schema_meta"):
        assert not table_exists(corpus.sync_url, table), table
    assert scalar(corpus.sync_url, "SELECT count(*) FROM knowledge_alembic_version") == 0

    upgrade_to(corpus.sync_url, "head")

    for table in (*CORPUS_TABLES, "schema_meta"):
        assert table_exists(corpus.sync_url, table), table
    assert scalar(corpus.sync_url, "SELECT count(*) FROM sync_lease") == 1
    assert schema_version(corpus.sync_url) == max(SUPPORTED_SCHEMA_VERSIONS)


def test_the_corpus_does_not_answer_to_the_control_planes_bookkeeping(corpus: Corpus) -> None:
    """Two databases, two version tables, and no way to migrate one as the other."""
    assert not table_exists(corpus.sync_url, "alembic_version")
    assert table_exists(corpus.sync_url, "knowledge_alembic_version")
    assert scalar(corpus.sync_url, "SELECT version_num FROM knowledge_alembic_version") == (
        head_revision()
    )


# --- The reader's privilege belongs to migration 001 ------------------------


def test_the_readers_default_privilege_arrives_and_leaves_with_the_first_revision(
    corpus: Corpus,
) -> None:
    """The grant is a migration's business, and so is taking it back.

    At head, a table created after the migrations is readable: that is
    ``ALTER DEFAULT PRIVILEGES`` doing its job for every revision that comes
    later. Taken back to base, the same statement produces a table the reader
    cannot touch. A grant that survived the downgrade would be a privilege
    nobody can point at a line for.
    """
    execute(corpus.sync_url, "CREATE TABLE probe_after_head (id integer)")
    assert scalar(corpus.read_url, "SELECT count(*) FROM probe_after_head") == 0

    downgrade_to(corpus.sync_url, "base")
    execute(corpus.sync_url, "CREATE TABLE probe_after_base (id integer)")

    with pytest.raises(sa.exc.ProgrammingError) as caught:
        scalar(corpus.read_url, "SELECT count(*) FROM probe_after_base")
    assert "permission denied" in str(caught.value)

    execute(corpus.sync_url, "DROP TABLE probe_after_base")
    execute(corpus.sync_url, "DROP TABLE probe_after_head")


def test_migrating_an_instance_that_was_never_provisioned_stops_and_names_the_script(
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure mode this replaces is silence.

    Without the reader role, every statement in the chain still succeeds, the
    sync fills the corpus, and the server sees an empty mirror for as long as
    nobody thinks to check ``pg_roles``. Raising here converts that into a
    deployment that stops with the file name it needs.
    """
    monkeypatch.setenv("AGENT_KNOWLEDGE_READ_ROLE", "knowledge_read_absent_role")
    downgrade_to(corpus.sync_url, "base")

    with pytest.raises(sa.exc.SQLAlchemyError) as caught:
        upgrade_to(corpus.sync_url, "head")

    assert "knowledge_db_init.sql" in str(caught.value)
    # The revision runs in its own transaction, so the half that got as far as
    # creating the marker table is gone with it.
    assert not table_exists(corpus.sync_url, "schema_meta")

    monkeypatch.undo()
    migrate(corpus.sync_url)
    assert schema_version(corpus.sync_url) == max(SUPPORTED_SCHEMA_VERSIONS)
