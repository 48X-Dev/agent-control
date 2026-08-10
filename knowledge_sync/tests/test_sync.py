"""What one pass records: the cursor, the tombstones and the counts.

Stubbed collaborators and no database, so these pin the decisions rather than Postgres.
"""

from __future__ import annotations

import pytest
from agent_control_knowledge_sync.drive_client import (
    DriveChange,
    DriveRefusalError,
    DriveRefusalRecord,
    LocationUnknown,
    OutsideRoot,
)
from agent_control_knowledge_sync.ingest import IngestOutcome
from agent_control_knowledge_sync.sync import RunCounters

from tests.sync_fakes import (
    FakeClient,
    FakeIngestor,
    FakeJournal,
    _item,
    _kinds,
    _run,
)


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
    """The document is live; a 503 on its parent walk must not delete its chunks."""
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
