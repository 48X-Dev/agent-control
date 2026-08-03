"""The claim, driven concurrently, plus every property that depends on a lease.

The claim statement is the highest-risk code in the dispatch plan, and the bug
it exists to prevent is the one that passes every serialized test: a
read-then-write looks correct, agrees with itself, and hands the same task to
two dispatchers under exactly the concurrency it was added to arbitrate.
``TestClient`` serializes requests and therefore cannot show it. So the claims
here go over a real socket, at the same instant, against a real Postgres.

Four properties, and three of them are about something *not* happening.

**Exactly one dispatcher wins.** Not "usually one". Every task in a batch is
claimed by exactly one of the two processes racing for it, and the loser gets a
conflict rather than a second copy of the work.

**A dead holder's task is recoverable.** A dispatcher that stops heartbeating
loses its claim once the lease expires, and the step it abandoned is marked
rather than deleted - the row is the only record that a turn may have reached
the executor, spent money, and acted through a tool before anybody stopped
watching.

**A dead holder's late write does not reach its successor's task.** This is the
fence, and it is the failure that would be invisible: a straggler heartbeat
from the old holder extending the *new* holder's lease, or a straggler step
write overwriting the new holder's output. Every write is refused, and the
successor's state is asserted unchanged afterwards rather than merely assumed.

**Resume position is read from the steps, never from the counter.** A
dispatcher that died between a completed step and its own bookkeeping leaves
that counter behind. The counter is the half that is allowed to be wrong, and
the test makes it wrong on purpose.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from .conftest import TEST_ADMIN_API_KEY, LiveServer

TASKS_URL = "/api/v1/agent-tasks"
STEP_AGENT = "reviewer_agent"

_CONTESTED_TASKS = 8
_TASKS_IN_FLIGHT = 2
"""How many contested tasks are raced at once, which is a pool bound and not a
weakening of the race.

Each task is raced by two requests that are genuinely in flight together, and
that pair is the whole arbitration: both are inside the claim at the same
instant, on separate connections. What this constant limits is how many *pairs*
overlap, and the reason is the server's own pool - five connections plus ten
overflow. Sixteen simultaneous claims sit one request over that ceiling, and
the sixteenth waits on a pool queue that a module-level async engine bound to
whichever event loop reached it first. The failure that produces is a
``RuntimeError`` about event loops, which says nothing about claims and would
land on a different test each run."""


def _ref(prefix: str = "race") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _race_in_pairs(
    client: httpx.AsyncClient, keys: list[str], first: str, second: str
) -> list[tuple[httpx.Response, httpx.Response]]:
    """Claim every key from two dispatchers at once, a few pairs at a time.

    Returns one pair of responses per key, in the order the keys were given.
    """

    async def claim(task_key: str, instance: str) -> httpx.Response:
        return await client.post(
            f"{TASKS_URL}/{task_key}/claim", json={"instance_id": instance}
        )

    pairs: list[tuple[httpx.Response, httpx.Response]] = []
    for start in range(0, len(keys), _TASKS_IN_FLIGHT):
        batch = keys[start : start + _TASKS_IN_FLIGHT]
        results = await asyncio.gather(
            *(
                coro
                for key in batch
                for coro in (claim(key, first), claim(key, second))
            )
        )
        pairs.extend(zip(results[::2], results[1::2], strict=True))
    return pairs


async def _import(client: httpx.AsyncClient, refs: list[str]) -> list[str]:
    """Preview then commit, and hand back the task keys in import order."""
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": ref, "title": f"title for {ref}"} for ref in refs],
    }
    preview = await client.post(f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview"})
    assert preview.status_code == 200, preview.text
    committed = await client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )
    assert committed.status_code == 200, committed.text
    keys = [str(key) for key in committed.json()["task_keys"]]
    assert len(keys) == len(refs)
    return keys


def _expire_lease(db_engine: Any, task_key: str) -> None:
    """Stop the holder's heart, which is what a dead dispatcher looks like.

    Pushing ``heartbeat_at`` into the past rather than shortening the lease in
    settings: the configured lease is what production runs, and a test that
    lowered it would be proving the reclaim works under a configuration the
    settings validator exists to refuse.
    """
    with db_engine.begin() as conn:
        updated = conn.execute(
            text(
                "UPDATE agent_tasks SET heartbeat_at = now() - interval '2 hours' "
                " WHERE task_key = :key"
            ),
            {"key": task_key},
        )
    assert updated.rowcount == 1


def _row(db_engine: Any, task_key: str) -> Any:
    with db_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, claimed_by, heartbeat_at, deadline_at, current_step, "
                "       chain_trace_id "
                "  FROM agent_tasks WHERE task_key = :key"
            ),
            {"key": task_key},
        ).one()


# ---------------------------------------------------------------------------
# Two dispatchers, one task
# ---------------------------------------------------------------------------


async def test_two_dispatchers_racing_one_task_produce_exactly_one_holder(
    live_server: LiveServer, db_engine: Any
) -> None:
    """One winner per task, and the loser is told so rather than served.

    Both claims are in flight at once, so the arbitration is Postgres's rather
    than the event loop's ordering. A read-then-write would give two 200s here
    on some fraction of runs and none on others, which is precisely why this
    file exists.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    keys = await _import(client, [_ref() for _ in range(_CONTESTED_TASKS)])

    pairs = await _race_in_pairs(client, keys, "inst-a", "inst-b")

    for key, (first, second) in zip(keys, pairs, strict=True):
        codes = sorted((first.status_code, second.status_code))
        assert codes == [200, 409], (key, first.text, second.text)
        loser = first if first.status_code == 409 else second
        assert loser.json()["error_code"] == "TASK_ALREADY_CLAIMED"
        winner = first if first.status_code == 200 else second
        row = _row(db_engine, key)
        assert row.status == "running"
        assert row.claimed_by == winner.json()["task"]["claimed_by"]
        assert row.chain_trace_id, "the chain trace is minted by whichever claim won"


