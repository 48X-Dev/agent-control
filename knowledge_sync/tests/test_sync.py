"""The orchestration, against stubbed collaborators and no database.

Two properties are the reason this file exists. The cursor advances only after
its batch has committed, so a death mid-run replays at most one batch; and every
completed run stamps ``last_verified_at`` even when nothing changed, because
plan section 10 keys staleness on that column and a quiet source is not a dead
sync. Everything else here is counting, and counting is the only thing that
makes a refusal visible instead of silent.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials
from agent_control_knowledge_sync.drive_client import (
    DriveChange,
    DriveItem,
    DriveRefusalError,
    DriveRefusalRecord,
    DriveRootUnreachableError,
    FetchedContent,
    FolderLocation,
    LocationUnknown,
    OutsideRoot,
    UnderRoot,
)
from agent_control_knowledge_sync.ingest import (
    REFUSAL_TOMBSTONES,
    IngestOutcome,
    TombstoneReason,
)
from agent_control_knowledge_sync.sync import (
    LEASE_RENEW_EVERY,
    RunCounters,
    SyncFailedError,
    run_once_with,
)

ROOT_ID = "folder-root"

ROOT = DriveItem(
    id=ROOT_ID,
    name="Company Knowledge",
    mime_type="application/vnd.google-apps.folder",
    modified_time=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    size=None,
    md5_checksum=None,
    trashed=False,
    shortcut_target_id=None,
)


def _item(file_id: str, name: str = "Handbook.pdf") -> DriveItem:
    return DriveItem(
        id=file_id,
        name=name,
        mime_type="application/pdf",
        modified_time=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
        size=1024,
        md5_checksum="abc",
        trashed=False,
        shortcut_target_id=None,
    )


def _config(**overrides: Any) -> SyncConfig:
    fields: dict[str, Any] = {
        "credentials": DriveCredentials(client_id="id", client_secret="s", refresh_token="r"),
        "root_folder_id": ROOT_ID,
        "database_url": "postgresql+psycopg://knowledge_sync:x@localhost/agent_knowledge",
    }
    fields.update(overrides)
    return SyncConfig(**fields)


class FakeClient:
    """Every Drive call the runner makes, logged in the order it made them."""

    def __init__(
        self,
        *,
        items: list[DriveItem] | None = None,
        changes: list[DriveChange] | None = None,
        new_cursor: str = "cursor-2",
        content: dict[str, bytes | Exception] | None = None,
        locations: dict[str, FolderLocation] | None = None,
        root: DriveItem | Exception = ROOT,
        walk_truncated: bool = False,
    ) -> None:
        self.items = items or []
        self.changes = changes or []
        self.new_cursor = new_cursor
        self.content = content or {}
        self.locations = locations or {}
        self.root = root
        self.walk_truncated = walk_truncated
        self.refusals: list[DriveRefusalRecord] = []
        self.events: list[str] = []

    async def resolve_root(self) -> DriveItem:
        self.events.append("resolve_root")
        if isinstance(self.root, Exception):
            raise self.root
        return self.root

    async def start_cursor(self) -> str:
        self.events.append("start_cursor")
        return "cursor-1"

    async def walk_subtree(self) -> AsyncIterator[DriveItem]:
        for item in self.items:
            self.events.append(f"walk:{item.id}")
            yield item

    async def list_changes(self, cursor: str) -> tuple[list[DriveChange], str]:
        self.events.append(f"list_changes:{cursor}")
        return self.changes, self.new_cursor

    async def resolve_folder_path(self, file_id: str) -> FolderLocation:
        self.events.append(f"folder_path:{file_id}")
        return self.locations.get(file_id, UnderRoot(()))

    async def fetch_content(self, item: DriveItem) -> FetchedContent:
        self.events.append(f"fetch:{item.id}")
        answer = self.content.get(item.id, b"body")
        if isinstance(answer, Exception):
            raise answer
        return FetchedContent(answer, item.mime_type)


class FakeIngestor:
    """Records what reached it and answers whatever the test set up."""

    def __init__(
        self,
        *,
        outcomes: dict[str, IngestOutcome] | None = None,
        tombstones: dict[str, bool] | None = None,
        secrets_skipped: int = 0,
    ) -> None:
        self.outcomes = outcomes or {}
        self.tombstones = tombstones or {}
        self.secrets_skipped = secrets_skipped
        self.events: list[str] = []

    async def ingest(self, item: DriveItem, content: FetchedContent) -> IngestOutcome:
        self.events.append(f"ingest:{item.id}")
        return self.outcomes.get(item.id, IngestOutcome("1", 3, False, None))

    async def tombstone(self, external_id: str, *, reason: str = TombstoneReason.DELETED) -> bool:
        self.events.append(f"tombstone:{external_id}:{reason}")
        return self.tombstones.get(external_id, True)

    async def refuse_fetch(self, external_id: str, code: str) -> bool:
        reason = REFUSAL_TOMBSTONES.get(code)
        if reason is None:
            return False
        self.events.append(f"refuse_fetch:{external_id}:{reason}")
        return self.tombstones.get(external_id, True)


class FakeJournal:
    """The corpus rows, as an ordered event log rather than a database."""

    def __init__(self, *, cursor: str | None = None, schema_error: Exception | None = None) -> None:
        self.cursor = cursor
        self.schema_error = schema_error
        self.events: list[tuple[str, Any]] = []

    async def assert_schema(self) -> int:
        self.events.append(("assert_schema", None))
        if self.schema_error is not None:
            raise self.schema_error
        return 3

    async def lapse_orphans(self, holder: str) -> int:
        self.events.append(("lapse_orphans", holder))
        return 0

    async def open_run(self, holder: str) -> int:
        self.events.append(("open_run", holder))
        return 7

    async def close_run(
        self, run_id: int, *, status: str, counters: RunCounters, tally: Any, error_code: str | None
    ) -> None:
        self.events.append(("close_run", (status, error_code, counters)))

    async def ensure_source(self, *, ref: str, display_name: str) -> Any:
        self.events.append(("ensure_source", (ref, display_name)))
        from agent_control_knowledge_sync.sync import SourceState

        return SourceState(id=11, cursor=self.cursor)

    async def advance_cursor(self, source_id: int, cursor: str) -> None:
        self.events.append(("advance_cursor", cursor))

    async def mark_verified(self, source_id: int, *, status: str, error_code: str | None) -> None:
        self.events.append(("mark_verified", (status, error_code)))

    async def mark_source_failed(self, *, ref: str, error_code: str) -> None:
        self.events.append(("mark_source_failed", error_code))


class FakeLease:
    def __init__(self, *, renews: bool = True) -> None:
        self.holder = "run-token"
        self.renews = renews
        self.renewals = 0

    async def renew(self) -> bool:
        self.renewals += 1
        return self.renews


async def _run(
    client: FakeClient,
    journal: FakeJournal,
    ingestor: FakeIngestor,
    *,
    lease: FakeLease | None = None,
    config: SyncConfig | None = None,
) -> RunCounters:
    return await run_once_with(
        config or _config(),
        client=client,  # type: ignore[arg-type]
        journal=journal,  # type: ignore[arg-type]
        lease=lease or FakeLease(),  # type: ignore[arg-type]
        ingestor_factory=lambda _: ingestor,  # type: ignore[arg-type,return-value]
    )


def _kinds(journal: FakeJournal) -> list[str]:
    return [name for name, _ in journal.events]


@pytest.mark.asyncio
async def test_a_first_run_takes_the_cursor_before_it_walks() -> None:
    """A file changed between the two would otherwise be missed by both."""
    client = FakeClient(items=[_item("f1"), _item("f2")])
    journal = FakeJournal(cursor=None)

    counters = await _run(client, journal, FakeIngestor())

    assert client.events.index("start_cursor") < client.events.index("walk:f1")
    assert counters.seen == 2
    assert counters.indexed == 2
    assert ("advance_cursor", "cursor-1") in journal.events


@pytest.mark.asyncio
async def test_the_cursor_advances_only_after_the_batch_committed() -> None:
    """A death before this point replays one batch; a death after it loses one."""
    client = FakeClient(changes=[DriveChange("f1", False, _item("f1"))])
    journal = FakeJournal(cursor="cursor-1")
    ingestor = FakeIngestor()

    await _run(client, journal, ingestor)

    assert ingestor.events == ["ingest:f1"]
    assert _kinds(journal).index("advance_cursor") > _kinds(journal).index("ensure_source")
    assert ("advance_cursor", "cursor-2") in journal.events


@pytest.mark.asyncio
async def test_a_zero_change_run_still_stamps_last_verified_at() -> None:
    """Section 10: a quiet source is not a dead sync, and staleness reads this."""
    client = FakeClient(changes=[])
    journal = FakeJournal(cursor="cursor-1")

    counters = await _run(client, journal, FakeIngestor())

    assert counters == RunCounters(0, 0, 0, 0, 0, {})
    assert ("mark_verified", ("ok", None)) in journal.events


@pytest.mark.asyncio
async def test_a_cursor_that_came_back_unchanged_is_not_stamped_as_advanced() -> None:
    """A poll that moved nothing verified the source; `cursor_advanced_at` says moved."""
    client = FakeClient(changes=[], new_cursor="cursor-1")
    journal = FakeJournal(cursor="cursor-1")

    await _run(client, journal, FakeIngestor())

    assert "advance_cursor" not in _kinds(journal)
    assert ("mark_verified", ("ok", None)) in journal.events


@pytest.mark.asyncio
async def test_a_removal_is_tombstoned_and_counted() -> None:
    client = FakeClient(changes=[DriveChange("gone", True, None)])
    journal = FakeJournal(cursor="cursor-1")
    ingestor = FakeIngestor()

    counters = await _run(client, journal, ingestor)

    assert ingestor.events == ["tombstone:gone:deleted"]
    assert counters.tombstoned == 1
    assert counters.seen == 1


@pytest.mark.asyncio
async def test_a_document_moved_out_of_the_root_is_tombstoned_too() -> None:
    """The feed never flags it removed: it is still readable, just not corpus."""
    client = FakeClient(
        changes=[DriveChange("moved", False, _item("moved"))], locations={"moved": OutsideRoot()}
    )
    journal = FakeJournal(cursor="cursor-1")
    ingestor = FakeIngestor()

    counters = await _run(client, journal, ingestor)

    assert ingestor.events == ["tombstone:moved:excluded"]
    assert counters.tombstoned == 1


@pytest.mark.asyncio
async def test_an_unrelated_change_outside_the_root_is_not_even_seen() -> None:
    """The account is a destination for shares; they must not inflate the counters."""
    client = FakeClient(
        changes=[DriveChange("other", False, _item("other"))], locations={"other": OutsideRoot()}
    )
    journal = FakeJournal(cursor="cursor-1")
    ingestor = FakeIngestor(tombstones={"other": False})

    counters = await _run(client, journal, ingestor)

    assert counters == RunCounters(0, 0, 0, 0, 0, {})


@pytest.mark.asyncio
async def test_a_location_drive_would_not_answer_refuses_rather_than_tombstoning() -> None:
    """The document is live; a 503 on its parent walk must not delete its chunks.

    Only a confirmed absence tombstones. A failure to determine location is a
    refusal, counted under its own name, and the stored document is untouched.
    """
    client = FakeClient(
        changes=[DriveChange("live", False, _item("live"))],
        locations={"live": LocationUnknown("location_unknown", "Drive answered HTTP 503")},
    )
    journal = FakeJournal(cursor="cursor-1")
    ingestor = FakeIngestor()

    counters = await _run(client, journal, ingestor)

    assert ingestor.events == []
    assert counters.tombstoned == 0
    assert counters.refusals_by_code == {"location_unknown": 1}
    assert counters.seen == 1


@pytest.mark.asyncio
async def test_a_refused_fetch_is_counted_by_code_and_the_run_continues() -> None:
    client = FakeClient(
        items=[_item("big"), _item("ok")],
        content={"big": DriveRefusalError("oversize", "over the ceiling")},
    )
    journal = FakeJournal(cursor=None)

    counters = await _run(client, journal, FakeIngestor())

    assert counters.refused == 1
    assert counters.refusals_by_code == {"oversize": 1}
    assert counters.indexed == 1


@pytest.mark.asyncio
async def test_an_ingest_refusal_carries_its_own_code() -> None:
    client = FakeClient(items=[_item("scan")])
    journal = FakeJournal(cursor=None)
    ingestor = FakeIngestor(
        outcomes={"scan": IngestOutcome(None, 0, False, "conversion_failed")},
    )

    counters = await _run(client, journal, ingestor)

    assert counters.refusals_by_code == {"conversion_failed": 1}
    assert counters.indexed == 0


@pytest.mark.asyncio
async def test_refusals_the_walk_survived_are_folded_in_rather_than_lost() -> None:
    client = FakeClient(items=[_item("f1")])
    client.refusals.append(DriveRefusalRecord("f9", "Secrets", "unreadable_folder", "403"))
    journal = FakeJournal(cursor=None)

    counters = await _run(client, journal, FakeIngestor())

    assert counters.refusals_by_code == {"unreadable_folder": 1}


@pytest.mark.asyncio
async def test_an_unchanged_document_is_counted_apart_from_an_indexed_one() -> None:
    client = FakeClient(items=[_item("same"), _item("new")])
    journal = FakeJournal(cursor=None)
    ingestor = FakeIngestor(outcomes={"same": IngestOutcome("3", 0, True, None)})

    counters = await _run(client, journal, ingestor)

    assert counters.unchanged == 1
    assert counters.indexed == 1
    assert counters.seen == 2


@pytest.mark.asyncio
async def test_an_unreachable_root_aborts_and_does_not_stamp_verified() -> None:
    """A root that did not resolve reads exactly like a successful empty sync."""
    client = FakeClient(root=DriveRootUnreachableError("the folder is not shared"))
    journal = FakeJournal(cursor="cursor-1")

    with pytest.raises(SyncFailedError) as caught:
        await _run(client, journal, FakeIngestor())

    assert caught.value.code == "root_unreachable"
    assert "mark_verified" not in _kinds(journal)
    assert ("mark_source_failed", "root_unreachable") in journal.events
    assert ("close_run", ("failed", "root_unreachable", RunCounters(0, 0, 0, 0, 0, {}))) in (
        journal.events
    )


@pytest.mark.asyncio
async def test_a_truncated_first_walk_leaves_the_cursor_unset() -> None:
    """Storing it would strand everything the walk never reached."""
    client = FakeClient(items=[_item("f1"), _item("f2")], walk_truncated=True)
    journal = FakeJournal(cursor=None)

    counters = await _run(client, journal, FakeIngestor(), config=_config(max_documents_per_run=2))

    assert "advance_cursor" not in _kinds(journal)
    assert ("mark_verified", ("partial", "source_ceiling")) in journal.events
    assert counters.indexed == 2


@pytest.mark.asyncio
async def test_a_walk_that_ended_on_its_own_stores_its_cursor() -> None:
    """A corpus exactly the size of the ceiling is a complete walk, not a stopped one."""
    client = FakeClient(items=[_item("f1"), _item("f2")])
    journal = FakeJournal(cursor=None)

    await _run(client, journal, FakeIngestor(), config=_config(max_documents_per_run=2))

    assert ("advance_cursor", "cursor-1") in journal.events
    assert ("mark_verified", ("ok", None)) in journal.events


@pytest.mark.asyncio
async def test_a_stolen_lease_stops_the_run_before_the_cursor_moves() -> None:
    client = FakeClient(changes=[DriveChange("f1", False, _item("f1"))])
    journal = FakeJournal(cursor="cursor-1")

    with pytest.raises(SyncFailedError) as caught:
        await _run(client, journal, FakeIngestor(), lease=FakeLease(renews=False))

    assert caught.value.code == "lease_lost"
    assert "advance_cursor" not in _kinds(journal)
    assert "mark_verified" not in _kinds(journal)


@pytest.mark.asyncio
async def test_a_long_walk_renews_its_lease_while_it_walks() -> None:
    """1,800 seconds of lease against a first walk that runs for hours.

    Without this the lease lapses mid-walk, a second sync claims it legitimately,
    and two processes write the same tables.
    """
    walked = LEASE_RENEW_EVERY * 2 + 5
    client = FakeClient(items=[_item(f"f{index}") for index in range(walked)])
    journal = FakeJournal(cursor=None)
    lease = FakeLease()

    await _run(client, journal, FakeIngestor(), lease=lease)

    assert lease.renewals == 3


@pytest.mark.asyncio
async def test_a_walk_that_lost_its_lease_stops_before_the_cursor_moves() -> None:
    """Somebody else is walking now, so this process must not store a cursor for them."""
    client = FakeClient(items=[_item("f1")])
    journal = FakeJournal(cursor=None)

    with pytest.raises(SyncFailedError) as caught:
        await _run(client, journal, FakeIngestor(), lease=FakeLease(renews=False))

    assert caught.value.code == "lease_lost"
    assert "advance_cursor" not in _kinds(journal)
    assert "mark_verified" not in _kinds(journal)


@pytest.mark.asyncio
async def test_an_unsupported_schema_refuses_before_a_run_row_exists() -> None:
    journal = FakeJournal(
        cursor=None, schema_error=SyncFailedError("version 2", code="schema_unsupported")
    )

    with pytest.raises(SyncFailedError):
        await _run(FakeClient(), journal, FakeIngestor())

    assert _kinds(journal) == ["assert_schema"]


@pytest.mark.asyncio
async def test_an_orphaned_run_row_is_lapsed_before_this_one_opens() -> None:
    """The previous holder died mid-run; its row must not stay `running` forever."""
    journal = FakeJournal(cursor="cursor-1")

    await _run(FakeClient(), journal, FakeIngestor())

    assert _kinds(journal)[:3] == ["assert_schema", "lapse_orphans", "open_run"]
    # Fenced on this run's own holder, or the sweep closes the row of the
    # process making it.
    assert ("lapse_orphans", "run-token") in journal.events
