"""The accept/reject gate of plan 5.7, proven mostly by absence.

Where the shipped suite proves the happy paths, this file holds the gate to
its refusal matrix and asserts on every refusal that nothing left for
Linear: a fake client records *every* call, so "the issue stayed open" is a
count of zero mutations rather than an inference.

This module also carries the shared plumbing - the recording fake, the
fixtures and the flow helpers - imported by the queue, provider and absence
test modules beside it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.services.linear_client import LinearError
from agent_control_server.services.linear_writeback import (
    CompletedStateResolver,
    IssueReviewState,
    decision_digest,
)
from agent_control_server.services.linear_writeback_runtime import (
    WritebackRuntime,
    get_writeback_runtime,
)

TASKS_URL = "/api/v1/agent-tasks"
STEP_AGENT = "gatecheck_agent"
DUMMY_DIGEST = "sha256:" + "0" * 64


class RecordingLinearClient:
    """A fake that records every call, so absence is assertable.

    ``issue_has_marker`` mirrors ``HttpLinearWritebackClient``'s first-line
    matching on purpose; if that matching changes shape, change this with it.
    """

    MUTATIONS = ("create_comment", "update_issue_state")

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.state_updates: list[tuple[str, str]] = []
        self.completed_state_id = "state-done"
        self.issues: dict[str, IssueReviewState] = {}
        self.raise_on: dict[str, Exception] = {}
        self.runtime: WritebackRuntime | None = None

    def mutation_count(self) -> int:
        return sum(1 for name in self.calls if name in self.MUTATIONS)

    def issue(self, ref: str, **overrides: Any) -> IssueReviewState:
        state = IssueReviewState(
            ref=ref,
            identifier="OPS-9",
            title="A queued issue",
            state_id="state-backlog",
            state_name="Backlog",
            state_type="backlog",
            team_key="OPS",
            milestone_id="milestone-1",
        )
        state = IssueReviewState(**{**state.__dict__, **overrides})
        self.issues[ref] = state
        return state

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        exc = self.raise_on.get(name)
        if exc is not None:
            raise exc

    async def create_comment(self, *, issue_id: str, body: str) -> str:
        self._enter("create_comment")
        self.comments.append((issue_id, body))
        return f"comment-{len(self.comments)}"

    async def issue_has_marker(self, *, issue_id: str, marker: str) -> bool:
        self._enter("issue_has_marker")
        return any(
            ref == issue_id and body.split("\n", 1)[0].strip() == marker
            for ref, body in self.comments
        )

    async def update_issue_state(self, *, issue_id: str, state_id: str) -> None:
        self._enter("update_issue_state")
        self.state_updates.append((issue_id, state_id))

    async def fetch_completed_state_id(self, *, team_key: str) -> str:
        self._enter("fetch_completed_state_id")
        return self.completed_state_id

    async def fetch_issue_review_state(self, *, issue_id: str) -> IssueReviewState:
        self._enter("fetch_issue_review_state")
        if issue_id not in self.issues:
            self.issue(issue_id)
        return self.issues[issue_id]

    async def aclose(self) -> None:
        pass


def _install_runtime(
    app: Any, fake: RecordingLinearClient | None, *, write_enabled: bool
) -> RecordingLinearClient | None:
    runtime = WritebackRuntime(
        client=fake,
        resolver=CompletedStateResolver(fake) if fake is not None else None,
        write_enabled=write_enabled,
    )
    if fake is not None:
        fake.runtime = runtime
    app.dependency_overrides[get_writeback_runtime] = lambda: runtime
    return fake


@pytest.fixture()
def linear(app: Any):
    fake = _install_runtime(app, RecordingLinearClient(), write_enabled=True)
    assert fake is not None
    yield fake
    app.dependency_overrides.pop(get_writeback_runtime, None)


@pytest.fixture()
def linear_off(app: Any):
    fake = _install_runtime(app, RecordingLinearClient(), write_enabled=False)
    assert fake is not None
    yield fake
    app.dependency_overrides.pop(get_writeback_runtime, None)


@pytest.fixture()
def keyless(app: Any):
    _install_runtime(app, None, write_enabled=False)
    yield
    app.dependency_overrides.pop(get_writeback_runtime, None)


# -- flow helpers ------------------------------------------------------------


def _ref() -> str:
    return f"issue-{uuid.uuid4().hex[:12]}"


def _commit_linear(
    client: TestClient, ref: str, *, dry_run: bool = False, **extra: Any
) -> str:
    scope = {
        "kind": "items",
        "source_kind": "linear",
        "items": [{"source_ref": ref, "title": f"title for {ref}"}],
    }
    preview = client.post(
        f"{TASKS_URL}/import",
        json={"scope": scope, "mode": "preview", "dry_run": dry_run, **extra},
    )
    assert preview.status_code == 200, preview.text
    commit = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "dry_run": dry_run,
            "expected_refs_digest": preview.json()["refs_digest"],
            **extra,
        },
    )
    assert commit.status_code == 200, commit.text
    return str(commit.json()["task_keys"][0])


def _run_to_completion(
    client: TestClient,
    task_key: str,
    *,
    output_text: str = "summary of what was done",
    agent_name: str = STEP_AGENT,
    instance: str = "inst-a",
) -> None:
    claimed = client.post(f"{TASKS_URL}/{task_key}/claim", json={"instance_id": instance})
    assert claimed.status_code == 200, claimed.text
    started = client.post(
        f"{TASKS_URL}/{task_key}/steps",
        json={"instance_id": instance, "step_index": 0, "agent_name": agent_name},
    )
    assert started.status_code == 200, started.text
    finished = client.post(
        f"{TASKS_URL}/{task_key}/steps/0/finish",
        json={"instance_id": instance, "status": "completed", "output_text": output_text},
    )
    assert finished.status_code == 200, finished.text
    done = client.post(
        f"{TASKS_URL}/{task_key}/finish",
        json={"instance_id": instance, "status": "completed"},
    )
    assert done.status_code == 200, done.text


def _rows(db_engine: Any, task_key: str) -> list[dict[str, Any]]:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT w.id, w.kind, w.status, w.body, w.attempts, w.rejected_reason "
                "FROM agent_task_writebacks w JOIN agent_tasks t ON t.id = w.task_id "
                "WHERE t.task_key = :key ORDER BY w.id"
            ),
            {"key": task_key},
        ).mappings()
        return [dict(row) for row in rows]


def _entry(client: TestClient, task_key: str) -> dict[str, Any]:
    response = client.get(f"{TASKS_URL}/review")
    assert response.status_code == 200, response.text
    for entry in response.json()["entries"]:
        if entry["task_key"] == task_key:
            return dict(entry)
    raise AssertionError(f"no review entry for {task_key}: {response.text}")


def _accept(
    client: TestClient, task_key: str, entry: dict[str, Any], **overrides: Any
) -> Any:
    body = {
        "writeback_id": entry["writeback_id"],
        "expected_decision_digest": entry["decision_digest"],
    }
    body.update(overrides)
    return client.post(f"{TASKS_URL}/{task_key}/accept", json=body)


def _reject(client: TestClient, task_key: str, entry: dict[str, Any], **body: Any) -> Any:
    return client.post(
        f"{TASKS_URL}/{task_key}/reject",
        json={"writeback_id": entry["writeback_id"], **body},
    )


# ---------------------------------------------------------------------------
# A decision is single-shot, in both orders, and a rejection posts nothing
# ---------------------------------------------------------------------------


def test_a_rejected_proposal_posts_nothing_and_cannot_be_accepted_later(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
) -> None:
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _entry(admin_client, key)
    before = linear.mutation_count()

    rejected = _reject(admin_client, key, entry, reason="not actually done")
    assert rejected.status_code == 200, rejected.text

    accepted = _accept(admin_client, key, entry)
    assert accepted.status_code == 409, accepted.text
    assert accepted.json()["error_code"] == "TASK_STATUS_CONFLICT"

    assert linear.mutation_count() == before, "a rejection reached Linear"
    proposal = [r for r in _rows(db_engine, key) if r["kind"] == "status_change"]
    assert [r["status"] for r in proposal] == ["rejected"]
    assert proposal[0]["rejected_reason"] == "not actually done"


def test_an_accepted_proposal_cannot_then_be_rejected(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
) -> None:
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _entry(admin_client, key)
    assert _accept(admin_client, key, entry).status_code == 200

    response = _reject(admin_client, key, entry, reason="second thoughts")

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_STATUS_CONFLICT"
    assert len(linear.state_updates) == 1, "the close happened exactly once"


# ---------------------------------------------------------------------------
# Accept-time Linear failures re-offer; a moved state refuses without leaking
# ---------------------------------------------------------------------------


def test_a_linear_failure_during_accept_reoffers_the_proposal(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
) -> None:
    """5.7 step 5's ordering: the mutation runs before the row flips, so a
    failure leaves the row ``awaiting_approval`` and the same accept works
    once Linear answers."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _entry(admin_client, key)
    linear.raise_on["update_issue_state"] = LinearError("Linear reported an internal error.")

    failed = _accept(admin_client, key, entry)
    assert failed.status_code == 503, failed.text
    assert failed.json()["error_code"] == "LINEAR_UNAVAILABLE"
    proposal = [r for r in _rows(db_engine, key) if r["kind"] == "status_change"]
    assert [r["status"] for r in proposal] == ["awaiting_approval"], (
        "a Linear failure must not consume the proposal"
    )

    linear.raise_on.clear()
    retried = _accept(admin_client, key, entry)
    assert retried.status_code == 200, retried.text
    assert retried.json()["writeback"]["status"] == "sent"
    # Only the landed attempt is guaranteed on the row: the failed attempt's
    # increment is flushed inside a request transaction the 503 rolls back.
    assert retried.json()["writeback"]["attempts"] >= 1