async def test_a_whole_queue_claimed_by_two_dispatchers_is_never_double_worked(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The same poll on both sides, claimed at the same instant.

    This is the shape the plan describes: two dispatchers read the identical
    oldest-first page and both attempt the head. Safe, and not faster - which
    is a stated property rather than an oversight.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    keys = await _import(client, [_ref("queue") for _ in range(_CONTESTED_TASKS)])

    async def sweep(instance: str) -> list[str]:
        page = await client.get(f"{TASKS_URL}?status=queued&limit=100")
        assert page.status_code == 200, page.text
        won: list[str] = []
        for task in page.json()["tasks"]:
            claimed = await client.post(
                f"{TASKS_URL}/{task['task_key']}/claim", json={"instance_id": instance}
            )
            if claimed.status_code == 200:
                won.append(str(task["task_key"]))
            else:
                assert claimed.status_code == 409, claimed.text
        return won

    won_a, won_b = await asyncio.gather(sweep("inst-a"), sweep("inst-b"))

    assert set(won_a).isdisjoint(won_b), "one task went to both dispatchers"
    assert sorted([*won_a, *won_b]) == sorted(keys), "a task went to neither"


# ---------------------------------------------------------------------------
# A dispatcher dies
# ---------------------------------------------------------------------------


async def test_a_dead_dispatchers_task_is_reclaimed_and_its_step_marked_abandoned(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The gap is made visible rather than papered over.

    The abandoned step reached the executor. It may have spent money and it may
    have acted through a tool, and nobody knows which - so the row stays, with
    a reason on it, and ``attempts`` records that the index is being run again.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    (key,) = await _import(client, [_ref("dead")])
    assert (
        await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-dead"})
    ).status_code == 200
    started = await client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-dead", "step_index": 0, "agent_name": STEP_AGENT},
    )
    assert started.status_code == 200, started.text

    _expire_lease(db_engine, key)

    reclaimed = await client.post(
        f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-live"}
    )

    assert reclaimed.status_code == 200, reclaimed.text
    body = reclaimed.json()
    assert body["reclaimed"] is True
    assert body["prior_status"] == "running"
    assert body["abandoned_step_indexes"] == [0]
    assert body["resume_step_index"] == 0, "the abandoned step's index is run again"
    assert body["task"]["claimed_by"] == "inst-live"

    step = body["task"]["steps"][0]
    assert step["status"] == "abandoned"
    assert step["failure_code"] == "DISPATCHER_LEASE_EXPIRED"
    assert step["ended_at"] is not None


async def test_a_reclaim_keeps_the_chain_trace_and_does_not_move_the_deadline(
    live_server: LiveServer, db_engine: Any
) -> None:
    """One chain however many processes carried it, under one ceiling.

    A reclaim that reset the deadline would let a task whose dispatcher keeps
    dying live for ever, one lease at a time, under a column whose whole
    purpose is to stop that.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    (key,) = await _import(client, [_ref("deadline")])
    first = await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-one"})
    assert first.status_code == 200, first.text
    before = _row(db_engine, key)

    _expire_lease(db_engine, key)
    second = await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-two"})

    assert second.status_code == 200, second.text
    after = _row(db_engine, key)
    assert after.deadline_at == before.deadline_at, "a reclaim must not extend the ceiling"
    assert after.chain_trace_id == before.chain_trace_id
    assert second.json()["task"]["chain_trace_id"] == first.json()["task"]["chain_trace_id"]


async def test_a_task_stopped_on_quota_is_reclaimable_and_one_that_timed_out_is_not(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The two non-terminal endings differ, and the difference is safety.

    A quota refusal runs before anything leaves the process, so there is no
    side effect to duplicate and resuming is provably safe. A turn that timed
    out may still be running, and a machine that resumed it would be the
    duplicated-email failure with extra steps - so only a human clears it.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    quota_key, unknown_key = await _import(client, [_ref("quota"), _ref("unknown")])

    for key, status in ((quota_key, "paused_quota"), (unknown_key, "running_unknown")):
        assert (
            await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-dead"})
        ).status_code == 200
        finished = await client.post(
            f"{TASKS_URL}/{key}/finish",
            json={"instance_id": "inst-dead", "status": status},
        )
        assert finished.status_code == 200, finished.text
        _expire_lease(db_engine, key)

    quota = await client.post(
        f"{TASKS_URL}/{quota_key}/claim", json={"instance_id": "inst-live"}
    )
    unknown = await client.post(
        f"{TASKS_URL}/{unknown_key}/claim", json={"instance_id": "inst-live"}
    )

    assert quota.status_code == 200, quota.text
    assert quota.json()["prior_status"] == "paused_quota"
    assert quota.json()["reclaimed"] is True

    assert unknown.status_code == 409, unknown.text
    assert unknown.json()["error_code"] == "TASK_ALREADY_CLAIMED"
    assert _row(db_engine, unknown_key).status == "running_unknown", (
        "a timed-out task holds its slot until a person decides"
    )


async def test_two_dispatchers_racing_one_expired_lease_still_produce_one_holder(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The reclaim path is the claim path, so it has to arbitrate too.

    An operator restarting the fleet starts several dispatchers at once, and
    they all find the same expired leases. Reclaim is the moment two of them
    are most likely to arrive together.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    keys = await _import(client, [_ref("recontest") for _ in range(_CONTESTED_TASKS)])
    for key in keys:
        assert (
            await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-dead"})
        ).status_code == 200
        _expire_lease(db_engine, key)

    pairs = await _race_in_pairs(client, keys, "inst-a", "inst-b")

    for key, (first, second) in zip(keys, pairs, strict=True):
        assert sorted((first.status_code, second.status_code)) == [200, 409], (
            key,
            first.text,
            second.text,
        )
        assert _row(db_engine, key).claimed_by in {"inst-a", "inst-b"}


# ---------------------------------------------------------------------------
# The fence: a late write from the holder that lost
# ---------------------------------------------------------------------------


async def test_a_late_write_from_the_dead_holder_never_reaches_the_successor(
    live_server: LiveServer, db_engine: Any
) -> None:
    """Every write is fenced on the instance that holds the claim.

    The dangerous one is the heartbeat. Unfenced, a straggler from the old
    holder would refresh the *successor's* lease, and both processes would then
    believe they held one task with neither of them wrong about the row. The
    step write is the other half: it would overwrite the live holder's output
    with the output of a turn nobody is watching.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    (key,) = await _import(client, [_ref("fence")])
    assert (
        await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-dead"})
    ).status_code == 200
    assert (
        await client.post(
            f"{TASKS_URL}/{key}/steps",
            json={"instance_id": "inst-dead", "step_index": 0, "agent_name": STEP_AGENT},
        )
    ).status_code == 200
    _expire_lease(db_engine, key)
    assert (
        await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-live"})
    ).status_code == 200
    live_started = await client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-live", "step_index": 0, "agent_name": STEP_AGENT},
    )
    assert live_started.status_code == 200, live_started.text
    before = _row(db_engine, key)

    late = await asyncio.gather(
        client.post(f"{TASKS_URL}/{key}/heartbeat", json={"instance_id": "inst-dead"}),
        client.post(
            f"{TASKS_URL}/{key}/steps",
            json={"instance_id": "inst-dead", "step_index": 0, "agent_name": "other_agent"},
        ),
        client.post(
            f"{TASKS_URL}/{key}/steps/0/finish",
            json={
                "instance_id": "inst-dead",
                "status": "completed",
                "output_text": "written by the process that already lost",
            },
        ),
        client.post(
            f"{TASKS_URL}/{key}/finish",
            json={"instance_id": "inst-dead", "status": "failed"},
        ),
    )

    for response in late:
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "TASK_NOT_CLAIMED"

    after = _row(db_engine, key)
    assert after.claimed_by == "inst-live"
    assert after.status == "running"
    assert after.heartbeat_at == before.heartbeat_at, (
        "a straggler heartbeat extended the successor's lease"
    )
    detail = await client.get(f"{TASKS_URL}/{key}")
    step = detail.json()["task"]["steps"][0]
    assert step["status"] == "running", "the successor's step is still its own"
    assert step["output_text"] is None
    assert step["agent_name"] == STEP_AGENT
    assert step["attempts"] == 2, "one abandonment, then one live attempt"


