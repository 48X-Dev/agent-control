"""The resolved plan for one task, and the chain that ran, through the API.

Two reads and one refusal, and each of them is load-bearing.

**The plan is where agent selection happens, and it happens here.** Two
server-side sources in order - the workflow step, then the team's
``default_agent_name`` - and no third. Nothing the task can express reaches it:
not the title, not the body, not the source, not a label. Anyone who can file an
issue in a tracker can label it, so a label that chose the agent would hand an
attacker the choice of executor, and agents differ in system prompt, in bound
controls and in tools. A step neither source answers comes back *named* as
unresolved rather than filled in, and the dispatcher blocks the task.

**The import refuses a workflow that cannot name an agent for every step, before
a single row exists.** Four blocked tasks and four identical comments on
somebody's issues is the failure that avoids.

**The chain is built from ``agent_task_steps``.** Never from a trace: the
rollup at ``GET /observability/traces/{id}`` builds hops exclusively from
control-execution events, so an agent with no bound control that fired
contributes zero hops and vanishes from it - a three-agent chain where two have
no controls renders there as a one-agent trace with nothing saying otherwise.
Never from a caller-supplied id either; ``chain_trace_id`` is minted server-side
at claim time, because the audited party does not author its own audit record.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

TASKS_URL = "/api/v1/agent-tasks"
WORKFLOWS_URL = "/api/v1/agent-workflows"
TEAMS_URL = "/api/v1/teams"
AGENTS_URL = "/api/v1/agents"

SESSION_KEY = "5" * 32


def _ref() -> str:
    return f"ref-{uuid.uuid4().hex[:12]}"


def _key() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


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
    response = client.put(TEAMS_URL, json={"display_name": f"Team {uuid.uuid4().hex[:8]}"})
    assert response.status_code == 200, response.text
    slug = str(response.json()["slug"])
    for member in members or []:
        added = client.post(f"{TEAMS_URL}/{slug}/members/{member}")
        assert added.status_code == 200, added.text
    return slug


def _set_default_agent(client: TestClient, slug: str, agent_name: str | None) -> Any:
    return client.patch(f"{TEAMS_URL}/{slug}", json={"default_agent_name": agent_name})


def _workflow(client: TestClient, steps: list[dict[str, Any]], **extra: Any) -> str:
    key = _key()
    body: dict[str, Any] = {"display_name": "A chain", "steps": steps}
    body.update(extra)
    response = client.put(f"{WORKFLOWS_URL}/{key}", json=body)
    assert response.status_code == 200, response.text
    return key


def _import(client: TestClient, ref: str | None = None, **extra: Any) -> Any:
    """Preview then commit one item. Returns the commit response."""
    ref = ref or _ref()
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": ref, "title": f"title for {ref}", "body": "the body"}],
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


def _chain(client: TestClient, task_key: str) -> dict[str, Any]:
    response = client.get(f"{TASKS_URL}/{task_key}/chain")
    assert response.status_code == 200, response.text
    return dict(response.json()["chain"])


def _claim(client: TestClient, task_key: str, instance: str = "inst-a") -> dict[str, Any]:
    response = client.post(f"{TASKS_URL}/{task_key}/claim", json={"instance_id": instance})
    assert response.status_code == 200, response.text
    return dict(response.json())


def _run_step(
    client: TestClient,
    task_key: str,
    *,
    index: int,
    agent_name: str,
    output: str | None = "what this agent found",
    status: str = "completed",
    instance: str = "inst-a",
    **finish: Any,
) -> None:
    started = client.post(
        f"{TASKS_URL}/{task_key}/steps",
        json={
            "instance_id": instance,
            "step_index": index,
            "agent_name": agent_name,
            "brief": f"step {index}",
            "session_key": SESSION_KEY,
        },
    )
    assert started.status_code == 200, started.text
    finished = client.post(
        f"{TASKS_URL}/{task_key}/steps/{index}/finish",
        json={
            "instance_id": instance,
            "status": status,
            "output_text": output,
            "turn_trace_id": f"trace-{index}",
            **finish,
        },
    )
    assert finished.status_code == 200, finished.text


# ---------------------------------------------------------------------------
# Resolving the plan
# ---------------------------------------------------------------------------


def test_a_task_with_no_workflow_gets_one_implicit_unresolved_step(
    client: TestClient,
) -> None:
    """Most of the value is one agent doing one thing, and a design that
    demands a workflow before anything runs does not get used. The step pins no
    agent, so the operator's ``--agent`` fills it."""
    plan = _plan(client, _one_task(client))

    assert plan["implicit"] is True
    assert plan["workflow_key"] == "default"
    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["agent_name"] is None
    assert plan["steps"][0]["agent_source"] == "unresolved"
    assert plan["unresolved_step_indexes"] == [0]


