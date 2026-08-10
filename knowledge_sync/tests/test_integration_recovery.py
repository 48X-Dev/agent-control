"""What a mid-run death has to survive, and the two source species under the ceiling.

None of these can be seen from inside one module's unit tests, which is why they live here.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync import drive_transport as drive_transport_module
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.journal import SyncJournal
from agent_control_knowledge_sync.lease import LeaseHeldError, mint_token
from agent_control_knowledge_sync.sync import run_once
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import execute, query, scalar
from tests.fakes.drive import DOCUMENT_MIME, FakeDrive, FakeFile
from tests.integration_support import (
    CREDENTIALS,
    EXPENSES,
    LAPTOPS,
    LAPTOPS_REVISED,
    PHONES,
    PHONES_REVISED,
    ROOT_ID,
    _no_wait,
    config_for,
    document,
    populate,
    source_row,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def config(corpus: Any) -> SyncConfig:
    return config_for(corpus)


# --- what makes a mid-run death survivable ----------------------------------


async def test_replaying_the_same_batch_indexes_nothing_twice(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    populate(drive)
    drive.set_changes(
        "t1", changed=("file-laptops", "file-phones", "file-releases"), new_token="t2"
    )

    first = await run_once(config)
    high_water = scalar(corpus, "SELECT max(id) FROM chunks")
    second = await run_once(config)

    assert first.indexed == 3
    assert (second.indexed, second.unchanged) == (0, 3)
    assert scalar(corpus, "SELECT count(*) FROM documents") == 3
    assert scalar(corpus, "SELECT max(id) FROM chunks") == high_water


async def test_a_failed_batch_leaves_the_cursor_where_it_was(
    drive: FakeDrive, config: SyncConfig, corpus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cursor past unindexed rows is the one loss a replay cannot repair."""
    monkeypatch.setattr(drive_transport_module, "_sleep", _no_wait)
    populate(drive)
    await run_once(config)
    before = source_row(corpus)
    stale_hash = document(corpus, "file-laptops")["content_sha256"]
    drive.markdown("file-laptops", "laptops.md", LAPTOPS_REVISED, ROOT_ID)
    drive.markdown("file-phones", "phones.md", PHONES_REVISED, ROOT_ID)
    drive.set_changes("t1", changed=("file-laptops", "file-phones"), new_token="t2")
    drive.media_failures["file-phones"] = 503

    with contextlib.suppress(Exception):
        await run_once(config)

    after = source_row(corpus)
    # The first document committed, which is what makes this mid-batch rather
    # than a run that fell over before it started.
    assert document(corpus, "file-laptops")["content_sha256"] != stale_hash
    assert after["cursor"] == before["cursor"]
    assert after["cursor_advanced_at"] == before["cursor_advanced_at"]