async def test_the_successor_finishes_the_task_the_old_holder_could_not(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The fence refuses the loser without wedging the row.

    A fence that also blocked the winner would turn a recoverable task into a
    permanent orphan, which is the failure the reclaim predicate exists to
    prevent in the first place.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    (key,) = await _import(client, [_ref("handover")])
    await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-dead"})
    await client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-dead", "step_index": 0, "agent_name": STEP_AGENT},
    )
    _expire_lease(db_engine, key)
    await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-live"})
    await client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-live", "step_index": 0, "agent_name": STEP_AGENT},
    )

    finished_step = await client.post(
        f"{TASKS_URL}/{key}/steps/0/finish",
        json={
            "instance_id": "inst-live",
            "status": "completed",
            "output_text": "the successor's report",
        },
    )
    finished_task = await client.post(
        f"{TASKS_URL}/{key}/finish",
        json={"instance_id": "inst-live", "status": "completed"},
    )

    assert finished_step.status_code == 200, finished_step.text
    assert finished_task.status_code == 200, finished_task.text
    task = finished_task.json()["task"]
    assert task["status"] == "completed"
    assert task["claimed_by"] is None
    assert task["steps"][0]["output_text"] == "the successor's report"
    assert task["steps"][0]["attempts"] == 2


# ---------------------------------------------------------------------------
# Resume position
# ---------------------------------------------------------------------------


