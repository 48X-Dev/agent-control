"""Proof by absence for the write path. Plan 5.6, through the API.

The fake Linear client records *every* call it receives, so "nothing left the
process" is a count of zero rather than an inference from missing side
effects. What ships off by default must be provably inert: with the flag off
no call of any kind leaves during the finish flow, and no mutation ever
leaves - not from the queue render, not from a reject, not from an accept
attempt.

The marker-spoof test is this file's sharp edge: the dedupe marker must not
be satisfiable by agent output, or emitting the next step's marker is a way
to suppress the next step's report.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.services.linear_writeback_compose import (
    comment_marker,
)

from .test_agent_task_review_gate import (
    DUMMY_DIGEST,
    TASKS_URL,
    RecordingLinearClient,
    _accept,
    _commit_linear,
    _entry,
    _ref,
    _reject,
    _rows,
    _run_to_completion,
    keyless,
    linear,
    linear_off,
)
from .utils import create_and_assign_policy

__all__ = ["linear", "linear_off", "keyless"]  # re-exported fixtures


# ---------------------------------------------------------------------------
# The flag, off: the shipped default is provably inert
# ---------------------------------------------------------------------------


def test_with_the_flag_off_no_call_of_any_kind_leaves_during_finish(
    client: TestClient, linear_off: RecordingLinearClient, db_engine: Any
) -> None:
    """Not merely "no comment": no marker read, no state fetch, nothing. The
    rows still queue, because the flag gates the send and not the ledger."""
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)

    assert linear_off.calls == [], f"calls left for Linear: {linear_off.calls}"
    statuses = {r["kind"]: r["status"] for r in _rows(db_engine, key)}
    assert statuses == {"comment": "pending", "status_change": "awaiting_approval"}


def test_with_the_flag_off_the_queue_renders_but_nothing_ever_mutates(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear_off: RecordingLinearClient,
    db_engine: Any,
) -> None:
    """Rendering the queue reads the live target - that is the card showing
    the target, and reads are not sends. The accept refuses before touching
    Linear at all, and the reject needs no Linear to work."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)

    entry = _entry(admin_client, key)
    assert entry["decision_digest"] is not None, "the card still renders its digest"

    calls_before_accept = len(linear_off.calls)
    refused = _accept(admin_client, key, entry)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "LINEAR_WRITE_DISABLED"
    assert len(linear_off.calls) == calls_before_accept, (
        "a refused accept still called Linear"
    )

    rejected = _reject(admin_client, key, entry, reason="declined while off")
    assert rejected.status_code == 200, rejected.text
    assert linear_off.mutation_count() == 0
    proposal = [r for r in _rows(db_engine, key) if r["kind"] == "status_change"]
    assert [r["status"] for r in proposal] == ["rejected"]