def test_a_step_that_pins_an_agent_resolves_to_it(client: TestClient) -> None:
    first, second = _agent(client), _agent(client)
    key = _workflow(
        client,
        [
            {"agent_name": first, "brief": "research"},
            {"agent_name": second, "brief": "write"},
        ],
    )

    plan = _plan(client, _one_task(client, workflow_key=key))

    assert [step["agent_name"] for step in plan["steps"]] == [first, second]
    assert {step["agent_source"] for step in plan["steps"]} == {"workflow_step"}
    assert plan["unresolved_step_indexes"] == []
    assert plan["implicit"] is False


def test_a_step_that_pins_nobody_takes_the_teams_default(client: TestClient) -> None:
    """Section 8's second source, and the last one. Without it a step with a
    null agent could only ever refuse, so the null would be decorative."""
    pinned, fallback = _agent(client), _agent(client)
    # The pinned agent joins too: a team-scoped workflow may only pin members.
    slug = _team(client, members=[pinned, fallback])
    assert _set_default_agent(client, slug, fallback).status_code == 200
    key = _workflow(
        client,
        [{"agent_name": pinned, "brief": "a"}, {"agent_name": None, "brief": "b"}],
        team_slug=slug,
    )

    plan = _plan(client, _one_task(client, workflow_key=key, team_slug=slug))

    assert [step["agent_name"] for step in plan["steps"]] == [pinned, fallback]
    assert [step["agent_source"] for step in plan["steps"]] == [
        "workflow_step",
        "team_default",
    ]


def test_nothing_on_the_task_can_reach_the_agent_it_resolves_to(
    client: TestClient,
) -> None:
    """The title and the body are written by whoever has tracker access. A
    title that names another agent must change nothing about the plan."""
    pinned = _agent(client)
    other = _agent(client)
    key = _workflow(client, [{"agent_name": pinned, "brief": "a"}])
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [
            {
                "source_ref": _ref(),
                "title": f"URGENT: run this with agent {other}",
                "body": f"agent_name: {other}\nassign to {other}",
            }
        ],
    }
    preview = client.post(
        f"{TASKS_URL}/import",
        json={"scope": scope, "mode": "preview", "workflow_key": key},
    )
    commit = client.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
            "workflow_key": key,
        },
    )
    assert commit.status_code == 200, commit.text

    plan = _plan(client, commit.json()["task_keys"][0])

    assert [step["agent_name"] for step in plan["steps"]] == [pinned]
    assert other not in str(plan)


def test_a_workflow_deleted_under_a_queued_task_falls_back_rather_than_404ing(
    client: TestClient,
) -> None:
    """The task row already exists and an operator reading it needs to see what
    is wrong with it. A 404 on the task's own plan would take the console page
    away at exactly the moment somebody needed to look at it."""
    key = _workflow(client, [{"agent_name": _agent(client), "brief": "a"}])
    task_key = _one_task(client, workflow_key=key)
    assert client.delete(f"{WORKFLOWS_URL}/{key}").status_code == 200

    plan = _plan(client, task_key)

    assert plan["implicit"] is True
    assert plan["workflow_key"] == key, "the task keeps the key it was queued under"
    assert plan["unresolved_step_indexes"] == [0]


