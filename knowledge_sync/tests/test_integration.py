"""A first run end to end, and the flags without which it quietly indexes nothing.

The network is faked and nothing above it, so the request the client builds is under test.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveTokenProvider
from agent_control_knowledge_sync.drive_client import DriveClient, DriveRootUnreachableError
from agent_control_knowledge_sync.sync import SyncFailedError, run_once

from tests.conftest import query, scalar
from tests.fakes.drive import FakeDrive
from tests.integration_support import (
    CREDENTIALS,
    LAPTOPS,
    LAPTOPS_REVISED,
    ROOT_ID,
    config_for,
    document,
    populate,
    source_row,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def config(corpus: Any) -> SyncConfig:
    return config_for(corpus)


# --- the first run ----------------------------------------------------------


async def test_a_first_run_indexes_the_whole_subtree(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    populate(drive)

    counters = await run_once(config)

    assert (counters.indexed, counters.unchanged) == (3, 0)
    assert (counters.tombstoned, counters.refused) == (0, 0)
    assert counters.seen == 3
    titles = {row["title"] for row in query(corpus, "SELECT title FROM documents")}
    assert titles == {"laptops.md", "phones.md", "releases.md"}
    assert scalar(corpus, "SELECT count(*) FROM chunks") >= 3


async def test_a_first_run_writes_the_columns_retrieval_reads(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    """The optional columns are the ones that go missing quietly."""
    populate(drive)

    await run_once(config)

    row = document(corpus, "file-laptops")
    assert len(row["content_sha256"]) == 64
    assert row["bytes"] == len(LAPTOPS.encode("utf-8"))
    assert row["source_modified_at"] is not None
    assert row["synced_at"] is not None
    assert row["tombstoned_at"] is None
    # Drive names no owner on a file the reader can only read, so 'unknown' is
    # the honest value and 'workspace' would be a trust claim nothing backs.
    assert row["author_kind"] == "unknown"


async def test_a_nested_document_keeps_its_folder_in_its_path(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    """A citation is checkable in one click or it is not provenance at all."""
    populate(drive)

    await run_once(config)

    assert "Onboarding" in document(corpus, "file-releases")["path"]


async def test_a_path_is_the_whole_chain_from_the_corpus_root_down(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    """`knowledge_render` prints this string alone, so it has to stand on its own."""
    populate(drive)
    await run_once(config)

    assert document(corpus, "file-laptops")["path"] == "Company Knowledge/laptops.md"
    assert document(corpus, "file-releases")["path"] == "Company Knowledge/Onboarding/releases.md"

    drive.markdown("file-laptops", "laptops.md", LAPTOPS_REVISED, ROOT_ID)
    drive.set_changes("t1", changed=("file-laptops",), new_token="t2")
    await run_once(config)

    assert document(corpus, "file-laptops")["path"] == "Company Knowledge/laptops.md"


async def test_a_first_run_registers_the_source_with_its_cursor(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    populate(drive)

    await run_once(config)

    source = source_row(corpus)
    assert (source["kind"], source["ref"]) == ("drive_folder", ROOT_ID)
    # 4.2 spells the Drive cursor {"start_page_token": ...}; the status endpoint
    # and the console panel both read this column by that name.
    assert source["cursor"] == {"start_page_token": "t1"}
    assert source["last_verified_at"] is not None
    assert source["last_run_status"] == "ok"


# --- the shared-drive flags, and the 404 they otherwise hide -----------------


async def test_every_drive_request_carries_supports_all_drives(
    drive: FakeDrive, config: SyncConfig
) -> None:
    """Without it `files.get` on the root is 404 and `files.list` is empty, silently."""
    populate(drive)

    await run_once(config)

    assert drive.drive_requests(), "the run made no Drive calls at all"
    missing = [
        str(request.url)
        for request in drive.drive_requests()
        if request.url.params.get("supportsAllDrives") != "true"
    ]
    assert missing == []


async def test_every_list_request_asks_for_items_from_all_drives(
    drive: FakeDrive, config: SyncConfig
) -> None:
    populate(drive)

    await run_once(config)

    assert drive.list_requests(), "the run listed nothing"
    missing = [
        str(request.url)
        for request in drive.list_requests()
        if request.url.params.get("includeItemsFromAllDrives") != "true"
    ]
    assert missing == []


async def test_a_root_the_reader_cannot_see_is_refused_rather_than_resolved(
    drive: FakeDrive, config: SyncConfig
) -> None:
    populate(drive)
    drive.unreachable.add(ROOT_ID)
    client = httpx.AsyncClient(transport=drive.transport())
    tokens = DriveTokenProvider(credentials=CREDENTIALS, client=client)

    try:
        with pytest.raises(DriveRootUnreachableError) as caught:
            await DriveClient(tokens, client, config).resolve_root()
    finally:
        await client.aclose()

    assert ROOT_ID in str(caught.value)


async def test_a_run_against_an_unreachable_root_does_not_report_success(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    """A 404 root and an empty folder look identical; one of them may look like success."""
    populate(drive)
    drive.unreachable.add(ROOT_ID)

    with pytest.raises(SyncFailedError):
        await run_once(config)

    assert scalar(corpus, "SELECT count(*) FROM documents") == 0
    assert scalar(corpus, "SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1") == "failed"
