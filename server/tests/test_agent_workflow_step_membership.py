"""Steps that pin an agent on a team-scoped workflow: members only.

``PUT /agent-workflows/{key}`` with a ``team_slug`` is a claim that the chain
runs on that team's members. A step pinning an outsider used to be accepted,
and it broke the claim twice over: the agent's system prompt is the wrong
persona for the work, and controls are bound per agent, so the wrong control
surface ran the task. The write now refuses it with the same
``AGENT_NOT_IN_TEAM`` that ``default_agent_name`` raises on ``/teams`` - one
invariant, both fields that name an agent.

Held on the way out as well as on the way in, because membership changes after
workflows are written. The plan re-checks it and reports a since-removed member
as unresolved - reported, never substituted with the team default, and never an
error on the read - and an import against such a workflow is refused before a
single task row exists. The check answers to the workflow's *own* team: the
task's team picks the default for steps that pin nobody, and a workflow with no
``team_slug`` claims no boundary at all.

The write-and-resolve cases run under both providers. The header client is an
admin with a caller identity; ``NoAuthProvider`` authorizes every operation and
leaves ``caller_id`` None. The refusal is about configuration, not about who
asked, so it has to hold on both branches.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_server.auth_framework import set_authorizer
from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider
from fastapi.testclient import TestClient

WORKFLOWS_URL = "/api/v1/agent-workflows"
TASKS_URL = "/api/v1/agent-tasks"
TEAMS_URL = "/api/v1/teams"
AGENTS_URL = "/api/v1/agents"


def _key() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _ref() -> str:
    return f"ref-{uuid.uuid4().hex[:12]}"


def _agent(client: TestClient) -> str:
    name = f"agent_{uuid.uuid4().hex[:10]}"
    response = client.post(
        f"{AGENTS_URL}/initAgent",
        json={
            "agent": {
                "agent_name": name,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert response.status_code == 200, response.text
    return name


def _team(client: TestClient, *, members: list[str] | None = None) -> str:
    response = client.put(
        TEAMS_URL, json={"display_name": f"Team {uuid.uuid4().hex[:8]}"}
    )
    assert response.status_code == 200, response.text
    slug = str(response.json()["slug"])
    for member in members or []:
        added = client.post(f"{TEAMS_URL}/{slug}/members/{member}")
        assert added.status_code == 200, added.text
    return slug


def _remove_member(client: TestClient, slug: str, agent_name: str) -> None:
    response = client.delete(f"{TEAMS_URL}/{slug}/members/{agent_name}")
    assert response.status_code == 200, response.text


def _set_default(client: TestClient, slug: str, agent_name: str | None) -> None:
    response = client.patch(
        f"{TEAMS_URL}/{slug}", json={"default_agent_name": agent_name}
    )
    assert response.status_code == 200, response.text


def _put(
    client: TestClient, workflow_key: str, steps: list[dict[str, Any]], **extra: Any
) -> Any:
    body: dict[str, Any] = {"display_name": "A chain", "steps": steps}
    body.update(extra)
    return client.put(f"{WORKFLOWS_URL}/{workflow_key}", json=body)


def _stored_steps(client: TestClient, workflow_key: str) -> list[dict[str, Any]]:
    response = client.get(f"{WORKFLOWS_URL}/{workflow_key}")
    assert response.status_code == 200, response.text
    return list(response.json()["workflow"]["steps"])


def _import(client: TestClient, **extra: Any) -> Any:
    """Preview then commit one file-source item. Returns the failing response,
    or the commit response when both succeed."""
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": _ref(), "title": "a title", "body": "the body"}],
    }
    preview = client.post(
        f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview", **extra}
    )
    if preview.status_code != 200:
        return preview
    return client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
            **extra,
        },
    )


def _one_task(client: TestClient, **extra: Any) -> str:
    response = _import(client, **extra)
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1, response.text
    return str(response.json()["task_keys"][0])


def _plan(client: TestClient, task_key: str) -> dict[str, Any]:
    response = client.get(f"{TASKS_URL}/{task_key}/plan")
    assert response.status_code == 200, response.text
    return dict(response.json()["plan"])


@pytest.fixture(params=["header", "no_auth"])
def either_provider(
    request: pytest.FixtureRequest, client: TestClient, app: Any
) -> TestClient:
    """The same case under the two providers this invariant has to hold under.

    The admin header client and ``NoAuthProvider`` reach the write path by
    different branches - one is an admin, the other has no caller identity at
    all - and the refusal is about the configuration, not the caller.
    """
    if request.param == "header":
        return client
    set_authorizer(NoAuthProvider())
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def test_a_step_pinning_a_member_is_accepted(either_provider: TestClient) -> None:
    client = either_provider
    member = _agent(client)
    slug = _team(client, members=[member])
    key = _key()

    response = _put(
        client,
        key,
        [{"agent_name": member, "brief": "a"}, {"agent_name": None, "brief": "b"}],
        team_slug=slug,
    )

    assert response.status_code == 200, response.text
    assert [step["agent_name"] for step in _stored_steps(client, key)] == [member, None]


def test_a_step_pinning_an_outsider_is_refused(either_provider: TestClient) -> None:
    """The outsider is a registered agent, so what refuses it is membership,
    not existence - the refusal ``default_agent_name`` already raises."""
    client = either_provider
    outsider = _agent(client)
    slug = _team(client, members=[_agent(client)])
    key = _key()

    response = _put(
        client, key, [{"agent_name": outsider, "brief": "a"}], team_slug=slug
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "AGENT_NOT_IN_TEAM"
    assert outsider in body["detail"]
    assert slug in body["detail"]
    assert client.get(f"{WORKFLOWS_URL}/{key}").status_code == 404


def test_a_workflow_with_no_team_may_pin_anybody(client: TestClient) -> None:
    """No ``team_slug`` claims no team boundary, so there is none to hold. The
    agents here sit on a team and on none at all, and both are accepted."""
    theirs = _agent(client)
    _team(client, members=[theirs])
    unaffiliated = _agent(client)

    response = _put(
        client,
        _key(),
        [
            {"agent_name": theirs, "brief": "a"},
            {"agent_name": unaffiliated, "brief": "b"},
        ],
    )

    assert response.status_code == 200, response.text


def test_a_refused_rewrite_leaves_the_stored_workflow_intact(
    client: TestClient,
) -> None:
    member, outsider = _agent(client), _agent(client)
    slug = _team(client, members=[member])
    key = _key()
    created = _put(client, key, [{"agent_name": member, "brief": "a"}], team_slug=slug)
    assert created.status_code == 200, created.text

    refused = _put(
        client, key, [{"agent_name": outsider, "brief": "a"}], team_slug=slug
    )

    assert refused.status_code == 409, refused.text
    assert [step["agent_name"] for step in _stored_steps(client, key)] == [member]


def test_the_refusal_names_the_outsider_and_the_step(client: TestClient) -> None:
    """The operator has to be told which step to fix, and a list mixing members
    with an outsider stores nothing at all."""
    member, outsider = _agent(client), _agent(client)
    slug = _team(client, members=[member])
    key = _key()

    response = _put(
        client,
        key,
        [{"agent_name": member, "brief": "a"}, {"agent_name": outsider, "brief": "b"}],
        team_slug=slug,
    )

    assert response.status_code == 409, response.text
    assert f"'{outsider}' (step 1)" in response.json()["detail"]
    assert client.get(f"{WORKFLOWS_URL}/{key}").status_code == 404


# ---------------------------------------------------------------------------
# Keeping it true: the plan
# ---------------------------------------------------------------------------


def test_membership_removed_after_the_write_surfaces_as_unresolved(
    either_provider: TestClient,
) -> None:
    """The stored workflow now names an agent the write would refuse. The plan
    reports the step as unresolved - a 200, not a 500, because the task row
    exists and its page is where somebody looks to see what is wrong."""
    client = either_provider
    member = _agent(client)
    slug = _team(client, members=[member])
    key = _key()
    created = _put(client, key, [{"agent_name": member, "brief": "a"}], team_slug=slug)
    assert created.status_code == 200, created.text
    task_key = _one_task(client, workflow_key=key, team_slug=slug)

    _remove_member(client, slug, member)
    plan = _plan(client, task_key)

    assert plan["steps"][0]["agent_name"] is None
    assert plan["steps"][0]["agent_source"] == "unresolved"
    assert plan["unresolved_step_indexes"] == [0]
    # The stored workflow still shows who was pinned. The plan reports the
    # configuration; it does not rewrite it.
    assert [step["agent_name"] for step in _stored_steps(client, key)] == [member]


def test_a_stale_pinned_step_is_not_filled_from_the_team_default(
    client: TestClient,
) -> None:
    """The step named its agent. Substituting the default would be agent
    selection nobody reviewed, on exactly the path built to refuse that."""
    pinned, fallback = _agent(client), _agent(client)
    slug = _team(client, members=[pinned, fallback])
    _set_default(client, slug, fallback)
    key = _key()
    created = _put(client, key, [{"agent_name": pinned, "brief": "a"}], team_slug=slug)
    assert created.status_code == 200, created.text
    task_key = _one_task(client, workflow_key=key, team_slug=slug)

    _remove_member(client, slug, pinned)
    plan = _plan(client, task_key)

    assert plan["steps"][0]["agent_name"] is None
    assert plan["steps"][0]["agent_source"] == "unresolved"
    assert plan["unresolved_step_indexes"] == [0]


def test_the_check_runs_against_the_workflows_team_not_the_tasks(
    client: TestClient,
) -> None:
    """A workflow shared between teams still resolves each team's own default,
    and its pinned steps answer to the team the workflow itself claims."""
    theirs, ours = _agent(client), _agent(client)
    their_slug = _team(client, members=[theirs])
    our_slug = _team(client, members=[ours])
    _set_default(client, our_slug, ours)
    key = _key()
    created = _put(
        client,
        key,
        [{"agent_name": theirs, "brief": "a"}, {"agent_name": None, "brief": "b"}],
        team_slug=their_slug,
    )
    assert created.status_code == 200, created.text

    plan = _plan(client, _one_task(client, workflow_key=key, team_slug=our_slug))

    assert [step["agent_name"] for step in plan["steps"]] == [theirs, ours]
    assert [step["agent_source"] for step in plan["steps"]] == [
        "workflow_step",
        "team_default",
    ]
    assert plan["unresolved_step_indexes"] == []


def test_a_deleted_team_blocks_the_workflows_pinned_steps(client: TestClient) -> None:
    """The workflow claimed a boundary that can no longer be verified, so its
    pinned steps block rather than run outside it."""
    member = _agent(client)
    slug = _team(client, members=[member])
    key = _key()
    created = _put(client, key, [{"agent_name": member, "brief": "a"}], team_slug=slug)
    assert created.status_code == 200, created.text
    task_key = _one_task(client, workflow_key=key, team_slug=slug)

    deleted = client.delete(f"{TEAMS_URL}/{slug}", params={"force": "true"})
    assert deleted.status_code == 200, deleted.text
    plan = _plan(client, task_key)

    assert plan["steps"][0]["agent_name"] is None
    assert plan["steps"][0]["agent_source"] == "unresolved"
    assert plan["unresolved_step_indexes"] == [0]


# ---------------------------------------------------------------------------
# Keeping it true: the import
# ---------------------------------------------------------------------------


def test_an_import_against_a_stale_workflow_is_refused(
    either_provider: TestClient,
) -> None:
    """Refused at the confirm, before any row exists, with the reason the write
    would have given. Otherwise every imported task blocks at claim time, which
    is the four-blocked-tasks failure with a different cause."""
    client = either_provider
    member = _agent(client)
    slug = _team(client, members=[member])
    key = _key()
    created = _put(client, key, [{"agent_name": member, "brief": "a"}], team_slug=slug)
    assert created.status_code == 200, created.text
    _remove_member(client, slug, member)

    response = _import(client, workflow_key=key, team_slug=slug)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "AGENT_NOT_IN_TEAM"
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 0
