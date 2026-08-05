"""One decision per proposal, held under real concurrency. Plan 5.7.

``TestClient`` serializes requests, so the sibling gate file can only prove
accept-then-reject in sequence. The race worth a test is simultaneous:
accept's check-then-act spans a live Linear round trip, and without the row
lock in ``_require_pair`` a reject arriving inside that window answers 200 -
an audit row that says nothing changed while the accept goes on to close the
issue. Here the fake Linear client parks the accept mid-mutation over a real
socket, a reject arrives while it is parked, and the assertions are that the
reject *waits* and then refuses on status, that exactly one state mutation
left for Linear, and that the surviving row records the accept.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_control_server.services.linear_writeback_runtime import get_writeback_runtime

from .conftest import TEST_ADMIN_API_KEY, TEST_API_KEY, LiveServerFactory
from .test_agent_task_review_gate import (
    TASKS_URL,
    RecordingLinearClient,
    _install_runtime,
    _rows,
)

_GATE_TIMEOUT = 5.0
"""How long the test waits for the accept to reach the parked mutation, and
for either request to finish once released. Generous for a loopback socket,
short enough that a hang fails the test rather than the suite."""


class HoldableLinearClient(RecordingLinearClient):
    """The recording fake, with a parking brake inside the state mutation.

    ``entered_update`` is set the moment an accept reaches
    ``update_issue_state`` - by which point it holds the proposal row's lock -
    and the mutation does not return until the test sets ``release_update``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered_update = asyncio.Event()
        self.release_update = asyncio.Event()

    async def update_issue_state(self, *, issue_id: str, state_id: str) -> None:
        self.entered_update.set()
        await asyncio.wait_for(self.release_update.wait(), timeout=_GATE_TIMEOUT)
        await super().update_issue_state(issue_id=issue_id, state_id=state_id)


@pytest.fixture()
def linear_holdable(app: FastAPI):
    fake = HoldableLinearClient()
    installed = _install_runtime(app, fake, write_enabled=True)
    assert installed is fake
    yield fake
    app.dependency_overrides.pop(get_writeback_runtime, None)


def _ref() -> str:
    return f"race-{uuid.uuid4().hex[:12]}"


async def _commit_linear(api: httpx.AsyncClient, ref: str) -> str:
    scope = {
        "kind": "items",
        "source_kind": "linear",
        "items": [{"source_ref": ref, "title": f"title for {ref}"}],
    }
    preview = await api.post(
        f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview", "dry_run": False}
    )
    assert preview.status_code == 200, preview.text
    commit = await api.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "dry_run": False,
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )
    assert commit.status_code == 200, commit.text
    return str(commit.json()["task_keys"][0])


async def _run_to_completion(api: httpx.AsyncClient, task_key: str) -> None:
    claimed = await api.post(
        f"{TASKS_URL}/{task_key}/claim", json={"instance_id": "inst-race"}
    )
    assert claimed.status_code == 200, claimed.text
    started = await api.post(
        f"{TASKS_URL}/{task_key}/steps",
        json={"instance_id": "inst-race", "step_index": 0, "agent_name": "race_agent"},
    )
    assert started.status_code == 200, started.text
    finished = await api.post(
        f"{TASKS_URL}/{task_key}/steps/0/finish",
        json={"instance_id": "inst-race", "status": "completed", "output_text": "raced"},
    )
    assert finished.status_code == 200, finished.text
    done = await api.post(
        f"{TASKS_URL}/{task_key}/finish",
        json={"instance_id": "inst-race", "status": "completed"},
    )
    assert done.status_code == 200, done.text


async def test_a_reject_during_a_live_accept_waits_and_then_refuses(
    live_server_factory: LiveServerFactory,
    app: FastAPI,
    linear_holdable: HoldableLinearClient,
    db_engine: Any,
) -> None:
    live = await live_server_factory(app)
    approver = live.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    runner = live.client(headers={"X-API-Key": TEST_API_KEY})

    ref = _ref()
    key = await _commit_linear(approver, ref)
    await _run_to_completion(runner, key)

    review = await approver.get(f"{TASKS_URL}/review")
    assert review.status_code == 200, review.text
    entry = next(e for e in review.json()["entries"] if e["task_key"] == key)
    assert entry["decision_digest"] is not None

    accept_task = asyncio.create_task(
        approver.post(
            f"{TASKS_URL}/{key}/accept",
            json={
                "writeback_id": entry["writeback_id"],
                "expected_decision_digest": entry["decision_digest"],
            },
        )
    )
    try:
        await asyncio.wait_for(
            linear_holdable.entered_update.wait(), timeout=_GATE_TIMEOUT
        )

        reject_task = asyncio.create_task(
            approver.post(
                f"{TASKS_URL}/{key}/reject",
                json={"writeback_id": entry["writeback_id"], "reason": "raced"},
            )
        )
        try:
            done, _pending = await asyncio.wait({reject_task}, timeout=0.3)
            assert not done, (
                "the reject answered while the accept still held the row; "
                "the decision paths are not serialized on the proposal"
            )
        finally:
            linear_holdable.release_update.set()

        accept_response = await asyncio.wait_for(accept_task, timeout=_GATE_TIMEOUT)
        reject_response = await asyncio.wait_for(reject_task, timeout=_GATE_TIMEOUT)
    finally:
        linear_holdable.release_update.set()
        accept_task.cancel()

    assert accept_response.status_code == 200, accept_response.text
    assert reject_response.status_code == 409, reject_response.text
    assert reject_response.json()["error_code"] == "TASK_STATUS_CONFLICT"
    assert "sent" in reject_response.json()["detail"]

    assert linear_holdable.state_updates == [(ref, "state-done")]
    status_rows = [r for r in _rows(db_engine, key) if r["kind"] == "status_change"]
    assert len(status_rows) == 1
    assert status_rows[0]["status"] == "sent"
    assert status_rows[0]["rejected_reason"] is None

    after = await approver.get(f"{TASKS_URL}/review")
    assert all(e["task_key"] != key for e in after.json()["entries"])
