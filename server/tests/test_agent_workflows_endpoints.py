"""Workflow configuration, asserted through the API.

A workflow names the agents an autonomous chain runs and writes the one part of
a dispatch turn's message that is *not* framed as untrusted data. Three
properties follow, and every test here is about one of them.

**Writing is ADMIN, reading is not.** The dispatcher reads a resolved plan for
every task it claims, so a read at the admin tier would mean the fleet ran on an
admin key. Writing sits beside authoring a control, because agents differ in
system prompt, in bound controls and in tools: choosing the agent is choosing
the blast radius.

**Replace, never patch.** A partial update that could move one entry in the
middle is a way to change who runs step 2 without whoever reviews the change
seeing steps 1 and 3.

**A silent step in the middle is refused at write time.** A step permitted to
report nothing, followed by a step that would be handed its report, is a chain
with a hole in it - and the run-time version of that refusal costs a claimed
task and a paid turn.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import agent_control_server
from agent_control_server.auth_framework import (
    Operation,
    Principal,
    set_authorizer,
)
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)

WORKFLOWS_URL = "/api/v1/agent-workflows"
TEAMS_URL = "/api/v1/teams"
AGENTS_URL = "/api/v1/agents"


def _key(prefix: str = "wf") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _agent(client: TestClient, name: str | None = None) -> str:
    name = name or f"agent_{uuid.uuid4().hex[:10]}"
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


def _steps(*agents: str | None, **overrides: Any) -> list[dict[str, Any]]:
    return [
        {"agent_name": agent, "brief": f"step {index}", **overrides}
        for index, agent in enumerate(agents)
    ]


def _put(
    client: TestClient, workflow_key: str, steps: list[dict[str, Any]], **extra: Any
) -> Any:
    body: dict[str, Any] = {"display_name": "A chain", "steps": steps}
    body.update(extra)
    return client.put(f"{WORKFLOWS_URL}/{workflow_key}", json=body)


# ---------------------------------------------------------------------------
# Storing the configuration
# ---------------------------------------------------------------------------


def test_a_workflow_round_trips_with_its_steps_in_order(client: TestClient) -> None:
    first, second = _agent(client), _agent(client)
    key = _key()

    created = _put(client, key, _steps(first, second))

    assert created.status_code == 200, created.text
    assert created.json()["created"] is True
    stored = client.get(f"{WORKFLOWS_URL}/{key}").json()["workflow"]
    assert [step["agent_name"] for step in stored["steps"]] == [first, second]
    assert stored["workflow_key"] == key


def test_writing_the_same_key_again_replaces_the_whole_list(client: TestClient) -> None:
    """Replace semantics. A three-step workflow rewritten as one step is one
    step, not one step plus two survivors."""
    first, second = _agent(client), _agent(client)
    key = _key()
    _put(client, key, _steps(first, second))

    rewritten = _put(client, key, _steps(second))

    assert rewritten.status_code == 200, rewritten.text
    assert rewritten.json()["created"] is False
    stored = client.get(f"{WORKFLOWS_URL}/{key}").json()["workflow"]
    assert [step["agent_name"] for step in stored["steps"]] == [second]


def test_a_step_carries_its_ceilings_and_its_defaults(client: TestClient) -> None:
    key = _key()
    name = _agent(client)

    _put(client, key, [{"agent_name": name, "brief": "do it", "max_turns": 3}])

    step = client.get(f"{WORKFLOWS_URL}/{key}").json()["workflow"]["steps"][0]
    assert step["max_turns"] == 3
    assert step["required_output"] == "text"
    assert step["idempotent"] is False


def test_a_step_may_name_no_agent_and_fall_back_to_the_team(client: TestClient) -> None:
    """Null is "the team's default", not "any agent". Which agent that is
    depends on the task's team, so this route does not answer it."""
    key = _key()

    response = _put(client, key, [{"agent_name": None, "brief": "whoever"}])

    assert response.status_code == 200, response.text
    assert response.json()["workflow"]["steps"][0]["agent_name"] is None


