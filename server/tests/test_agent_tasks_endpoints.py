"""What the dispatch ledger's routes do, asserted through the API.

Every test here is about behaviour a caller can observe, and several of them
are about behaviour that must *not* happen. Those are the ones worth naming up
front, because an absence is the thing a suite most easily fails to check.

**No field on an import selects an agent.** The whole injection argument in the
plan rests on it: an issue body is written by whoever has tracker access, so if
anything the source can express reached a decision about which agent runs, an
attacker who can file an issue would be choosing the agent. The item model
forbids extras, and the tests below try to smuggle one in.

**No caller supplies a lease, a deadline, a trace or an instance's authority.**
A caller that could set its own lease could reclaim live work; one that could
set its own deadline would have no deadline; one that could set the chain trace
would be authoring its own audit key. Each of those is a request field that
does not exist, and the tests assert the request is rejected rather than
quietly ignored.

**A namespace sees its own rows and nothing else.** Not merely on the list: a
task key from another namespace is a 404 on every route that takes one, and the
same source ref may be imported independently on both sides.

The concurrency properties - two dispatchers racing, a lease expiring, a dead
holder's late write - are in ``test_agent_tasks_concurrency.py``, because
``TestClient`` serializes requests and cannot express them.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

import agent_control_server
from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.config import dispatch_settings

TASKS_URL = "/api/v1/agent-tasks"

STEP_AGENT = "reviewer_agent"


def _ref(prefix: str = "ref") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _scope(*refs: str, source_kind: str = "file") -> dict[str, Any]:
    return {
        "kind": "items",
        "source_kind": source_kind,
        "items": [{"source_ref": ref, "title": f"title for {ref}"} for ref in refs],
    }


def _preview(client: TestClient, *refs: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"scope": _scope(*refs), "mode": "preview"}
    body.update(extra)
    response = client.post(f"{TASKS_URL}/import", json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _commit(client: TestClient, *refs: str, **extra: Any) -> dict[str, Any]:
    """Preview, then commit against the digest the preview returned."""
    preview = _preview(client, *refs, **extra)
    body: dict[str, Any] = {
        "scope": _scope(*refs),
        "mode": "commit",
        "expected_refs_digest": preview["refs_digest"],
    }
    body.update(extra)
    response = client.post(f"{TASKS_URL}/import", json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _commit_items(client: TestClient, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Preview then commit an explicit item list, for the fields ``_scope`` omits."""
    scope = {"kind": "items", "source_kind": "file", "items": items}
    preview = client.post(f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview"})
    assert preview.status_code == 200, preview.text
    response = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _one_task(client: TestClient, ref: str | None = None, **extra: Any) -> str:
    ref = ref or _ref()
    created = _commit(client, ref, **extra)
    assert created["created"] == 1, created
    return str(created["task_keys"][0])


def _claim(client: TestClient, task_key: str, instance: str = "inst-a") -> dict[str, Any]:
    response = client.post(f"{TASKS_URL}/{task_key}/claim", json={"instance_id": instance})
    assert response.status_code == 200, response.text
    return dict(response.json())


def _start_step(
    client: TestClient, task_key: str, *, index: int = 0, instance: str = "inst-a", **extra: Any
) -> Any:
    body: dict[str, Any] = {
        "instance_id": instance,
        "step_index": index,
        "agent_name": STEP_AGENT,
    }
    body.update(extra)
    return client.post(f"{TASKS_URL}/{task_key}/steps", json=body)


def _finish_step(
    client: TestClient,
    task_key: str,
    *,
    index: int = 0,
    instance: str = "inst-a",
    status: str = "completed",
    **extra: Any,
) -> Any:
    body: dict[str, Any] = {"instance_id": instance, "status": status}
    body.update(extra)
    return client.post(f"{TASKS_URL}/{task_key}/steps/{index}/finish", json=body)


# ---------------------------------------------------------------------------
# Import: the preview is what makes the commit an authorization
# ---------------------------------------------------------------------------


def test_a_preview_returns_the_items_themselves_and_writes_nothing(
    client: TestClient,
) -> None:
    """A count is not an authorization. The operator agrees to a set.

    An attacker with tracker access files an item into the targeted scope and
    is inside the enumerated set; somebody who expected four and is shown "5
    items" presses anyway, because 5 and 4 look the same at a glance.
    """
    one, two = _ref("alpha"), _ref("beta")

    preview = _preview(client, one, two)

    assert preview["mode"] == "preview"
    assert {item["source_ref"] for item in preview["eligible"]} == {one, two}
    assert {item["title"] for item in preview["eligible"]} == {
        f"title for {one}",
        f"title for {two}",
    }
    assert preview["created"] == 0
    assert preview["task_keys"] == []
    assert preview["refs_digest"].startswith("sha256:")

    listed = client.get(TASKS_URL)
    assert listed.status_code == 200, listed.text
    assert listed.json()["pagination"]["total"] == 0, "a preview inserted a row"


def test_a_commit_without_the_digest_is_refused(client: TestClient) -> None:
    body = {"scope": _scope(_ref()), "mode": "commit"}

    response = client.post(f"{TASKS_URL}/import", json=body)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SCOPE_CHANGED"
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 0


def test_a_substituted_set_of_the_same_size_is_refused_and_creates_nothing(
    client: TestClient,
) -> None:
    """The digest is over the set, not the count.

    Four items replaced by four different items has the same count and a
    different digest. A confirm bound to a count could not tell them apart.
    """
    shown = [_ref("shown") for _ in range(4)]
    preview = _preview(client, *shown)

    swapped = [*shown[:3], _ref("swapped")]
    response = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": _scope(*swapped),
            "mode": "commit",
            "expected_refs_digest": preview["refs_digest"],
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SCOPE_CHANGED"
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 0, (
        "a refused commit must create nothing at all"
    )


def test_committing_twice_queues_nothing_the_second_time(client: TestClient) -> None:
    ref = _ref()
    first = _commit(client, ref)
    assert first["created"] == 1

    second = _preview(client, ref)

    assert second["eligible"] == []
    assert second["skipped"]["already_queued"] == 1
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 1


def test_a_finished_ref_is_reported_but_not_requeued_unless_asked(
    client: TestClient,
) -> None:
    """A terminal task frees the ref by the index; doing it unasked spends twice.

    An unattended loop re-reading the same source would otherwise pay for the
    same work on every pass, so re-running finished work is an operator's
    decision and shows up as one.
    """
    ref = _ref()
    key = _one_task(client, ref)
    _claim(client, key)
    finished = client.post(
        f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst-a", "status": "completed"}
    )
    assert finished.status_code == 200, finished.text

    default = _preview(client, ref)
    assert default["eligible"] == []
    assert default["skipped"]["already_worked"] == 1

    asked = _preview(client, ref, requeue_completed=True)
    assert [item["source_ref"] for item in asked["eligible"]] == [ref]
    assert asked["skipped"]["already_worked"] == 1, "still reported either way"


def test_an_import_body_cannot_name_an_agent(client: TestClient) -> None:
    """Nothing the source can express reaches a decision about who runs it."""
    body = {
        "scope": {
            "kind": "items",
            "source_kind": "file",
            "items": [
                {"source_ref": _ref(), "title": "t", "agent_name": "attacker_agent"}
            ],
        },
        "mode": "preview",
    }

    response = client.post(f"{TASKS_URL}/import", json=body)

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "field, value",
    [
        ("labels", ["agent:attacker"]),
        ("priority", 1),
        ("workflow_key", "attacker-workflow"),
        ("dry_run", False),
    ],
)
def test_an_import_item_carries_nothing_that_could_decide_anything(
    client: TestClient, field: str, value: Any
) -> None:
    item: dict[str, Any] = {"source_ref": _ref(), "title": "t", field: value}
    response = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": {"kind": "items", "source_kind": "file", "items": [item]},
            "mode": "preview",
        },
    )

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "data:text/html,<script>", "file:///etc/passwd"]
)
def test_a_source_url_the_console_would_render_as_a_link_is_scheme_checked(
    client: TestClient, url: str
) -> None:
    """The confirm screen's whole job is to let an operator check provenance.

    That link is the one part of an untrusted item the screen makes clickable,
    and it arrives from whoever can file into the source.
    """
    response = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": {
                "kind": "items",
                "source_kind": "file",
                "items": [{"source_ref": _ref(), "title": "t", "source_url": url}],
            },
            "mode": "preview",
        },
    )

    assert response.status_code == 422, response.text