def test_an_unreadable_issue_refuses_the_accept_and_changes_nothing(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
) -> None:
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _entry(admin_client, key)
    linear.raise_on["fetch_issue_review_state"] = LinearError("Linear could not be reached.")

    response = _accept(admin_client, key, entry)

    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "LINEAR_UNAVAILABLE"
    assert linear.state_updates == [], "an unreadable issue was still mutated"
    proposal = [r for r in _rows(db_engine, key) if r["kind"] == "status_change"]
    assert [r["status"] for r in proposal] == ["awaiting_approval"]


def test_a_completed_state_that_moved_refuses_without_leaking_the_fresh_digest(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
) -> None:
    """The digest's third part: the state the accept would move the issue to.
    The refusal names the conflict and nothing else - the current digest is
    read back through the queue, not through the error body."""
    ref = _ref()
    key = _commit_linear(admin_client, ref)
    _run_to_completion(non_admin_client, key)
    entry = _entry(admin_client, key)
    assert linear.runtime is not None and linear.runtime.resolver is not None
    linear.runtime.resolver.invalidate("OPS")
    linear.completed_state_id = "state-v2"

    response = _accept(admin_client, key, entry)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "DECISION_CHANGED"
    fresh = decision_digest(entry["summary"], ref, "state-v2")
    assert fresh not in response.text, "the 409 leaked the fresh digest"
    assert linear.state_updates == [], "a refused digest still closed the issue"


# ---------------------------------------------------------------------------
# Dry run, defense in depth
# ---------------------------------------------------------------------------


def test_even_a_smuggled_row_on_a_dry_run_task_is_refused(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
) -> None:
    """The flow never creates a proposal for a dry run; this row arrives by
    SQL to prove the accept's own check holds even if one existed."""
    key = _commit_linear(admin_client, _ref(), dry_run=True)
    _run_to_completion(non_admin_client, key)
    with db_engine.begin() as conn:
        row_id = conn.execute(
            text(
                "INSERT INTO agent_task_writebacks "
                "(namespace_key, task_id, step_index, kind, status, body) "
                "SELECT t.namespace_key, t.id, 0, 'status_change', "
                "'awaiting_approval', 'smuggled' FROM agent_tasks t "
                "WHERE t.task_key = :key RETURNING id"
            ),
            {"key": key},
        ).scalar_one()

    response = admin_client.post(
        f"{TASKS_URL}/{key}/accept",
        json={"writeback_id": row_id, "expected_decision_digest": DUMMY_DIGEST},
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "TASK_STATUS_CONFLICT"
    assert "dry" in body["detail"].lower()
    assert linear.mutation_count() == 0