def test_there_is_no_field_a_step_could_use_to_address_another_step(
    client: TestClient,
) -> None:
    """Asserted as an absence, because it is the property the whole design
    rests on. Extras are forbidden, so an unknown key is refused rather than
    stored and ignored."""
    key = _key()

    response = _put(
        client, key, [{"agent_name": _agent(client), "brief": "x", "send_to": "other"}]
    )

    assert response.status_code == 422, response.text


def test_more_steps_than_the_cap_are_refused(client: TestClient) -> None:
    """The cap is a ceiling on chain length, so a workflow cannot loop."""
    names = [_agent(client) for _ in range(5)]

    response = _put(client, _key(), _steps(*names))

    assert response.status_code == 422, response.text


def test_a_workflow_with_no_steps_is_refused(client: TestClient) -> None:
    response = _put(client, _key(), [])

    assert response.status_code == 422, response.text


def test_a_turn_ceiling_above_the_maximum_is_refused(client: TestClient) -> None:
    response = _put(
        client, _key(), [{"agent_name": _agent(client), "brief": "x", "max_turns": 4}]
    )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# The silent-step rule
# ---------------------------------------------------------------------------


def test_a_silent_step_in_the_middle_is_refused_at_write_time(
    client: TestClient,
) -> None:
    """The next agent would receive an empty prior-report block, have nothing
    to work from, and answer anyway - which the envelope's untrusted framing
    cannot help with, because there is no text to distrust."""
    first, second = _agent(client), _agent(client)

    response = _put(
        client,
        _key(),
        [
            {"agent_name": first, "brief": "a", "required_output": "none"},
            {"agent_name": second, "brief": "b"},
        ],
    )

    assert response.status_code == 422, response.text
    assert "Only the last step of a workflow may be silent" in response.text


def test_a_silent_last_step_is_allowed(client: TestClient) -> None:
    """There is nobody downstream to mislead."""
    first, second = _agent(client), _agent(client)

    response = _put(
        client,
        _key(),
        [
            {"agent_name": first, "brief": "a"},
            {"agent_name": second, "brief": "b", "required_output": "none"},
        ],
    )

    assert response.status_code == 200, response.text


def test_a_refused_workflow_is_not_stored_at_all(client: TestClient) -> None:
    key = _key()

    _put(
        client,
        key,
        [
            {"agent_name": _agent(client), "brief": "a", "required_output": "none"},
            {"agent_name": _agent(client), "brief": "b"},
        ],
    )

    assert client.get(f"{WORKFLOWS_URL}/{key}").status_code == 404


def test_a_refused_rewrite_leaves_the_previous_version_intact(
    client: TestClient,
) -> None:
    """The workflow a running dispatcher is walking must not be half-replaced
    by a write that was refused."""
    first, second = _agent(client), _agent(client)
    key = _key()
    _put(client, key, _steps(first, second))

    _put(
        client,
        key,
        [
            {"agent_name": first, "brief": "a", "required_output": "none"},
            {"agent_name": second, "brief": "b"},
        ],
    )

    stored = client.get(f"{WORKFLOWS_URL}/{key}").json()["workflow"]
    assert [step["agent_name"] for step in stored["steps"]] == [first, second]


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def test_a_workflow_scoped_to_a_missing_team_is_refused(client: TestClient) -> None:
    """It would resolve no default agent and refuse every step that relied on
    one, at claim time, on somebody else's shift."""
    response = _put(
        client, _key(), _steps(_agent(client)), team_slug="team-that-is-not-there"
    )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "TEAM_NOT_FOUND"


def test_a_workflow_can_be_scoped_to_a_team_that_exists(client: TestClient) -> None:
    name = _agent(client)
    slug = _team(client, members=[name])

    response = _put(client, _key(), _steps(name), team_slug=slug)

    assert response.status_code == 200, response.text
    assert response.json()["workflow"]["team_slug"] == slug


# ---------------------------------------------------------------------------
# Listing and deleting
# ---------------------------------------------------------------------------