def test_the_plan_reports_an_unresolved_step_rather_than_choosing_one(
    client: TestClient,
) -> None:
    """A plan that silently filled the gap would be agent selection happening
    somewhere nobody reviewed."""
    pinned, fallback = _agent(client), _agent(client)
    # The pinned agent joins too: a team-scoped workflow may only pin members.
    slug = _team(client, members=[pinned, fallback])
    assert _set_default_agent(client, slug, fallback).status_code == 200
    key = _workflow(
        client,
        [{"agent_name": pinned, "brief": "a"}, {"agent_name": None, "brief": "b"}],
        team_slug=slug,
    )
    # Queued while the second step still resolved. Clearing the team's default
    # afterwards is what a mid-flight configuration change looks like, and the
    # plan has to report the hole rather than fill it.
    task_key = _one_task(client, workflow_key=key, team_slug=slug)
    assert _set_default_agent(client, slug, None).status_code == 200

    plan = _plan(client, task_key)

    assert plan["steps"][1]["agent_name"] is None
    assert plan["steps"][1]["agent_source"] == "unresolved"
    assert plan["unresolved_step_indexes"] == [1]


def test_a_plan_for_an_unknown_task_is_a_404(client: TestClient) -> None:
    response = client.get(f"{TASKS_URL}/{'0' * 32}/plan")

    assert response.status_code == 404, response.text


