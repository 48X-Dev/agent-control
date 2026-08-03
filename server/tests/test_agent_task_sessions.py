"""The binding between a dispatch task and the session one of its steps runs on,
and the statuses under which no further step may start.

Each property here was broken or missing when this file was written, and every
one of them is silent when it breaks.

**The binding has to be sent when the session is opened.** ``agent_task_id`` is
what the turn path reads to tell a fleet turn from a human's chat, so every
ceiling that keys off it - the namespace budget, the dispatch pause, the kill
switch - simply does not apply to a session that omitted it. A column that is
always null looks exactly like a column nobody has needed yet.

**The oversight branch is not a turn grant.** A task's session has no human
owner, so ``require_content_access`` lets a caller who did not open it read,
halt and nudge it. That same predicate gates ``run_turn``, and an unqualified
branch would therefore let any authenticated caller in the namespace append to
a fleet conversation and spend against it. The plan refuses to share the
dispatcher's key because that "lets every reviewer start turns as the
dispatcher"; granting it to everyone would be the same thing, wider.

**A task that has finished takes no more sessions.** Binding one to a terminal
task would put a turn under a budget nobody is watching any more.

**Holding the claim is not the same as being runnable.** ``blocked``,
``paused_quota`` and ``running_unknown`` all keep ``claimed_by``, and none of
them may take a step or be finished off by the machine that holds it. The last
one is the one that matters: a timed-out task must never silently advance, and
that has to be a refusal on this side rather than a discipline in the process
being refused.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.errors import ForbiddenError
from agent_control_server.models import AgentSession
from agent_control_server.services.agent_sessions import require_content_access

from .test_agent_sessions_endpoints import (
    _agent_name,
    _bind,
    _open_session,
    _register_agent,
    executor_enabled,  # noqa: F401 - fixture
    fake_executor,  # noqa: F401 - fixture
)

TASKS_URL = "/api/v1/agent-tasks"
SESSIONS_URL = "/api/v1/agent-sessions"


def _claimed_task(client: TestClient, ref: str) -> str:
    """Import one item, then claim it, and hand back its key."""
    body: dict[str, Any] = {
        "scope": {
            "kind": "items",
            "source_kind": "file",
            "items": [{"source_ref": ref, "title": ref}],
        },
        "mode": "preview",
    }
    preview = client.post(f"{TASKS_URL}/import", json=body)
    assert preview.status_code == 200, preview.text
    body["mode"] = "commit"
    body["expected_refs_digest"] = preview.json()["refs_digest"]
    commit = client.post(f"{TASKS_URL}/import", json=body)
    assert commit.status_code == 200, commit.text
    key = str(commit.json()["task_keys"][0])
    claimed = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst"})
    assert claimed.status_code == 200, claimed.text
    return key


def test_a_session_opened_for_a_task_carries_the_binding(
    client: TestClient,
    db_engine: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,  # noqa: F811 - fixture
) -> None:
    agent = _agent_name()
    _register_agent(client, agent)
    _bind(client, agent)
    key = _claimed_task(client, "task-session-bound")

    session = _open_session(client, agent, task_key=key)

    with db_engine.connect() as conn:
        bound = conn.execute(
            text(
                "SELECT t.task_key FROM agent_sessions s "
                "  JOIN agent_tasks t ON t.id = s.agent_task_id "
                " WHERE s.session_key = :key"
            ),
            {"key": session["session_key"]},
        ).first()

    assert bound is not None, "agent_task_id was not set, so no ceiling keys off it"
    assert bound.task_key == key


def test_an_unknown_or_finished_task_is_refused_before_the_executor(
    client: TestClient,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,  # noqa: F811 - fixture
) -> None:
    agent = _agent_name()
    _register_agent(client, agent)
    _bind(client, agent)

    missing = client.post(SESSIONS_URL, json={"agent_name": agent, "task_key": "0" * 32})
    assert missing.status_code == 404, missing.text

    key = _claimed_task(client, "task-session-finished")
    client.post(
        f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst", "status": "completed"}
    )
    finished = client.post(SESSIONS_URL, json={"agent_name": agent, "task_key": key})
    assert finished.status_code == 409, finished.text


def test_the_task_branch_grants_oversight_but_not_a_turn() -> None:
    """Read, halt and nudge for anyone. Starting a turn for the holder or an admin."""
    row = AgentSession(
        session_key="s",
        agent_name="reviewer_agent",
        created_by_hash="the-dispatcher",
        agent_task_id=7,
    )

    require_content_access(row, caller_hash="somebody-else", is_admin=False)

    with pytest.raises(ForbiddenError):
        require_content_access(
            row, caller_hash="somebody-else", is_admin=False, for_turn=True
        )

    require_content_access(row, caller_hash="the-dispatcher", is_admin=False, for_turn=True)
    require_content_access(row, caller_hash="somebody-else", is_admin=True, for_turn=True)


def test_a_dispatcher_cannot_clear_its_own_timed_out_task(client: TestClient) -> None:
    """``running_unknown`` moves on a human's say-so and on nothing else.

    The holder keeps ``claimed_by`` on a timed-out task so a person can find
    it. That is not permission for the holder to come back and record the work
    failed: nothing on this side can prove the invocation stopped, and a status
    that any machine can clear is not a hold.
    """
    key = _claimed_task(client, "task-timed-out")
    client.post(
        f"{TASKS_URL}/{key}/finish",
        json={"instance_id": "inst", "status": "running_unknown"},
    )

    for status in ("failed", "completed", "blocked"):
        escape = client.post(
            f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst", "status": status}
        )
        assert escape.status_code == 409, (status, escape.text)
        assert escape.json()["error_code"] == "TASK_STATUS_CONFLICT"

    assert client.get(f"{TASKS_URL}/{key}").json()["task"]["status"] == "running_unknown"

    resolved = client.post(f"{TASKS_URL}/{key}/resolve", json={"requeue": True})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["task"]["status"] == "queued"


def test_no_step_starts_on_a_task_that_is_not_running(client: TestClient) -> None:
    """Holding the claim is not the same as being runnable.

    ``blocked``, ``paused_quota`` and ``running_unknown`` all keep
    ``claimed_by`` - the row is still somebody's responsibility - and none of
    them may take a step. The last one is the one that matters: a turn that
    timed out with no proof it stopped must not be followed by another turn on
    the strength of a claim the dispatcher happens to still hold.
    """
    for ref, status in (
        ("no-step-blocked", "blocked"),
        ("no-step-quota", "paused_quota"),
        ("no-step-unknown", "running_unknown"),
    ):
        key = _claimed_task(client, ref)
        client.post(
            f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst", "status": status}
        )
        started = client.post(
            f"{TASKS_URL}/{key}/steps",
            json={"instance_id": "inst", "step_index": 0, "agent_name": "reviewer_agent"},
        )
        assert started.status_code == 409, (status, started.text)
        assert started.json()["error_code"] == "TASK_STATUS_CONFLICT"