def test_the_list_is_ordered_by_key(client: TestClient) -> None:
    name = _agent(client)
    keys = sorted(_key(prefix) for prefix in ("cc", "aa", "bb"))
    for key in reversed(keys):
        _put(client, key, _steps(name))

    listed = client.get(WORKFLOWS_URL).json()["workflows"]

    assert [workflow["workflow_key"] for workflow in listed] == keys


def test_deleting_a_workflow_reports_the_open_tasks_that_named_it(
    client: TestClient,
) -> None:
    """The count is returned rather than used to refuse. A workflow somebody
    wants gone is usually one that is going wrong."""
    name = _agent(client)
    slug = _team(client, members=[name])
    key = _key()
    _put(client, key, _steps(name), team_slug=slug)
    _import_one(client, workflow_key=key, team_slug=slug)

    deleted = client.delete(f"{WORKFLOWS_URL}/{key}")

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["open_task_count"] == 1
    assert client.get(f"{WORKFLOWS_URL}/{key}").status_code == 404


def test_deleting_a_workflow_nobody_named_reports_zero(client: TestClient) -> None:
    key = _key()
    _put(client, key, _steps(_agent(client)))

    deleted = client.delete(f"{WORKFLOWS_URL}/{key}")

    assert deleted.json()["open_task_count"] == 0


def test_deleting_a_workflow_that_is_not_there_is_a_404(client: TestClient) -> None:
    response = client.delete(f"{WORKFLOWS_URL}/{_key()}")

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "AGENT_WORKFLOW_NOT_FOUND"


def _import_one(client: TestClient, **extra: Any) -> str:
    """Queue one task, so a delete has an open task to count."""
    ref = f"ref-{uuid.uuid4().hex[:12]}"
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": ref, "title": "a title"}],
    }
    preview = client.post(
        "/api/v1/agent-tasks/import", json={"scope": scope, "mode": "preview", **extra}
    )
    assert preview.status_code == 200, preview.text
    commit = client.post(
        "/api/v1/agent-tasks/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
            **extra,
        },
    )
    assert commit.status_code == 200, commit.text
    return str(commit.json()["task_keys"][0])


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def test_the_two_operations_sit_at_the_tiers_the_plan_names() -> None:
    """Read AUTHENTICATED so the dispatcher does not need an admin key; write
    ADMIN, beside authoring a control."""
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_WORKFLOWS_READ] is (
        AccessLevel.AUTHENTICATED
    )
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_WORKFLOWS_WRITE] is AccessLevel.ADMIN
    assert (
        DEFAULT_OPERATION_ACCESS[Operation.AGENT_WORKFLOWS_WRITE]
        is DEFAULT_OPERATION_ACCESS[Operation.CONTROLS_CREATE]
    )


def test_a_non_admin_may_read_a_workflow_but_not_write_one(
    client: TestClient, non_admin_client: TestClient
) -> None:
    key = _key()
    _put(client, key, _steps(_agent(client)))

    assert non_admin_client.get(f"{WORKFLOWS_URL}/{key}").status_code == 200
    assert non_admin_client.get(WORKFLOWS_URL).status_code == 200
    assert _put(non_admin_client, key, _steps("someone_else")).status_code == 403
    assert non_admin_client.delete(f"{WORKFLOWS_URL}/{key}").status_code == 403


def test_an_unauthenticated_caller_reads_nothing(
    client: TestClient, unauthenticated_client: TestClient
) -> None:
    key = _key()
    _put(client, key, _steps(_agent(client)))

    assert unauthenticated_client.get(f"{WORKFLOWS_URL}/{key}").status_code == 401


def test_a_refused_write_changed_nothing(
    client: TestClient, non_admin_client: TestClient
) -> None:
    first = _agent(client)
    key = _key()
    _put(client, key, _steps(first))

    _put(non_admin_client, key, _steps("someone_else"))

    stored = client.get(f"{WORKFLOWS_URL}/{key}").json()["workflow"]
    assert [step["agent_name"] for step in stored["steps"]] == [first]


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