async def test_resume_reads_the_steps_and_not_the_counter_a_crash_left_behind(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The counter is the half that is allowed to be wrong.

    A dispatcher that died between a completed step and its own bookkeeping
    leaves ``current_step`` behind the steps. Resuming from that counter would
    re-run a step that already spent money and may already have acted through a
    tool, so the resume rule reads ``MAX(step_index) WHERE status='completed'``
    instead. The counter is put back by hand here, which is exactly the state
    such a crash leaves.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    (key,) = await _import(client, [_ref("resume")])
    await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-dead"})
    await client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-dead", "step_index": 0, "agent_name": STEP_AGENT},
    )
    finished = await client.post(
        f"{TASKS_URL}/{key}/steps/0/finish",
        json={"instance_id": "inst-dead", "status": "completed", "output_text": "step one"},
    )
    assert finished.status_code == 200, finished.text
    with db_engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_tasks SET current_step = 0 WHERE task_key = :key"),
            {"key": key},
        )
    _expire_lease(db_engine, key)

    reclaimed = await client.post(
        f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-live"}
    )

    assert reclaimed.status_code == 200, reclaimed.text
    body = reclaimed.json()
    assert body["resume_step_index"] == 1, (
        "resumed at the completed step, which would re-run work already paid for"
    )
    assert body["abandoned_step_indexes"] == [], "a completed step is not abandoned"
    assert body["task"]["steps"][0]["status"] == "completed"
    assert body["task"]["steps"][0]["output_text"] == "step one"