def test_a_task_key_from_another_namespace_is_a_404_on_both_reads(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    """A key is not a capability. Holding one from another tenant must not read
    which agents run there, nor what they produced."""
    one, two = namespaced
    task_key = _one_task(one)

    assert two.get(f"{TASKS_URL}/{task_key}/plan").status_code == 404
    assert two.get(f"{TASKS_URL}/{task_key}/chain").status_code == 404
    assert one.get(f"{TASKS_URL}/{task_key}/plan").status_code == 200


def test_a_workflow_key_does_not_resolve_across_namespaces(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    """One tenant's workflow must not supply another tenant's agents.

    The task keeps the key it was queued under, so the plan falls back to the
    implicit one-step workflow rather than reading somebody else's steps.
    """
    one, two = namespaced
    key = _key()
    put = two.put(
        f"{WORKFLOWS_URL}/{key}",
        json={
            "display_name": "Theirs",
            "steps": [{"agent_name": "their_secret_agent", "brief": "x"}],
        },
    )
    assert put.status_code == 200, put.text

    commit = _import(one, workflow_key=key)

    # The commit refuses: this namespace has no such workflow. The preview
    # ahead of it does not check, which is a legible ordering - the resolvable
    # check runs in the transaction that would insert, so nothing is created
    # either way.
    assert commit.status_code == 404, commit.text
    assert commit.json()["error_code"] == "AGENT_WORKFLOW_NOT_FOUND"
    assert one.get(TASKS_URL).json()["pagination"]["total"] == 0
    assert "their_secret_agent" not in commit.text


def test_reading_a_plan_needs_no_admin_key(
    client: TestClient, non_admin_client: TestClient
) -> None:
    """The dispatcher reads a plan for every task it claims. At the admin tier
    the fleet would have to run on an admin credential."""
    task_key = _one_task(client)

    assert non_admin_client.get(f"{TASKS_URL}/{task_key}/plan").status_code == 200


# ---------------------------------------------------------------------------
# The import refusal
# ---------------------------------------------------------------------------


def test_an_import_naming_an_unresolvable_workflow_creates_nothing(
    client: TestClient,
) -> None:
    """Refused at the confirm, before any row exists. Four blocked tasks and
    four identical comments on somebody's issues is the failure this avoids."""
    slug = _team(client)
    key = _workflow(client, [{"agent_name": None, "brief": "nobody"}], team_slug=slug)

    response = _import(client, workflow_key=key, team_slug=slug)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "NO_AGENT_SELECTED"
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 0


def test_the_refusal_names_the_steps_that_could_not_be_resolved(
    client: TestClient,
) -> None:
    """The operator has to be told *which* steps to fix.

    In prose, and only in prose: ``ErrorDetails`` carries ``name``, ``kind``,
    ``causes`` and ``retry_after_seconds``, so the ``extra_details`` dict the
    service passes is dropped on the way out. That is asserted here as it
    behaves rather than as it reads, because a test written against the
    unreachable field would pass the day somebody made it reachable and say
    nothing until then.
    """
    member = _agent(client)
    # A member, because a team-scoped workflow may only pin members and this
    # test is about the *other* refusal: the steps that resolve to nobody.
    slug = _team(client, members=[member])
    key = _workflow(
        client,
        [
            {"agent_name": member, "brief": "a"},
            {"agent_name": None, "brief": "b"},
            {"agent_name": None, "brief": "c"},
        ],
        team_slug=slug,
    )

    response = _import(client, workflow_key=key, team_slug=slug)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "steps 1, 2" in detail
    assert "no default_agent_name" in detail
    assert "Pin an agent on the step" in response.json()["hint"]
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 0


def test_an_import_naming_a_workflow_that_does_not_exist_creates_nothing(
    client: TestClient,
) -> None:
    response = _import(client, workflow_key=_key())

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "AGENT_WORKFLOW_NOT_FOUND"
    assert client.get(TASKS_URL).json()["pagination"]["total"] == 0


def test_an_import_with_no_workflow_is_never_refused_for_a_missing_agent(
    client: TestClient,
) -> None:
    """The implicit one-step workflow pins no agent by construction, and the
    operator names one on the command line. That is the path slice 1 shipped."""
    response = _import(client)

    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1


def test_a_resolvable_workflow_imports_and_keeps_its_key(client: TestClient) -> None:
    name = _agent(client)
    key = _workflow(client, [{"agent_name": name, "brief": "a"}])

    response = _import(client, workflow_key=key)

    assert response.status_code == 200, response.text
    task_key = response.json()["task_keys"][0]
    assert _plan(client, task_key)["workflow_key"] == key


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def test_the_chain_renders_the_hops_in_order_from_the_step_rows(
    client: TestClient,
) -> None:
    first, second = _agent(client), _agent(client)
    key = _workflow(
        client,
        [{"agent_name": first, "brief": "a"}, {"agent_name": second, "brief": "b"}],
    )
    task_key = _one_task(client, workflow_key=key)
    _claim(client, task_key)
    _run_step(client, task_key, index=0, agent_name=first, output="the researcher's report")
    _run_step(client, task_key, index=1, agent_name=second, output="the writer's draft")

    chain = _chain(client, task_key)

    assert [hop["step_index"] for hop in chain["hops"]] == [0, 1]
    assert [hop["agent_name"] for hop in chain["hops"]] == [first, second]
    assert [hop["output_text"] for hop in chain["hops"]] == [
        "the researcher's report",
        "the writer's draft",
    ]
    assert all(hop["ran"] for hop in chain["hops"])
    assert chain["hops_ran"] == 2
    assert chain["hops_planned"] == 2


def test_a_hop_that_never_started_is_visible_as_one(client: TestClient) -> None:
    """The difference between "the writer found nothing" and "the writer never
    ran". Neither the plan nor the rows can say that alone."""
    first, second = _agent(client), _agent(client)
    key = _workflow(
        client,
        [{"agent_name": first, "brief": "a"}, {"agent_name": second, "brief": "b"}],
    )
    task_key = _one_task(client, workflow_key=key)
    _claim(client, task_key)
    _run_step(
        client,
        task_key,
        index=0,
        agent_name=first,
        output=None,
        status="failed",
        failure_code="EMPTY_STEP_OUTPUT",
    )

    chain = _chain(client, task_key)

    assert [hop["ran"] for hop in chain["hops"]] == [True, False]
    assert chain["hops"][0]["failure_code"] == "EMPTY_STEP_OUTPUT"
    assert chain["hops"][1]["agent_name"] == second, "the plan supplies who did not run"
    assert chain["hops"][1]["status"] is None
    assert chain["hops_ran"] == 1
    assert chain["hops_planned"] == 2


def test_each_hop_carries_its_own_trace_and_the_chain_carries_the_server_minted_one(
    client: TestClient,
) -> None:
    """Per-hop traces are links to a forensic view. The chain's identity is the
    step rows, and its ``chain_trace_id`` is minted at claim time by the server
    - a caller has never been able to supply either."""
    name = _agent(client)
    key = _workflow(client, [{"agent_name": name, "brief": "a"}])
    task_key = _one_task(client, workflow_key=key)
    _claim(client, task_key)
    _run_step(client, task_key, index=0, agent_name=name)

    chain = _chain(client, task_key)

    assert chain["hops"][0]["turn_trace_id"] == "trace-0"
    assert chain["chain_trace_id"]
    assert chain["chain_trace_id"] != "trace-0"


def test_a_chain_with_no_steps_yet_still_renders_its_planned_hops(
    client: TestClient,
) -> None:
    """It cannot 404, which is half the reason the view is these rows and not
    a trace rollup."""
    first, second = _agent(client), _agent(client)
    key = _workflow(
        client,
        [{"agent_name": first, "brief": "a"}, {"agent_name": second, "brief": "b"}],
    )

    chain = _chain(client, _one_task(client, workflow_key=key))

    assert [hop["ran"] for hop in chain["hops"]] == [False, False]
    assert chain["hops_ran"] == 0
    assert chain["status"] == "queued"


def test_a_workflow_rewritten_mid_task_does_not_rewrite_what_already_ran(
    client: TestClient,
) -> None:
    """The step rows are the record of what the agents actually did. Trimming
    them to fit the current configuration would show an operator a shorter
    chain than the one they paid for."""
    first, second = _agent(client), _agent(client)
    key = _workflow(
        client,
        [{"agent_name": first, "brief": "a"}, {"agent_name": second, "brief": "b"}],
    )
    task_key = _one_task(client, workflow_key=key)
    _claim(client, task_key)
    _run_step(client, task_key, index=0, agent_name=first)
    _run_step(client, task_key, index=1, agent_name=second)

    shortened = client.put(
        f"{WORKFLOWS_URL}/{key}",
        json={"display_name": "A chain", "steps": [{"agent_name": first, "brief": "a"}]},
    )
    assert shortened.status_code == 200, shortened.text
    chain = _chain(client, task_key)

    assert [hop["agent_name"] for hop in chain["hops"]] == [first, second]
    assert chain["hops_ran"] == 2
    assert chain["hops_planned"] == 1, "the count reports the workflow as it is now"


def test_the_row_wins_over_the_plan_for_a_hop_that_ran(client: TestClient) -> None:
    """A workflow edited mid-task must not rewrite the history of which agent
    actually ran a hop."""
    ran_it, replacement = _agent(client), _agent(client)
    key = _workflow(client, [{"agent_name": ran_it, "brief": "a"}])
    task_key = _one_task(client, workflow_key=key)
    _claim(client, task_key)
    _run_step(client, task_key, index=0, agent_name=ran_it)

    client.put(
        f"{WORKFLOWS_URL}/{key}",
        json={
            "display_name": "A chain",
            "steps": [{"agent_name": replacement, "brief": "a"}],
        },
    )
    chain = _chain(client, task_key)

    assert chain["hops"][0]["agent_name"] == ran_it


def test_the_chain_carries_the_untrusted_title_and_the_source_it_came_from(
    client: TestClient,
) -> None:
    ref = _ref()
    task_key = _one_task(client, ref=ref)

    chain = _chain(client, task_key)

    assert chain["source_ref"] == ref
    assert chain["source_kind"] == "file"
    assert chain["title"] == f"title for {ref}"
    assert chain["dry_run"] is False or chain["dry_run"] is True


def test_reading_a_chain_needs_no_admin_key(
    client: TestClient, non_admin_client: TestClient
) -> None:
    """Oversight without admin is a requirement of this design. An operator
    watching a task must not need a credential that also grants
    ``controls.create``."""
    task_key = _one_task(client)

    assert non_admin_client.get(f"{TASKS_URL}/{task_key}/chain").status_code == 200


def test_a_chain_for_an_unknown_task_is_a_404(client: TestClient) -> None:
    assert client.get(f"{TASKS_URL}/{'0' * 32}/chain").status_code == 404


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
