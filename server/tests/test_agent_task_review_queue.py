"""The review queue's rendering contract, and 5.7 step 6's moved bar.

Order, total, filters and the stale flag are what make "3 results waiting
for you" honest: nothing expires out of the queue in either direction, and
age is visible without doing anything. The milestone test pins the accept
response carrying the new progress value directly, with the process-local
cache invalidation as its best-effort second half.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from .test_agent_task_review_gate import (
    TASKS_URL,
    RecordingLinearClient,
    _accept,
    _commit_linear,
    _entry,
    _ref,
    _run_to_completion,
    linear,
)

__all__ = ["linear"]  # re-exported fixture


def test_the_queue_orders_oldest_first_counts_beyond_the_page_and_filters(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
) -> None:
    keys = []
    for index in range(3):
        key = _commit_linear(admin_client, _ref())
        _run_to_completion(non_admin_client, key, instance=f"inst-{index}")
        keys.append(key)
    with db_engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_tasks SET team_slug = 'team-x' WHERE task_key = :key"),
            {"key": keys[1]},
        )
        conn.execute(
            text(
                "UPDATE agent_tasks SET source_scope_ref = 'mile-1' WHERE task_key = :key"
            ),
            {"key": keys[2]},
        )

    page = admin_client.get(f"{TASKS_URL}/review", params={"limit": 2}).json()
    assert [e["task_key"] for e in page["entries"]] == keys[:2], "oldest first"
    assert page["total"] == 3, "total counts beyond the page"

    by_team = admin_client.get(f"{TASKS_URL}/review", params={"team": "team-x"}).json()
    assert [e["task_key"] for e in by_team["entries"]] == [keys[1]]
    assert by_team["total"] == 1

    by_milestone = admin_client.get(
        f"{TASKS_URL}/review", params={"milestone_id": "mile-1"}
    ).json()
    assert [e["task_key"] for e in by_milestone["entries"]] == [keys[2]]


def test_an_entry_older_than_the_threshold_renders_stale_and_stays_listed(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
) -> None:
    """Age is visible and that is all it does: nothing expires out of the
    queue, in either direction."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_task_writebacks SET created_at = now() - interval '49 hours' "
                "WHERE kind = 'status_change' AND task_id = "
                "(SELECT id FROM agent_tasks WHERE task_key = :key)"
            ),
            {"key": key},
        )

    entry = _entry(admin_client, key)

    assert entry["stale"] is True
    assert _accept(admin_client, key, entry).status_code == 200, (
        "a stale entry must still be acceptable, not expired"
    )


# ---------------------------------------------------------------------------
# Step 6: the bar the reviewer just moved, moved
# ---------------------------------------------------------------------------


class _RecordingMilestoneService:
    def __init__(self, progress: float | None) -> None:
        self.invalidated: list[tuple[str, str]] = []
        self._progress = progress

    def invalidate(self, *, namespace_key: str, linear_team_key: str) -> None:
        self.invalidated.append((namespace_key, linear_team_key))

    async def get_milestones(self, *, namespace_key: str, linear_team_key: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            milestones=[SimpleNamespace(id="milestone-1", progress=self._progress)]
        )


def test_accept_invalidates_both_milestone_caches_and_carries_progress(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_control_server.endpoints import agent_tasks as endpoint_module
    from agent_control_server.models import DEFAULT_NAMESPACE_KEY

    milestones = _RecordingMilestoneService(progress=0.42)
    issues = _RecordingMilestoneService(progress=None)
    monkeypatch.setattr(endpoint_module, "get_milestone_service", lambda: milestones)
    monkeypatch.setattr(endpoint_module, "get_milestone_issues_service", lambda: issues)

    key = _commit_linear(admin_client, _ref())
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_tasks SET source_scope_kind = 'milestone', "
                "source_scope_ref = 'milestone-1' WHERE task_key = :key"
            ),
            {"key": key},
        )
    _run_to_completion(non_admin_client, key)
    entry = _entry(admin_client, key)

    response = _accept(admin_client, key, entry)

    assert response.status_code == 200, response.text
    assert response.json()["milestone_progress"] == 0.42
    assert milestones.invalidated == [(DEFAULT_NAMESPACE_KEY, "OPS")]
    assert issues.invalidated == [(DEFAULT_NAMESPACE_KEY, "OPS")]
