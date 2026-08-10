"""One `once` run, end to end: a stubbed Drive transport and a real corpus.

What is faked here is the network and nothing above it, so the URLs and query
parameters the client builds are under test rather than mocked away. That is
the point of three of these: 5.7 measured that a Drive call missing
``supportsAllDrives`` returns 404 or zero rows *and no error*, which is the
same shape an unshared folder produces, so the flags are asserted per request
and a root that will not resolve is asserted to refuse rather than to sync
nothing and call it a success.

The rest are the properties a mid-run death depends on: a replay writes
nothing twice, a failed batch leaves the cursor where it was, a removal
tombstones, a quiet source still stamps its clock, and a second process does
not walk behind the first one's back. None of them can be seen from inside a
single module's unit tests, which is why they live here.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync import drive_transport as drive_transport_module
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials, DriveTokenProvider
from agent_control_knowledge_sync.drive_client import DriveClient, DriveRootUnreachableError
from agent_control_knowledge_sync.journal import SyncJournal
from agent_control_knowledge_sync.lease import LeaseHeldError, mint_token
from agent_control_knowledge_sync.sync import SyncFailedError, run_once
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import execute, query, scalar
from tests.fakes.drive import DOCUMENT_MIME, FakeDrive, FakeFile

pytestmark = pytest.mark.asyncio

ROOT_ID = "root-folder-1"
NESTED_ID = "nested-folder-1"

CREDENTIALS = DriveCredentials(
    client_id="123456789012-abcdefghijklmnop.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)

LAPTOPS = """# Onboarding

## Laptops

Laptops are reimbursed up to 1500 GBP. Submit the receipt within thirty days of
purchase and the finance team pays it in the next run. A replacement machine is
approved by the hiring manager, never by the agent, and the asset register is
updated by IT the same week the machine arrives on somebody's desk.
"""

LAPTOPS_REVISED = (
    LAPTOPS
    + """
## Loaners

A loaner is signed out at the front desk for up to two weeks. Anything longer
is a replacement and goes through the hiring manager like any other machine.
"""
)

PHONES = """# Onboarding

## Phones

Company phones are issued to staff who are on call. The monthly allowance is
forty pounds and it is claimed on the same expense form as travel. Handsets are
returned when somebody leaves the on-call rotation, and the finance team closes
the line in the same week rather than at the end of the quarter.
"""

PHONES_REVISED = PHONES.replace("forty pounds", "fifty pounds")

RELEASES = """# Release process

Releases ship on Thursdays. The release manager freezes the branch on Wednesday
afternoon, runs the full suite, and posts the changelog to the engineering
channel before anybody merges anything else into the release branch. A hotfix
is the one exception and it is announced in the same channel before it lands.
"""

EXPENSES = """# Expenses

## Travel

Trains are booked in advance and standard class. Anything over three hundred
pounds is approved by a manager before it is booked, and the receipt is filed
within thirty days like every other claim in this handbook.
"""


async def _no_wait(seconds: float) -> None:
    """The retry backoff, without the wall clock."""


def populate(drive: FakeDrive) -> None:
    """The three-file subtree every run in here walks."""
    drive.folder(ROOT_ID, "Company Knowledge")
    drive.folder(NESTED_ID, "Onboarding", ROOT_ID)
    drive.markdown("file-laptops", "laptops.md", LAPTOPS, ROOT_ID)
    drive.markdown("file-phones", "phones.md", PHONES, ROOT_ID)
    drive.markdown("file-releases", "releases.md", RELEASES, NESTED_ID)


@pytest.fixture()
def config(corpus: Any) -> SyncConfig:
    return SyncConfig(
        credentials=CREDENTIALS,
        root_folder_id=ROOT_ID,
        database_url=corpus.sync_url,
        max_file_bytes=1_000_000,
        max_documents_per_run=50,
        request_timeout_seconds=5.0,
    )


def source_row(corpus: Any) -> dict[str, Any]:
    rows = query(corpus, "SELECT * FROM sources WHERE ref = :ref", ref=ROOT_ID)
    assert rows, "the run registered no source for the root folder"
    return dict(rows[0])


def document(corpus: Any, external_id: str) -> dict[str, Any]:
    rows = query(corpus, "SELECT * FROM documents WHERE external_id = :id", id=external_id)
    assert rows, f"no document row for {external_id}"
    return dict(rows[0])


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
    """A citation is checkable in one click or it is not provenance at all.

    4.2's own example path is ``Ops Handbook/Onboarding/laptops.md``, and 4.3
    rests the whole heading-bounded-chunk argument on a person being able to
    find the source from what the fence header says. A bare filename does not
    survive two folders holding a ``notes.md`` each.
    """
    populate(drive)

    await run_once(config)

    assert "Onboarding" in document(corpus, "file-releases")["path"]


async def test_a_path_is_the_whole_chain_from_the_corpus_root_down(
    drive: FakeDrive, config: SyncConfig, corpus: Any
) -> None:
    """`knowledge_render` prints this string alone, so it has to stand on its own.

    A root-level file is the case a folder-only path cannot distinguish: it
    would store as a bare filename and cite like one. The replay writes the
    same string as the walk, or a document's citation changes the day it is
    edited.
    """
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
    """A 503 on the parent walk read as `moved out of the corpus` and deleted the chunks.

    Nothing about the document changed and nothing about its sharing changed.
    Drive was briefly unwell, and the mirror lost a live policy until somebody
    happened to edit it, because there is no repair in this phase.
    """
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
    """The sweep closes orphans. The process making it is the one known to be alive.

    Unfenced, a sync that renewed late enough to keep its lease still marked
    its own run row `lapsed`, and section 5.5's premise is that two never race.
    """
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
    """A count alone cannot tell a ceiling hit from a corpus that is simply that big.

    Called truncated, it stores no cursor, walks in full every run and reports
    `partial`/`source_ceiling` forever with nothing an operator can fix.
    """
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