def test_the_same_ref_twice_in_one_body_is_a_caller_bug(client: TestClient) -> None:
    ref = _ref()
    response = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": {
                "kind": "items",
                "source_kind": "file",
                "items": [
                    {"source_ref": ref, "title": "one"},
                    {"source_ref": ref, "title": "two"},
                ],
            },
            "mode": "preview",
        },
    )

    assert response.status_code == 422, response.text


def test_an_import_above_the_deployments_ceiling_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling no code path reads is not a ceiling, however documented."""
    monkeypatch.setattr(dispatch_settings, "max_import_items", 2)

    response = client.post(
        f"{TASKS_URL}/import",
        json={"scope": _scope(_ref(), _ref(), _ref()), "mode": "preview"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_STATUS_CONFLICT"


def test_the_same_ref_under_two_source_kinds_is_two_different_items(
    client: TestClient,
) -> None:
    """Dedup is on ``(namespace, source_kind, source_ref)``, and it has to be.

    A file line id and a Linear issue id are ids in different spaces, and a
    collision between them is a coincidence rather than the same work.
    """
    ref = _ref()
    _commit(client, ref)

    linear_scope = _scope(ref, source_kind="linear")
    preview = client.post(
        f"{TASKS_URL}/import", json={"scope": linear_scope, "mode": "preview"}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["skipped"]["already_queued"] == 0
    linear = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": linear_scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )

    assert linear.status_code == 200, linear.text
    assert linear.json()["created"] == 1
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 2


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_the_list_is_oldest_first_and_pages_with_its_own_cursor(
    client: TestClient,
) -> None:
    """Oldest first because this is the claim poll as well as a console list."""
    refs = [_ref(f"page{index}") for index in range(5)]
    for ref in refs:
        _one_task(client, ref)

    first = client.get(f"{TASKS_URL}?limit=2")
    assert first.status_code == 200, first.text
    page_one = first.json()
    assert [task["source_ref"] for task in page_one["tasks"]] == refs[:2]
    assert page_one["pagination"]["total"] == 5
    assert page_one["pagination"]["has_more"] is True

    second = client.get(f"{TASKS_URL}?limit=2&cursor={page_one['pagination']['next_cursor']}")
    assert [task["source_ref"] for task in second.json()["tasks"]] == refs[2:4]


def test_the_list_filters_by_status_and_by_team(client: TestClient) -> None:
    queued = _ref("queued")
    running = _ref("running")
    _one_task(client, queued, team_slug="marketing")
    running_key = _one_task(client, running, team_slug="operations")
    _claim(client, running_key)

    by_status = client.get(f"{TASKS_URL}?status=running")
    assert [task["source_ref"] for task in by_status.json()["tasks"]] == [running]

    by_team = client.get(f"{TASKS_URL}?team=marketing")
    assert [task["source_ref"] for task in by_team.json()["tasks"]] == [queued]


def test_a_list_row_carries_the_title_and_not_the_body(client: TestClient) -> None:
    """A queue poll that shipped every description moves megabytes to pick a key."""
    ref = _ref()
    _commit_items(
        client, [{"source_ref": ref, "title": "the title", "body": "the body"}]
    )

    row = client.get(TASKS_URL).json()["tasks"][0]
    assert row["title"] == "the title"
    assert "body" not in row

    detail = client.get(f"{TASKS_URL}/{row['task_key']}").json()["task"]
    assert detail["body"] == "the body"
    assert detail["steps"] == []


def test_an_unknown_task_key_is_a_not_found(client: TestClient) -> None:
    response = client.get(f"{TASKS_URL}/{uuid.uuid4().hex}")

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "AGENT_TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Claim, lease and the fields a caller does not get to set
# ---------------------------------------------------------------------------


def test_a_claim_mints_the_trace_and_sets_the_lease_from_deployment_settings(
    client: TestClient,
) -> None:
    key = _one_task(client)

    claimed = _claim(client, key)

    assert claimed["prior_status"] == "queued"
    assert claimed["reclaimed"] is False
    assert claimed["resume_step_index"] == 0
    assert claimed["abandoned_step_indexes"] == []
    assert claimed["lease_seconds"] == dispatch_settings.task_lease_seconds
    assert claimed["task"]["status"] == "running"
    assert claimed["task"]["claimed_by"] == "inst-a"
    assert claimed["task"]["chain_trace_id"], "the chain trace is server-minted at claim"


@pytest.mark.parametrize(
    "extra",
    [
        {"chain_trace_id": "deadbeef"},
        {"lease_seconds": 5},
        {"deadline_at": "2099-01-01T00:00:00Z"},
        {"agent_name": "attacker_agent"},
    ],
)
def test_a_claim_body_cannot_carry_a_trace_a_lease_a_deadline_or_an_agent(
    client: TestClient, extra: dict[str, Any]
) -> None:
    """Rejected rather than ignored, so a caller cannot believe it worked."""
    key = _one_task(client)

    response = client.post(
        f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-a", **extra}
    )

    assert response.status_code == 422, response.text


def test_a_second_claim_on_a_live_lease_is_refused(client: TestClient) -> None:
    key = _one_task(client)
    _claim(client, key, "inst-a")

    second = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-b"})

    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "TASK_ALREADY_CLAIMED"
    assert client.get(f"{TASKS_URL}/{key}").json()["task"]["claimed_by"] == "inst-a"


def test_a_heartbeat_from_something_that_is_not_the_holder_is_refused(
    client: TestClient,
) -> None:
    key = _one_task(client)
    _claim(client, key, "inst-a")

    response = client.post(
        f"{TASKS_URL}/{key}/heartbeat", json={"instance_id": "inst-b"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_NOT_CLAIMED"


def test_a_heartbeat_moves_the_lease_and_leaves_the_deadline_alone(
    client: TestClient,
) -> None:
    """The deadline is the ceiling. A lease a holder refreshes must not move it."""
    key = _one_task(client)
    claimed = _claim(client, key)

    beat = client.post(f"{TASKS_URL}/{key}/heartbeat", json={"instance_id": "inst-a"})

    assert beat.status_code == 200, beat.text
    body = beat.json()
    assert body["status"] == "running"
    assert body["deadline_at"] == claimed["task"]["deadline_at"]
    assert dt.datetime.fromisoformat(body["lease_expires_at"]) >= dt.datetime.fromisoformat(
        claimed["lease_expires_at"]
    )


def test_a_heartbeat_against_a_finished_task_is_refused(client: TestClient) -> None:
    """Answering it cheerfully would keep a dispatcher that has lost track going."""
    key = _one_task(client)
    _claim(client, key)
    client.post(
        f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst-a", "status": "completed"}
    )

    response = client.post(f"{TASKS_URL}/{key}/heartbeat", json={"instance_id": "inst-a"})

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_NOT_CLAIMED"


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def test_only_the_holder_writes_steps(client: TestClient) -> None:
    key = _one_task(client)
    _claim(client, key, "inst-a")

    started = _start_step(client, key, instance="inst-b")

    assert started.status_code == 409, started.text
    assert started.json()["error_code"] == "TASK_NOT_CLAIMED"
    assert client.get(f"{TASKS_URL}/{key}").json()["task"]["steps"] == []


def test_a_step_may_not_start_on_an_unclaimed_task(client: TestClient) -> None:
    key = _one_task(client)

    started = _start_step(client, key)

    assert started.status_code == 409, started.text
    assert started.json()["error_code"] == "TASK_NOT_CLAIMED"


def test_a_step_row_exists_before_its_turn_does(client: TestClient) -> None:
    """A row that only exists once its turn succeeded cannot record the case
    the table was added for: a hop that reached the executor and never came
    back."""
    key = _one_task(client)
    _claim(client, key)

    started = _start_step(client, key, brief="find the common causes")

    assert started.status_code == 200, started.text
    step = started.json()["step"]
    assert step["status"] == "running"
    assert step["step_index"] == 0
    assert step["attempts"] == 1
    assert step["output_text"] is None
    assert step["ended_at"] is None
    assert started.json()["task"]["current_step"] == 0, "nothing has completed yet"


def test_a_step_index_at_the_ceiling_is_refused(client: TestClient) -> None:
    """A workflow that can keep numbering steps is a workflow that can loop."""
    key = _one_task(client)
    _claim(client, key)

    at_the_cap = _start_step(client, key, index=dispatch_settings.max_steps_per_task)

    assert at_the_cap.status_code == 422, at_the_cap.text


def test_a_step_index_beyond_a_lowered_ceiling_is_refused_by_the_server(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch_settings, "max_steps_per_task", 1)
    key = _one_task(client)
    _claim(client, key)

    response = _start_step(client, key, index=1)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_STATUS_CONFLICT"


def test_no_step_starts_past_the_deadline(client: TestClient, db_engine: Any) -> None:
    """The ceiling that bounds a hung dispatcher is on this side of the wire."""
    key = _one_task(client)
    _claim(client, key)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_tasks SET deadline_at = now() - interval '1 minute' "
                " WHERE task_key = :key"
            ),
            {"key": key},
        )

    response = _start_step(client, key)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_DEADLINE_EXCEEDED"


def test_finishing_a_step_writes_the_output_and_moves_the_task(
    client: TestClient,
) -> None:
    key = _one_task(client)
    _claim(client, key)
    _start_step(client, key)

    finished = _finish_step(
        client, key, output_text="three incidents shared one cause", turn_trace_id="tr-1"
    )

    assert finished.status_code == 200, finished.text
    step = finished.json()["step"]
    assert step["status"] == "completed"
    assert step["output_text"] == "three incidents shared one cause"
    assert step["turn_trace_id"] == "tr-1"
    assert step["ended_at"] is not None
    task = finished.json()["task"]
    assert task["current_step"] == 1
    assert task["turns_used"] == 1


def test_a_failed_step_is_charged_but_does_not_advance_the_chain(
    client: TestClient,
) -> None:
    """A turn that reached the executor is charged whether or not it produced
    anything; ``current_step`` moves only on a completed step, because that is
    what the resume rule counts."""
    key = _one_task(client)
    _claim(client, key)
    _start_step(client, key)

    finished = _finish_step(client, key, status="failed", failure_code="EMPTY_STEP_OUTPUT")

    task = finished.json()["task"]
    assert task["turns_used"] == 1
    assert task["current_step"] == 0


def test_a_dispatcher_cannot_report_a_step_as_abandoned(client: TestClient) -> None:
    """``abandoned`` is what the server writes on reclaim. A dispatcher
    claiming it would be reporting somebody else's failure as its own."""
    key = _one_task(client)
    _claim(client, key)
    _start_step(client, key)

    response = _finish_step(client, key, status="abandoned")

    assert response.status_code == 422, response.text


