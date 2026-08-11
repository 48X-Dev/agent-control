"""What stops a run: an unreachable root, a ceiling, a lost lease, a schema it cannot use.

Stubbed collaborators and no database, so these pin the decisions rather than Postgres.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_control_knowledge_sync.drive_client import DriveChange, DriveRootUnreachableError
from agent_control_knowledge_sync.sync import LEASE_RENEW_EVERY, RunCounters, SyncFailedError

from tests.sync_fakes import (
    FakeClient,
    FakeIngestor,
    FakeJournal,
    FakeLease,
    _config,
    _item,
    _kinds,
    _run,
)


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
    """1,800 seconds of lease against a first walk that runs for hours."""
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


# --- 5.4's byte ceilings ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_walk_stops_at_the_source_byte_ceiling_with_its_cursor_unset() -> None:
    """A 10GB Drive is a configuration conversation, not an invisible half-mirror."""
    client = FakeClient(items=[_item("f1"), _item("f2"), _item("f3")])
    journal = FakeJournal(cursor=None)

    counters = await _run(client, journal, FakeIngestor(), config=_config(source_max_bytes=4))

    assert counters.indexed == 1
    assert "advance_cursor" not in _kinds(journal)
    assert ("mark_verified", ("partial", "source_ceiling")) in journal.events


@pytest.mark.asyncio
async def test_the_run_ceiling_names_itself_apart_from_the_source_one() -> None:
    """Two ceilings reported as one leave an operator raising the wrong number."""
    client = FakeClient(items=[_item("f1"), _item("f2")])
    journal = FakeJournal(cursor=None)

    await _run(
        client,
        journal,
        FakeIngestor(),
        config=_config(source_max_bytes=1_000_000, run_max_fetch_bytes=4),
    )

    assert ("mark_verified", ("partial", "run_fetch_ceiling")) in journal.events


@pytest.mark.asyncio
async def test_a_replay_stops_at_a_ceiling_and_leaves_the_cursor_where_it_was() -> None:
    """Advancing past changes this pass never applied is how a mirror goes quietly wrong."""
    changes = [DriveChange(f"f{index}", False, _item(f"f{index}")) for index in range(3)]
    client = FakeClient(changes=changes)
    journal = FakeJournal(cursor="cursor-1")

    counters = await _run(
        client, journal, FakeIngestor(), config=_config(max_documents_per_run=2)
    )

    assert counters.seen == 2
    assert "advance_cursor" not in _kinds(journal)
    assert ("mark_verified", ("partial", "source_ceiling")) in journal.events


@pytest.mark.asyncio
async def test_a_replay_under_every_ceiling_still_advances() -> None:
    client = FakeClient(changes=[DriveChange("f1", False, _item("f1"))])
    journal = FakeJournal(cursor="cursor-1")

    await _run(client, journal, FakeIngestor(), config=_config(max_documents_per_run=2))

    assert ("advance_cursor", "cursor-2") in journal.events
    assert ("mark_verified", ("ok", None)) in journal.events


@pytest.mark.asyncio
async def test_a_github_repo_that_went_short_makes_the_run_partial_and_drive_stays_ok() -> None:
    """The Drive source row keeps its own code; only the run row spans both halves."""
    journal = FakeJournal(cursor="cursor-1")

    async def github(tally: Any) -> str:
        return "repo_unreachable"

    await _run(FakeClient(), journal, FakeIngestor(), github=github)

    assert ("mark_verified", ("ok", None)) in journal.events
    status, error_code, _ = next(
        payload for name, payload in journal.events if name == "close_run"
    )
    assert (status, error_code) == ("partial", "repo_unreachable")


@pytest.mark.asyncio
async def test_github_is_skipped_rather_than_silent_when_the_run_budget_is_spent() -> None:
    """5.4's run ceiling bounds one process regardless of how many sources are configured."""
    client = FakeClient(items=[_item("f1"), _item("f2")])
    journal = FakeJournal(cursor=None)
    called = []

    async def github(tally: Any) -> None:
        called.append(tally)

    await _run(
        client,
        journal,
        FakeIngestor(),
        config=_config(run_max_fetch_bytes=4),
        github=github,
    )

    assert called == []
    assert ("mark_verified", ("partial", "run_fetch_ceiling")) in journal.events


@pytest.mark.asyncio
async def test_every_pass_sweeps_the_tombstones_past_their_window() -> None:
    """4.4: nothing else deletes them, so a pass that skips this accumulates forever."""
    journal = FakeJournal(cursor="cursor-1")

    await _run(
        FakeClient(), journal, FakeIngestor(), config=_config(tombstone_retention_days=30)
    )

    assert ("sweep_tombstones", 30) in journal.events
