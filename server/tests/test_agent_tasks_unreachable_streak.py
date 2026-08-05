"""A dead executor must not retry forever under a status that reads as running.

``paused_quota`` keeps its slot and is reclaimable once the lease lapses, which
is exactly right for a spent budget: budgets refill, so the retry loop is the
feature. An unreachable executor rides the same status, and without a bound the
operator watches the cycle from the console for ever: park, lease lapse,
reclaim, park - every cycle labelled "1 running", nothing ever running.

The bound: consecutive parks with ``EXECUTOR_UNAVAILABLE`` convert to
``blocked`` at the configured ceiling. ``blocked`` already means "a human
changes something first" and is operator-clearable with cancel, so the
conversion is an exit rather than a trap. A budget park never counts toward it,
and a step that actually starts resets the streak, because a claim that got as
far as opening a step proves the executor answered.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from starlette.testclient import TestClient

TASKS_URL = "/api/v1/agent-tasks"
UNREACHABLE = "EXECUTOR_UNAVAILABLE"
CEILING = 3  # the DispatchSettings default; asserted in the first test


def _import_one(client: TestClient) -> str:
    ref = f"streak-{uuid.uuid4().hex[:12]}"
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": ref, "title": f"title for {ref}"}],
    }
    preview = client.post(
        f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview"}
    )
    assert preview.status_code == 200, preview.text
    committed = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )
    assert committed.status_code == 200, committed.text
    return str(committed.json()["task_keys"][0])


def _claim(client: TestClient, task_key: str, instance: str) -> None:
    response = client.post(
        f"{TASKS_URL}/{task_key}/claim", json={"instance_id": instance}
    )
    assert response.status_code == 200, response.text


def _park(
    client: TestClient, task_key: str, instance: str, code: str
) -> dict[str, Any]:
    response = client.post(
        f"{TASKS_URL}/{task_key}/finish",
        json={
            "instance_id": instance,
            "status": "paused_quota",
            "failure_code": code,
            "failure_detail": f"synthetic park with {code}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]


def _expire_lease(db_engine: Any, task_key: str) -> None:
    """The reclaim predicate is the authority; this only ages the heartbeat."""
    with db_engine.begin() as conn:
        updated = conn.execute(
            text(
                "UPDATE agent_tasks SET heartbeat_at = now() - interval '2 hours' "
                " WHERE task_key = :key"
            ),
            {"key": task_key},
        )
    assert updated.rowcount == 1


def _park_cycle(
    client: TestClient, db_engine: Any, task_key: str, cycle: int, code: str
) -> dict[str, Any]:
    instance = f"dispatcher-{cycle}"
    _claim(client, task_key, instance)
    task = _park(client, task_key, instance, code)
    _expire_lease(db_engine, task_key)
    return task


def test_the_third_unreachable_park_blocks_instead_of_looping(
    client: TestClient, db_engine: Any
) -> None:
    task_key = _import_one(client)

    first = _park_cycle(client, db_engine, task_key, 1, UNREACHABLE)
    assert first["status"] == "paused_quota"
    second = _park_cycle(client, db_engine, task_key, 2, UNREACHABLE)
    assert second["status"] == "paused_quota"

    third = _park_cycle(client, db_engine, task_key, 3, UNREACHABLE)
    assert third["status"] == "blocked", third
    assert "unreachable across 3 claim cycles" in (third["failure_detail"] or "")
    # The exit is real: blocked is cancellable, so the operator is not trapped.
    cancelled = client.post(
        f"{TASKS_URL}/{task_key}/cancel", json={"reason": "executor restarted"}
    )
    assert cancelled.status_code == 200, cancelled.text


def test_a_budget_park_never_converts_however_often_it_repeats(
    client: TestClient, db_engine: Any
) -> None:
    """Budgets refill; retrying them forever is the design, not the bug."""
    task_key = _import_one(client)
    for cycle in range(1, CEILING + 2):
        task = _park_cycle(
            client, db_engine, task_key, cycle, "DISPATCH_BUDGET_EXHAUSTED"
        )
        assert task["status"] == "paused_quota", (cycle, task)


def test_a_step_that_starts_resets_the_streak(
    client: TestClient, db_engine: Any
) -> None:
    """A claim that opened a step proves the executor answered."""
    task_key = _import_one(client)
    _park_cycle(client, db_engine, task_key, 1, UNREACHABLE)
    _park_cycle(client, db_engine, task_key, 2, UNREACHABLE)

    instance = "dispatcher-3"
    _claim(client, task_key, instance)
    step = client.post(
        f"{TASKS_URL}/{task_key}/steps",
        json={
            "instance_id": instance,
            "step_index": 0,
            "agent_name": "streak_probe_agent",
            "brief": "prove the executor answered",
            "session_key": None,
        },
    )
    assert step.status_code == 200, step.text

    # Park again from the same claim: the streak restarts at one, so the next
    # two cycles stay paused and only the one after that blocks.
    task = _park(client, task_key, instance, UNREACHABLE)
    assert task["status"] == "paused_quota", task
    _expire_lease(db_engine, task_key)
    task = _park_cycle(client, db_engine, task_key, 4, UNREACHABLE)
    assert task["status"] == "paused_quota", task
    task = _park_cycle(client, db_engine, task_key, 5, UNREACHABLE)
    assert task["status"] == "blocked", task


def test_a_different_code_between_parks_restarts_the_count(
    client: TestClient, db_engine: Any
) -> None:
    task_key = _import_one(client)
    _park_cycle(client, db_engine, task_key, 1, UNREACHABLE)
    _park_cycle(client, db_engine, task_key, 2, "DISPATCH_BUDGET_EXHAUSTED")
    task = _park_cycle(client, db_engine, task_key, 3, UNREACHABLE)
    assert task["status"] == "paused_quota", task
    task = _park_cycle(client, db_engine, task_key, 4, UNREACHABLE)
    assert task["status"] == "paused_quota", task
    task = _park_cycle(client, db_engine, task_key, 5, UNREACHABLE)
    assert task["status"] == "blocked", task
