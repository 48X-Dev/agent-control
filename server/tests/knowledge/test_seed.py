"""What the seed helper writes, and what the engine refuses.

The seed helper is the only thing in the knowledge package that writes, and it
is also an executable statement of the write contract Phase 2 has to honour. A
sync producing different rows than these would be producing rows retrieval was
never tested against.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from agent_control_server.config import KnowledgeSettings
from agent_control_server.knowledge import (
    KnowledgeUnavailableError,
    knowledge_session,
    read_schema_version,
)
from agent_control_server.knowledge.seed import SeedDocument, seed_corpus
from sqlalchemy.pool import NullPool

from tests.knowledge.support import LAPTOPS
from tests.knowledge_provisioning import READ_PASSWORD, READ_ROLE, Corpus

# --- The seed helper's own contract -----------------------------------------


def test_a_chunk_carrying_a_credential_shape_is_dropped_and_counted(corpus: Corpus) -> None:
    body = (
        "# Deploy\n\n"
        + ("The release manager runs the deploy script on Thursday. " * 6)
        + "\n\n## Credentials\n\n"
        + ("The staging key is sk-abcdefghijklmnopqrstuvwx and it rotates monthly. " * 5)
    )
    result = seed_corpus(
        corpus.sync_url,
        source_ref="repo",
        source_name="agent-control",
        docs=[SeedDocument(path="agent-control/docs/deploy.md", body=body)],
    )

    assert result.secrets_skipped == 1
    assert result.chunks_written >= 1


def test_a_file_that_is_a_credential_by_name_is_tombstoned_whole(corpus: Corpus) -> None:
    result = seed_corpus(
        corpus.sync_url,
        source_ref="repo",
        source_name="agent-control",
        docs=[SeedDocument(path="agent-control/.env", body="TOKEN=abc\n")],
    )

    assert result.files_skipped == 1
    assert result.chunks_written == 0

    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            reason = conn.execute(sa.text("SELECT tombstone_reason FROM documents")).scalar_one()
    finally:
        engine.dispose()
    assert reason == "secret_file"


def test_index_time_normalization_reaches_the_stored_row(corpus: Corpus) -> None:
    seed_corpus(
        corpus.sync_url,
        source_ref="ops-handbook",
        source_name="Ops Handbook",
        docs=[SeedDocument(path="Ops Handbook/report‮fdp.exe.md", body=LAPTOPS)],
    )

    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            path, title = conn.execute(sa.text("SELECT path, title FROM documents")).one()
    finally:
        engine.dispose()

    assert path == "Ops Handbook/reportfdp.exe.md"
    assert title == "reportfdp.exe.md"


# --- Refusals that need no database -----------------------------------------


async def test_a_disabled_server_refuses_by_name() -> None:
    settings = KnowledgeSettings(enabled=False, db_url="postgresql+psycopg://x:y@localhost/z")

    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(settings):
            pass

    assert caught.value.code == "knowledge_disabled"


async def test_an_enabled_server_with_no_dsn_refuses_by_name() -> None:
    settings = KnowledgeSettings(enabled=True, db_url=None)

    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(settings):
            pass

    assert caught.value.code == "knowledge_unavailable"


async def test_an_unreachable_corpus_refuses_without_leaking_the_driver_message() -> None:
    settings = KnowledgeSettings(
        enabled=True,
        db_url=f"postgresql+psycopg://{READ_ROLE}:{READ_PASSWORD}@127.0.0.1:1/no_such_db",
        connect_timeout_seconds=1,
    )

    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(settings) as session:
            await read_schema_version(session)

    assert caught.value.code == "knowledge_unavailable"
    assert "psycopg" not in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)