async def test_a_reclaim_after_a_completed_step_and_an_abandoned_one_resumes_at_the_gap(
    live_server: LiveServer, db_engine: Any
) -> None:
    """Completed steps advance the resume point; an in-flight one does not.

    The in-flight step is abandoned rather than re-numbered, so the successor
    picks up at the index that never finished. Its worst case is a duplicated
    side effect on that one hop, which is the trade the plan makes explicitly.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    (key,) = await _import(client, [_ref("gap")])
    await client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-dead"})
    await client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-dead", "step_index": 0, "agent_name": STEP_AGENT},
    )
    await client.post(
        f"{TASKS_URL}/{key}/steps/0/finish",
        json={"instance_id": "inst-dead", "status": "completed", "output_text": "one"},
    )
    await client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-dead", "step_index": 1, "agent_name": STEP_AGENT},
    )
    _expire_lease(db_engine, key)

    reclaimed = await client.post(
        f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-live"}
    )

    assert reclaimed.status_code == 200, reclaimed.text
    body = reclaimed.json()
    assert body["resume_step_index"] == 1
    assert body["abandoned_step_indexes"] == [1]
    statuses = [step["status"] for step in body["task"]["steps"]]
    assert statuses == ["completed", "abandoned"]


# ---------------------------------------------------------------------------
# Import, concurrently
# ---------------------------------------------------------------------------


async def test_the_same_set_committed_twice_at_once_creates_one_row_per_ref(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The double-clicked button, and two replicas, are the same bug.

    Dedup is a partial unique index rather than a check in a handler, so two
    commits landing at the same instant produce one row and an honest count of
    what was created rather than what was attempted.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    refs = [_ref("double") for _ in range(6)]
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": ref, "title": ref} for ref in refs],
    }
    preview = await client.post(f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview"})
    commit = {
        "scope": scope,
        "mode": "commit",
        "expected_refs_digest": preview.json()["refs_digest"],
    }

    first, second = await asyncio.gather(
        client.post(f"{TASKS_URL}/import", json=commit),
        client.post(f"{TASKS_URL}/import", json=commit),
    )

    created = [
        response.json()["created"]
        for response in (first, second)
        if response.status_code == 200
    ]
    for response in (first, second):
        assert response.status_code in (200, 409), response.text
    assert sum(created) == len(refs), (first.text, second.text)

    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT source_ref, count(*) FROM agent_tasks GROUP BY source_ref")
        ).all()
    assert sorted(row[0] for row in rows) == sorted(refs)
    assert {row[1] for row in rows} == {1}, "one open task per source ref, always"


@pytest.mark.parametrize("instance", ["inst-a", "inst-b"])
async def test_a_claim_on_a_task_that_does_not_exist_is_a_not_found(
    live_server: LiveServer, instance: str
) -> None:
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})

    response = await client.post(
        f"{TASKS_URL}/{uuid.uuid4().hex}/claim", json={"instance_id": instance}
    )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "AGENT_TASK_NOT_FOUND"