def test_finishing_a_step_that_was_never_started_is_a_not_found(
    client: TestClient,
) -> None:
    key = _one_task(client)
    _claim(client, key)

    response = _finish_step(client, key)

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "AGENT_TASK_STEP_NOT_FOUND"


def test_a_completed_step_is_never_reopened(client: TestClient) -> None:
    key = _one_task(client)
    _claim(client, key)
    _start_step(client, key)
    _finish_step(client, key, output_text="done")

    reopened = _start_step(client, key)

    assert reopened.status_code == 409, reopened.text
    assert reopened.json()["error_code"] == "TASK_STATUS_CONFLICT"
    assert (
        client.get(f"{TASKS_URL}/{key}").json()["task"]["steps"][0]["output_text"] == "done"
    ), "the output survives the attempt"


def test_a_step_is_finished_once(client: TestClient) -> None:
    key = _one_task(client)
    _claim(client, key)
    _start_step(client, key)
    _finish_step(client, key)

    again = _finish_step(client, key)

    assert again.status_code == 409, again.text
    assert client.get(f"{TASKS_URL}/{key}").json()["task"]["turns_used"] == 1, (
        "a repeated finish must not charge a second turn"
    )


# ---------------------------------------------------------------------------
# Task transitions
# ---------------------------------------------------------------------------


