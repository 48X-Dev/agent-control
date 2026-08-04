"""Redelivery: no comment row is stranded, and none is invisible. Plan 5.6.

"Queued in its own table and retried independently of the task" has to mean a
row left ``pending`` or ``failed`` still has a way to Linear after its first
attempt missed. Three surfaces hold that promise: the finish-task pass retries
whatever the finish-step attempt left behind; the operator deliver route
reaches rows those can no longer touch (a task that finished while the flag
was off, or while Linear was down); and the task detail lists every row, so
"done here" and "written there" are two visible facts rather than one fact
and a mystery.

The sharp edge is the last refusal: the deliver route must never move a
``status_change`` row. A redelivery door that accepted one would be an accept
with no digest, no self-approval check and no named approver.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.services.linear_client import LinearError

from .test_agent_task_review_gate import (
    TASKS_URL,
    RecordingLinearClient,
    _commit_linear,
    _install_runtime,
    _ref,
    _rows,
    _run_to_completion,
    keyless,
    linear,
    linear_off,
)

__all__ = ["linear", "linear_off", "keyless"]  # re-exported fixtures


def _deliver(client: TestClient, task_key: str, writeback_id: int) -> Any:
    return client.post(f"{TASKS_URL}/{task_key}/writebacks/{writeback_id}/deliver")


def _comment_row(db_engine: Any, task_key: str) -> dict[str, Any]:
    rows = [r for r in _rows(db_engine, task_key) if r["kind"] == "comment"]
    assert len(rows) == 1, rows
    return rows[0]


def _proposal_row(db_engine: Any, task_key: str) -> dict[str, Any]:
    rows = [r for r in _rows(db_engine, task_key) if r["kind"] == "status_change"]
    assert len(rows) == 1, rows
    return rows[0]


# ---------------------------------------------------------------------------
# The finish-task pass: the retry the 409 on re-finish would otherwise forbid
# ---------------------------------------------------------------------------


def test_a_row_failed_at_the_step_is_retried_and_sent_when_the_task_finishes(
    client: TestClient, linear: RecordingLinearClient, db_engine: Any
) -> None:
    """A Linear blip at finish-step marks the row ``failed``; re-finishing the
    step is a 409, so without the finish-task pass that row would never move
    again. The retry sends it, once: the pass selects only pending and failed
    rows, so a row the step already sent is not posted twice."""
    ref = _ref()
    key = _commit_linear(client, ref)
    claimed = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-a"})
    assert claimed.status_code == 200, claimed.text
    started = client.post(
        f"{TASKS_URL}/{key}/steps",
        json={"instance_id": "inst-a", "step_index": 0, "agent_name": "retry_agent"},
    )
    assert started.status_code == 200, started.text

    linear.raise_on["create_comment"] = LinearError("Linear reported an internal error.")
    finished = client.post(
        f"{TASKS_URL}/{key}/steps/0/finish",
        json={"instance_id": "inst-a", "status": "completed", "output_text": "done"},
    )
    assert finished.status_code == 200, finished.text
    assert _comment_row(db_engine, key)["status"] == "failed"

    linear.raise_on.clear()
    done = client.post(
        f"{TASKS_URL}/{key}/finish",
        json={"instance_id": "inst-a", "status": "completed"},
    )
    assert done.status_code == 200, done.text

    assert [issue for issue, _ in linear.comments] == [ref]
    row = _comment_row(db_engine, key)
    assert row["status"] == "sent"
    assert row["attempts"] == 2, "one failed attempt, one successful retry"


def test_a_happy_run_still_posts_exactly_one_comment(
    client: TestClient, linear: RecordingLinearClient
) -> None:
    """The finish-task pass must be a retry, not a repeat: after a clean run
    the step's own send already moved the row to ``sent`` and the pass finds
    nothing to attempt."""
    ref = _ref()
    key = _commit_linear(client, ref)
    _run_to_completion(client, key)

    assert [issue for issue, _ in linear.comments] == [ref]


# ---------------------------------------------------------------------------
# The operator deliver route: the long tail the automatic attempts missed
# ---------------------------------------------------------------------------


def test_rows_stranded_by_a_flag_off_run_send_once_writes_are_enabled(
    app: Any, client: TestClient, linear_off: RecordingLinearClient, db_engine: Any
) -> None:
    """The reviewer's worst case: a deployment runs with the flag off, the
    queue fills, the flag is enabled later. The finish routes have already
    run, so the deliver route is what turns the backlog into comments."""
    ref = _ref()
    key = _commit_linear(client, ref)
    _run_to_completion(client, key)
    row = _comment_row(db_engine, key)
    assert row["status"] == "pending"
    assert linear_off.comments == []

    # The operator enables writes; same client, the flag flips.
    _install_runtime(app, linear_off, write_enabled=True)
    delivered = _deliver(client, key, row["id"])
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["writeback"]["status"] == "sent"

    assert [issue for issue, _ in linear_off.comments] == [ref]
    assert _comment_row(db_engine, key)["status"] == "sent"
    # The proposal is untouched: enabling comment delivery is not an accept.
    assert _proposal_row(db_engine, key)["status"] == "awaiting_approval"


def test_deliver_refuses_a_status_change_row_no_matter_the_flag(
    client: TestClient, linear: RecordingLinearClient, db_engine: Any
) -> None:
    """The review gate holding. With the flag on and a working client, the
    deliver route still refuses the proposal row outright: a deliver that
    sent one would close an issue with no digest and no named approver."""
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)
    proposal = _proposal_row(db_engine, key)
    mutations_before = linear.mutation_count()

    refused = _deliver(client, key, proposal["id"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "TASK_STATUS_CONFLICT"
    assert _proposal_row(db_engine, key)["status"] == "awaiting_approval"
    assert linear.mutation_count() == mutations_before


def test_deliver_refuses_while_the_write_flag_is_off(
    client: TestClient, linear_off: RecordingLinearClient, db_engine: Any
) -> None:
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)
    row = _comment_row(db_engine, key)

    refused = _deliver(client, key, row["id"])

    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "LINEAR_WRITE_DISABLED"
    assert linear_off.mutation_count() == 0


def test_deliver_refuses_a_row_already_sent_and_a_row_a_control_denied(
    client: TestClient, linear: RecordingLinearClient, db_engine: Any
) -> None:
    """``sent`` is finished and ``denied`` is terminal - the same body
    reproduces the same refusal, so there is nothing to retry around."""
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)
    row = _comment_row(db_engine, key)
    assert row["status"] == "sent"

    refused = _deliver(client, key, row["id"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "TASK_STATUS_CONFLICT"

    with db_engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_task_writebacks SET status = 'denied' WHERE id = :id"),
            {"id": row["id"]},
        )
    still_refused = _deliver(client, key, row["id"])
    assert still_refused.status_code == 409, still_refused.text
    assert still_refused.json()["error_code"] == "TASK_STATUS_CONFLICT"
    assert len(linear.comments) == 1, "a refused deliver still posted"


def test_deliver_404s_for_a_row_the_task_does_not_have(
    client: TestClient, linear: RecordingLinearClient
) -> None:
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)

    missing = _deliver(client, key, 999999)

    assert missing.status_code == 404, missing.text
    assert missing.json()["error_code"] == "AGENT_TASK_WRITEBACK_NOT_FOUND"


# ---------------------------------------------------------------------------
# The read surface: both facts, visible
# ---------------------------------------------------------------------------


def test_the_task_detail_lists_every_writeback_row_with_its_status(
    client: TestClient, linear_off: RecordingLinearClient, db_engine: Any
) -> None:
    """With the flag off the task completes and nothing reaches Linear, and
    the detail must say exactly that: a ``pending`` comment and a waiting
    proposal, beside a ``completed`` task. Without this list the console
    could render "done here" but never "not written there"."""
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key, output_text="the durable record")

    task = client.get(f"{TASKS_URL}/{key}").json()["task"]
    assert task["status"] == "completed"
    by_kind = {w["kind"]: w for w in task["writebacks"]}
    assert set(by_kind) == {"comment", "status_change"}
    assert by_kind["comment"]["status"] == "pending"
    assert by_kind["comment"]["step_index"] == 0
    assert by_kind["status_change"]["status"] == "awaiting_approval"
    assert by_kind["status_change"]["body"] == "the durable record"
    assert {w["task_key"] for w in task["writebacks"]} == {key}


def test_the_detail_row_moves_to_sent_after_a_successful_deliver(
    app: Any, client: TestClient, linear_off: RecordingLinearClient, db_engine: Any
) -> None:
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)
    row = _comment_row(db_engine, key)

    _install_runtime(app, linear_off, write_enabled=True)
    assert _deliver(client, key, row["id"]).status_code == 200

    task = client.get(f"{TASKS_URL}/{key}").json()["task"]
    statuses = {w["kind"]: w["status"] for w in task["writebacks"]}
    assert statuses == {"comment": "sent", "status_change": "awaiting_approval"}