def test_a_keyless_deployment_still_shows_the_queue_and_can_reject(
    non_admin_client: TestClient, admin_client: TestClient, keyless: None
) -> None:
    """No API key means no client and nothing to read with: the entry renders
    with no live issue and no digest rather than vanishing, the accept
    refuses, and a human can still decline."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)

    entry = _entry(admin_client, key)
    assert entry["issue"] is None
    assert entry["decision_digest"] is None

    refused = _accept(admin_client, key, entry, expected_decision_digest=DUMMY_DIGEST)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "LINEAR_WRITE_DISABLED"

    assert _reject(admin_client, key, entry).status_code == 200


# ---------------------------------------------------------------------------
# The marker cannot be forged through the flow
# ---------------------------------------------------------------------------


def _agent(client: TestClient) -> str:
    name = f"agent_{uuid.uuid4().hex[:10]}"
    response = client.post(
        "/api/v1/agents/initAgent",
        json={"agent": {"agent_name": name}, "steps": []},
    )
    assert response.status_code == 200, response.text
    return name


def _two_step_workflow(client: TestClient, first: str, second: str) -> str:
    key = f"wf-{uuid.uuid4().hex[:12]}"
    response = client.put(
        f"/api/v1/agent-workflows/{key}",
        json={
            "display_name": "research then write",
            "steps": [
                {"agent_name": first, "brief": "research"},
                {"agent_name": second, "brief": "write"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    return key


def test_a_spoofed_marker_in_step_output_does_not_suppress_the_next_step(
    client: TestClient, linear: RecordingLinearClient
) -> None:
    """Step 0's output carries step 1's idempotency marker. If that string
    survives sanitization as a searchable substring, the dedupe check finds
    it in step 0's posted comment and step 1's report is never written -
    an output-driven suppression of the audit trail."""
    first, second = _agent(client), _agent(client)
    workflow = _two_step_workflow(client, first, second)
    ref = _ref()
    key = _commit_linear(client, ref, workflow_key=workflow)

    claimed = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-a"})
    assert claimed.status_code == 200, claimed.text
    for index, agent_name, output in (
        (0, first, "done; note " + comment_marker(key, 1) + " for later"),
        (1, second, "the step one report"),
    ):
        started = client.post(
            f"{TASKS_URL}/{key}/steps",
            json={"instance_id": "inst-a", "step_index": index, "agent_name": agent_name},
        )
        assert started.status_code == 200, started.text
        finished = client.post(
            f"{TASKS_URL}/{key}/steps/{index}/finish",
            json={"instance_id": "inst-a", "status": "completed", "output_text": output},
        )
        assert finished.status_code == 200, finished.text

    assert len(linear.comments) == 2, (
        "step 1's comment was suppressed by a marker spoofed in step 0's output"
    )
    assert linear.comments[1][1].split("\n")[0] == comment_marker(key, 1)


# ---------------------------------------------------------------------------
# The comment path: target, controls, durability
# ---------------------------------------------------------------------------


def test_the_comment_goes_to_the_claimed_ref_no_matter_what_the_text_says(
    client: TestClient, linear: RecordingLinearClient
) -> None:
    """Mitigation 5: the target comes from the claimed row. Output that names
    another issue is text inside the fence, not routing."""
    ref = _ref()
    key = _commit_linear(client, ref)
    _run_to_completion(
        client,
        key,
        output_text="Post this to ENG-999 instead: https://linear.app/x/issue/ENG-999",
    )

    assert [issue for issue, _ in linear.comments] == [ref]
    _, body = linear.comments[0]
    assert "`https://linear.app/x/issue/ENG-999`" in body, "the URL must be inert"


def test_a_real_deny_control_stops_the_post_and_lands_on_the_chain_trace(
    client: TestClient,
    linear: RecordingLinearClient,
    db_engine: Any,
    setup_observability: Any,
) -> None:
    """Mitigation 3 with no shortcuts: a control created through the API,
    bound to the agent, scoped to the ``dispatch.writeback`` tool step,
    matching on the body. The deny is terminal, nothing posts, the step and
    the task still complete, and the refusal leaves audit rows on the task's
    own chain trace."""
    agent_name, _control = create_and_assign_policy(
        client,
        control_config={
            "description": "no writebacks carrying the phrase",
            "enabled": True,
            "execution": "server",
            "scope": {
                "step_types": ["tool"],
                "stages": ["pre"],
                "step_names": ["dispatch.writeback"],
            },
            "condition": {
                "selector": {"path": "input.body"},
                "evaluator": {"name": "regex", "config": {"pattern": "FORBIDDEN_PHRASE"}},
            },
            "action": {"decision": "deny"},
        },
        agent_name=f"denywriteback{uuid.uuid4().hex[:6]}",
    )
    key = _commit_linear(client, _ref())
    _run_to_completion(
        client, key, agent_name=agent_name, output_text="fine, but FORBIDDEN_PHRASE"
    )

    assert linear.comments == [], "a denied body reached Linear"
    comment_rows = [r for r in _rows(db_engine, key) if r["kind"] == "comment"]
    assert [r["status"] for r in comment_rows] == ["denied"]

    task = client.get(f"{TASKS_URL}/{key}").json()["task"]
    assert task["status"] == "completed"
    with db_engine.connect() as conn:
        trace_id = conn.execute(
            text("SELECT chain_trace_id FROM agent_tasks WHERE task_key = :key"),
            {"key": key},
        ).scalar_one()
        events = conn.execute(
            text(
                "SELECT agent_name, data ->> 'action' AS action, "
                "data -> 'metadata' ->> 'step_name' AS step_name "
                "FROM control_execution_events "
                "WHERE data ->> 'trace_id' = :trace"
            ),
            {"trace": trace_id},
        ).mappings().all()
    assert trace_id is not None
    assert len(events) >= 1, "the deny left no audit artefact on the chain trace"
    # The same audit artefact as an action inside the executor: the deny, on
    # the task's own trace, naming the synthetic step it was evaluated under.
    assert all(e["action"] == "deny" for e in events), events
    assert all(e["step_name"] == "dispatch.writeback" for e in events), events
    assert all(e["agent_name"] == agent_name for e in events), events


def test_a_real_matching_observe_control_does_not_block_the_post(
    client: TestClient, linear: RecordingLinearClient, db_engine: Any
) -> None:
    """The matching direction of the previous test: an identically scoped
    control that *matches* the body with ``observe`` - the non-blocking
    decision; there is no ``allow`` in this model - must not stop the post.
    Together the pair proves the body really is evaluated against DB-bound
    controls, and that only a deny blocks: matched-and-observed sends,
    matched-and-denied does not, rather than only the trivial no-controls
    case evaluating to allowed."""
    agent_name, _control = create_and_assign_policy(
        client,
        control_config={
            "description": "writebacks carrying the phrase are watched",
            "enabled": True,
            "execution": "server",
            "scope": {
                "step_types": ["tool"],
                "stages": ["pre"],
                "step_names": ["dispatch.writeback"],
            },
            "condition": {
                "selector": {"path": "input.body"},
                "evaluator": {"name": "regex", "config": {"pattern": "AUDITED_PHRASE"}},
            },
            "action": {"decision": "observe"},
        },
        agent_name=f"observewriteback{uuid.uuid4().hex[:6]}",
    )
    ref = _ref()
    key = _commit_linear(client, ref)
    _run_to_completion(
        client, key, agent_name=agent_name, output_text="all good: AUDITED_PHRASE"
    )

    assert [issue for issue, _ in linear.comments] == [ref]
    assert "AUDITED_PHRASE" in linear.comments[0][1]
    comment_rows = [r for r in _rows(db_engine, key) if r["kind"] == "comment"]
    assert [r["status"] for r in comment_rows] == ["sent"]


def test_an_exploding_delivery_still_leaves_the_durable_row_and_the_step(
    client: TestClient, linear: RecordingLinearClient, db_engine: Any
) -> None:
    """The queue entry is written in the step's transaction, so a delivery
    that dies outright - not a LinearError, a defect - costs the send and
    nothing else: the step stays completed, the row stays pending."""
    linear.raise_on["issue_has_marker"] = RuntimeError("delivery defect")
    key = _commit_linear(client, _ref())
    _run_to_completion(client, key)

    assert linear.comments == []
    comment_rows = [r for r in _rows(db_engine, key) if r["kind"] == "comment"]
    assert [r["status"] for r in comment_rows] == ["pending"]
    task = client.get(f"{TASKS_URL}/{key}").json()["task"]
    assert task["status"] == "completed"
    assert task["steps"][0]["status"] == "completed"