def test_finishing_a_task_terminally_releases_the_ref_and_the_claim(
    client: TestClient,
) -> None:
    ref = _ref()
    key = _one_task(client, ref)
    _claim(client, key)

    finished = client.post(
        f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst-a", "status": "completed"}
    )

    assert finished.status_code == 200, finished.text
    task = finished.json()["task"]
    assert task["status"] == "completed"
    assert task["claimed_by"] is None
    assert task["heartbeat_at"] is None
    assert _preview(client, ref, requeue_completed=True)["eligible"], (
        "a terminal task must not hold the ref for ever"
    )


def test_a_non_terminal_ending_keeps_the_slot_and_the_holder(client: TestClient) -> None:
    """``paused_quota`` and ``running_unknown`` are endings for the dispatcher
    and not for the ledger. Both keep the row somebody's responsibility."""
    for status in ("paused_quota", "running_unknown"):
        ref = _ref(status)
        key = _one_task(client, ref)
        _claim(client, key)
        client.post(f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst-a", "status": status})

        task = client.get(f"{TASKS_URL}/{key}").json()["task"]
        assert task["status"] == status
        assert task["claimed_by"] == "inst-a"
        assert _preview(client, ref)["skipped"]["already_queued"] == 1, (
            f"a {status} task still holds its source ref"
        )


@pytest.mark.parametrize("status", ["queued", "running", "cancelled", "awaiting_approval"])
def test_a_dispatcher_cannot_finish_a_task_into_a_status_that_is_not_its_own(
    client: TestClient, status: str
) -> None:
    key = _one_task(client)
    _claim(client, key)

    response = client.post(
        f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst-a", "status": status}
    )

    assert response.status_code == 422, response.text


def test_only_a_queued_task_is_cancelled(client: TestClient) -> None:
    """Cancelling a running task would say work had stopped while the turn
    carries on spending. Stopping a turn is a halt: a different mechanism."""
    key = _one_task(client)

    cancelled = client.post(f"{TASKS_URL}/{key}/cancel", json={"reason": "not now"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["task"]["status"] == "cancelled"

    running_key = _one_task(client)
    _claim(client, running_key)
    refused = client.post(f"{TASKS_URL}/{running_key}/cancel", json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "TASK_STATUS_CONFLICT"


def test_a_cancelled_task_cannot_be_claimed(client: TestClient) -> None:
    key = _one_task(client)
    client.post(f"{TASKS_URL}/{key}/cancel", json={})

    response = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst-a"})

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_ALREADY_CLAIMED"


def test_resolve_only_moves_a_timed_out_task(client: TestClient) -> None:
    key = _one_task(client)
    _claim(client, key)

    refused = client.post(f"{TASKS_URL}/{key}/resolve", json={"requeue": True})

    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "TASK_STATUS_CONFLICT"


def test_resolving_without_requeue_records_a_failure(client: TestClient) -> None:
    key = _one_task(client)
    _claim(client, key)
    client.post(
        f"{TASKS_URL}/{key}/finish",
        json={"instance_id": "inst-a", "status": "running_unknown"},
    )

    resolved = client.post(
        f"{TASKS_URL}/{key}/resolve", json={"requeue": False, "reason": "the turn did run"}
    )

    assert resolved.status_code == 200, resolved.text
    task = resolved.json()["task"]
    assert task["status"] == "failed"
    assert task["claimed_by"] is None
    assert task["failure_detail"] == "the turn did run"


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


class _HeaderNamespaceAuthorizer:
    """Maps ``X-Test-Namespace`` onto the principal's namespace."""

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del operation, context
        return Principal(
            namespace_key=request.headers.get("X-Test-Namespace", "default"),
            is_admin=True,
        )


@pytest.fixture()
def namespaced(app: FastAPI) -> tuple[TestClient, TestClient]:
    set_authorizer(_HeaderNamespaceAuthorizer())
    return (
        TestClient(app, raise_server_exceptions=True, headers={"X-Test-Namespace": "ns-one"}),
        TestClient(app, raise_server_exceptions=True, headers={"X-Test-Namespace": "ns-two"}),
    )


def test_one_namespaces_queue_is_invisible_to_another(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    one, two = namespaced
    _one_task(one, _ref("mine"))

    assert two.get(TASKS_URL).json()["pagination"]["total"] == 0
    assert one.get(TASKS_URL).json()["pagination"]["total"] == 1


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("get", "", None),
        ("post", "/claim", {"instance_id": "inst-b"}),
        ("post", "/heartbeat", {"instance_id": "inst-b"}),
        ("post", "/steps", {"instance_id": "inst-b", "step_index": 0, "agent_name": STEP_AGENT}),
        ("post", "/steps/0/finish", {"instance_id": "inst-b", "status": "completed"}),
        ("post", "/finish", {"instance_id": "inst-b", "status": "failed"}),
        ("post", "/cancel", {}),
        ("post", "/resolve", {"requeue": True}),
    ],
)
def test_a_task_key_from_another_namespace_is_a_not_found_everywhere(
    namespaced: tuple[TestClient, TestClient],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """A guessed key must not be a route into another tenant's work.

    Not merely invisible on the list: every route that takes a key answers as
    if the row did not exist, because a 409 would confirm it does.
    """
    one, two = namespaced
    key = _one_task(one)

    url = f"{TASKS_URL}/{key}{path}"
    response = two.get(url) if method == "get" else two.post(url, json=body)

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "AGENT_TASK_NOT_FOUND"


def test_two_namespaces_import_the_same_ref_independently(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    """The dedup index is namespace-scoped, so one tenant cannot block another."""
    one, two = namespaced
    ref = _ref("shared")

    assert _commit(one, ref)["created"] == 1
    assert _commit(two, ref)["created"] == 1

    assert one.get(TASKS_URL).json()["pagination"]["total"] == 1
    assert two.get(TASKS_URL).json()["pagination"]["total"] == 1


def test_a_claim_in_one_namespace_does_not_touch_the_others_row(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    one, two = namespaced
    ref = _ref("shared")
    one_key = _one_task(one, ref)
    two_key = _one_task(two, ref)

    _claim(one, one_key, "inst-one")

    assert two.get(f"{TASKS_URL}/{two_key}").json()["task"]["status"] == "queued"
    assert two.get(f"{TASKS_URL}/{two_key}").json()["task"]["claimed_by"] is None


# ---------------------------------------------------------------------------
# Who may reach these routes
#
# The ledger sits at AUTHENTICATED and that is a decision rather than a
# default: a play button only an admin can press is a play button an admin
# presses on somebody else's behalf. Read has a second, independent reason -
# it is the oversight path, and a session belonging to a task has no human
# owner, so requiring admin to look at one would mean overseeing the fleet
# needed a key that also carries ``controls.create``.
#
# The pause is the opposite tier, deliberately: under the local provider
# "authenticated" is every valid key in the deployment, and the flag that
# stops the fleet is the flag that holds it stopped.
# ---------------------------------------------------------------------------


def test_the_ledger_operations_sit_where_the_plan_puts_them() -> None:
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_TASKS_READ] is AccessLevel.AUTHENTICATED
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_TASKS_WRITE] is AccessLevel.AUTHENTICATED
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_TASKS_CLAIM] is AccessLevel.AUTHENTICATED
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_TASKS_APPROVE] is AccessLevel.AUTHENTICATED
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_DISPATCH_PAUSE] is AccessLevel.ADMIN


def test_a_non_admin_key_can_run_the_whole_ledger(non_admin_client: TestClient) -> None:
    """Import, claim, write a step, finish. No admin key anywhere in it."""
    key = _one_task(non_admin_client)
    claimed = _claim(non_admin_client, key)
    assert claimed["task"]["status"] == "running"

    started = _start_step(non_admin_client, key)
    assert started.status_code == 200, started.text
    finished = _finish_step(non_admin_client, key, output_text="a report")
    assert finished.status_code == 200, finished.text
    ended = non_admin_client.post(
        f"{TASKS_URL}/{key}/finish", json={"instance_id": "inst-a", "status": "completed"}
    )
    assert ended.status_code == 200, ended.text


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("get", "", None),
        ("post", "/import", {"scope": _scope("anon"), "mode": "preview"}),
    ],
)
def test_an_unauthenticated_caller_reaches_none_of_it(
    unauthenticated_client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    url = f"{TASKS_URL}{path}"
    response = (
        unauthenticated_client.get(url)
        if method == "get"
        else unauthenticated_client.post(url, json=body)
    )

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# The architectural line, checked rather than asserted in prose
#
# "The dispatch loop does not go in the FastAPI server." The plan names the
# tripwire itself: if a future reader finds ``asyncio.create_task`` or a
# scheduler import traceable to this work, the plan has been violated. This is
# that grep, run by CI instead of by whoever remembers to.
#
# Scoped to the three modules this phase added rather than to ``server/src``,
# because a request-scoped fan-out elsewhere - two Linear reads awaited
# together inside one handler - is not a loop and must not be flagged as one.
# ---------------------------------------------------------------------------


LEDGER_MODULES = (
    "services/agent_tasks.py",
    "services/task_claims.py",
    "endpoints/agent_tasks.py",
)

FORBIDDEN_IN_THE_LEDGER = (
    "asyncio.create_task",
    "apscheduler",
    "BackgroundTasks",
    "while True",
    "add_event_handler",
    "repeat_every",
)


def _without_docstrings(tree: ast.AST) -> ast.AST:
    """Drop every docstring, so prose quoting the rule is not read as breaking it."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


@pytest.mark.parametrize("module", LEDGER_MODULES)
def test_the_ledger_holds_no_loop_no_worker_and_no_timer(module: str) -> None:
    """Every route here is a request about rows.

    The server answers questions about ``agent_tasks``; it never polls the
    table, never starts a turn on its own initiative, never retries anything
    and has no background thread. A loop inside a request-scoped process whose
    pool is five plus ten overflow would hold a connection per running step for
    up to a turn timeout each, and a queue polled by N replicas is the
    double-claim bug by construction.
    """
    source = (Path(agent_control_server.__file__).parent / module).read_text()
    # Docstrings and comments quote the rule by name, so they are stripped
    # before the scan. What is left is the code, which has to obey it.
    body = ast.unparse(_without_docstrings(ast.parse(source)))

    for forbidden in FORBIDDEN_IN_THE_LEDGER:
        assert forbidden not in body, (
            f"{module} contains {forbidden!r}; the dispatch loop lives outside this server"
        )