async def test_a_removal_in_the_changes_feed_tombstones_the_document(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    populate(drive)
    await run_once(config)
    drive.set_changes("t1", removed=("file-phones",), new_token="t2")

    counters = await run_once(config)

    assert counters.tombstoned == 1
    row = document(corpus, "file-phones")
    assert row["tombstoned_at"] is not None
    assert row["tombstone_reason"] == "deleted"
    assert (
        scalar(
            corpus,
            "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.external_id = :id",
            id="file-phones",
        )
        == 0
    )


async def test_a_zero_change_run_still_stamps_last_verified_at(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    """A quiet source is not a dead sync, and the staleness clock keys on this."""
    populate(drive)
    await run_once(config)
    before = source_row(corpus)
    drive.set_changes("t1", new_token="t1")

    await run_once(config)

    after = source_row(corpus)
    assert after["last_verified_at"] > before["last_verified_at"]
    # Section 10 splits the two on purpose: the cursor did not move, so the
    # column that records when it last moved must not move either.
    assert after["cursor_advanced_at"] == before["cursor_advanced_at"]


async def test_a_document_survives_a_parent_walk_drive_would_not_answer(
    drive: FakeDrive, config: SyncConfig, corpus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 503 on the parent walk read as `moved out of the corpus` and deleted the chunks."""
    monkeypatch.setattr(drive_transport_module, "_sleep", _no_wait)
    populate(drive)
    await run_once(config)
    before = document(corpus, "file-laptops")
    drive.set_changes("t1", changed=("file-laptops",), new_token="t2")
    answer = drive.handle

    def parents_are_down(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("fields") == "id,name,parents":
            return httpx.Response(503, json={"error": {"code": 503, "message": "Backend Error"}})
        return answer(request)

    monkeypatch.setattr(drive, "handle", parents_are_down)

    counters = await run_once(config)

    assert counters.tombstoned == 0
    assert counters.refusals_by_code == {"location_unknown": 1}
    row = document(corpus, "file-laptops")
    assert row["tombstoned_at"] is None
    assert row["content_sha256"] == before["content_sha256"]
    assert (
        scalar(
            corpus,
            "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.external_id = :id",
            id="file-laptops",
        )
        > 0
    )


async def test_a_running_row_belonging_to_the_current_holder_is_not_lapsed(corpus: Any) -> None:
    """The sweep closes orphans. The process making it is the one known to be alive."""
    engine = create_async_engine(corpus.sync_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        journal = SyncJournal(sessions)
        holder = mint_token()
        orphan = await journal.open_run("a-holder-whose-process-died")
        mine = await journal.open_run(holder)

        lapsed = await journal.lapse_orphans(holder)
    finally:
        await engine.dispose()

    assert lapsed == 1
    rows = {row["id"]: row["status"] for row in query(corpus, "SELECT id, status FROM sync_runs")}
    assert rows[mine] == "running"
    assert rows[orphan] == "lapsed"


async def test_a_held_lease_stops_the_second_run_from_walking(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    populate(drive)
    execute(
        corpus,
        "UPDATE sync_lease SET holder = 'somebody-else', "
        "lease_expires_at = now() + interval '30 minutes' WHERE id = 1",
    )

    with pytest.raises(LeaseHeldError):
        await run_once(config)

    assert drive.drive_requests() == []
    assert scalar(corpus, "SELECT count(*) FROM documents") == 0


# --- the two source species, and the ceiling --------------------------------


async def test_a_google_doc_is_exported_rather_than_downloaded(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    """Native files have no bytes to download; `files.export` is the only path."""
    drive.folder(ROOT_ID, "Company Knowledge")
    drive.add(
        FakeFile(
            id="file-doc",
            name="Expenses policy",
            parents=(ROOT_ID,),
            mime_type=DOCUMENT_MIME,
            exports={"text/markdown": EXPENSES.encode("utf-8")},
        )
    )

    counters = await run_once(config)

    assert counters.indexed == 1
    exports = [r for r in drive.drive_requests() if r.url.path.endswith("/export")]
    assert [r.url.params.get("mimeType") for r in exports] == ["text/markdown"]
    bodies = " ".join(row["body"] for row in query(corpus, "SELECT body FROM chunks"))
    assert "standard class" in bodies


async def test_a_corpus_exactly_the_size_of_the_ceiling_stores_its_cursor(
    drive: FakeDrive, corpus: Any
) -> None:
    """A count alone cannot tell a ceiling hit from a corpus that is simply that big."""
    drive.folder(ROOT_ID, "Company Knowledge")
    drive.markdown("file-laptops", "laptops.md", LAPTOPS, ROOT_ID)
    drive.markdown("file-phones", "phones.md", PHONES, ROOT_ID)
    config = SyncConfig(
        credentials=CREDENTIALS,
        root_folder_id=ROOT_ID,
        database_url=corpus.sync_url,
        max_file_bytes=1_000_000,
        max_documents_per_run=2,
        request_timeout_seconds=5.0,
    )

    counters = await run_once(config)

    assert counters.indexed == 2
    source = source_row(corpus)
    assert source["cursor"] == {"start_page_token": "t1"}
    assert source["last_run_status"] == "ok"
    assert scalar(corpus, "SELECT error_code FROM sync_runs ORDER BY id DESC LIMIT 1") is None


async def test_a_file_over_the_ceiling_is_refused_and_counted(
    drive: FakeDrive, corpus: Any
) -> None:
    """An invisible half-mirror is the failure; a counted refusal is the fix."""
    drive.folder(ROOT_ID, "Company Knowledge")
    drive.markdown("file-laptops", "laptops.md", LAPTOPS, ROOT_ID)
    drive.markdown("file-huge", "huge.md", "# Huge\n\n" + "oversized " * 4000, ROOT_ID)
    config = SyncConfig(
        credentials=CREDENTIALS,
        root_folder_id=ROOT_ID,
        database_url=corpus.sync_url,
        max_file_bytes=2_000,
        max_documents_per_run=50,
        request_timeout_seconds=5.0,
    )

    counters = await run_once(config)

    assert (counters.indexed, counters.refused) == (1, 1)
    assert counters.refusals_by_code == {"oversize": 1}
    bodies = " ".join(row["body"] for row in query(corpus, "SELECT body FROM chunks"))
    assert "oversized" not in bodies