def test_one_namespaces_workflows_are_invisible_to_another(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    one, two = namespaced
    key = _key()
    _put(one, key, [{"agent_name": "agent_in_ns_one", "brief": "x"}])

    assert two.get(f"{WORKFLOWS_URL}/{key}").status_code == 404
    assert two.get(WORKFLOWS_URL).json()["workflows"] == []
    assert one.get(f"{WORKFLOWS_URL}/{key}").status_code == 200


def test_the_same_key_means_different_workflows_in_two_namespaces(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    one, two = namespaced
    key = _key()

    _put(one, key, [{"agent_name": "agent_in_ns_one", "brief": "x"}])
    _put(two, key, [{"agent_name": "agent_in_ns_two", "brief": "y"}])

    assert one.get(f"{WORKFLOWS_URL}/{key}").json()["workflow"]["steps"][0][
        "agent_name"
    ] == "agent_in_ns_one"
    assert two.get(f"{WORKFLOWS_URL}/{key}").json()["workflow"]["steps"][0][
        "agent_name"
    ] == "agent_in_ns_two"


def test_a_delete_in_one_namespace_leaves_the_other_alone(
    namespaced: tuple[TestClient, TestClient]
) -> None:
    one, two = namespaced
    key = _key()
    _put(one, key, [{"agent_name": "agent_in_ns_one", "brief": "x"}])
    _put(two, key, [{"agent_name": "agent_in_ns_two", "brief": "y"}])

    two.delete(f"{WORKFLOWS_URL}/{key}")

    assert one.get(f"{WORKFLOWS_URL}/{key}").status_code == 200
    assert two.get(f"{WORKFLOWS_URL}/{key}").status_code == 404


# ---------------------------------------------------------------------------
# The architectural line, extended to this phase's modules
#
# The same tripwire ``test_agent_tasks_endpoints.py`` puts on the ledger, on
# the two modules phase 5 added. A workflow is a list the *dispatcher* walks;
# the moment something here walks it instead, the loop has moved inside the
# control plane and every argument in section 3 has been lost - a connection
# held per running step against a pool of five plus ten overflow, and a queue
# polled by N replicas, which is the double-claim bug by construction.
# ---------------------------------------------------------------------------


WORKFLOW_MODULES = (
    "services/agent_workflows.py",
    "endpoints/agent_workflows.py",
)

FORBIDDEN_IN_THE_CONTROL_PLANE = (
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


@pytest.mark.parametrize("module", WORKFLOW_MODULES)
def test_the_workflow_modules_hold_no_loop_no_worker_and_no_timer(module: str) -> None:
    source = (Path(agent_control_server.__file__).parent / module).read_text()
    body = ast.unparse(_without_docstrings(ast.parse(source)))

    for forbidden in FORBIDDEN_IN_THE_CONTROL_PLANE:
        assert forbidden not in body, (
            f"{module} contains {forbidden!r}; the chain is walked outside this server"
        )


@pytest.mark.parametrize("module", WORKFLOW_MODULES)
def test_nothing_here_starts_a_turn_or_opens_a_session(module: str) -> None:
    """A workflow module that could start a turn would be the dispatch loop
    with a different name on it. The server answers questions about rows."""
    source = (Path(agent_control_server.__file__).parent / module).read_text()
    body = ast.unparse(_without_docstrings(ast.parse(source)))

    for forbidden in ("run_turn", "ExecutorClient", "create_session", "httpx"):
        assert forbidden not in body, f"{module} reaches for {forbidden!r}"


def test_the_observability_docstring_no_longer_calls_a_trace_a_task() -> None:
    """Section 2's rename, which this phase owns.

    Two things were called a task: a row in ``agent_tasks`` and a trace rollup.
    The console word for the row is "task" and the console word for the rollup
    is "chain", and an operator should not have to know the schema to tell
    which page they are on.
    """
    source = (
        Path(agent_control_server.__file__).parent / "endpoints" / "observability.py"
    ).read_text()

    assert "Read one multi-agent chain as a chain of hops." in source or (
        "one multi-agent chain" in source
    )
    assert "Read one multi-agent task as a chain of hops." not in source
