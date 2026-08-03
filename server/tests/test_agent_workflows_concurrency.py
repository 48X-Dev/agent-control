"""Two admins writing one workflow at the same time, over real sockets.

``TestClient`` serializes requests, so it cannot produce the race at all, and a
test that cannot produce an overlap is asserting nothing.

The race is a read-then-insert. ``upsert_workflow`` looks for an existing row,
finds none, and inserts - and two callers can both look before either inserts.
Without the savepoint the loser's ``INSERT`` violates
``ux_agent_workflows_key`` and surfaces to an admin as a 500 for behaviour that
is entirely legitimate: two people configuring the same chain a second apart.
With it, the loser rolls back only its own insert, re-reads the winning row and
applies its own values as an update.

**What must never happen is two rows.** ``plan_for_task`` reads one workflow by
key and a second row with the same key would mean which agents a task runs
depends on which row the query happened to return, with no way for an operator
to see that there were two.

Every case skips rather than passing vacuously on SQLite: the unique constraint
and the savepoint both behave differently there, so a green run would prove
nothing about the collision this file exists for.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from agent_control_models.workflows import AgentWorkflowStep
from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_control_server.config import db_config
from agent_control_server.services.agent_workflows import AgentWorkflowsService

from .conftest import TEST_ADMIN_API_KEY, AsyncSessionTest, LiveServer

pytestmark = pytest.mark.skipif(
    make_url(db_config.get_url()).get_backend_name() != "postgresql",
    reason=(
        "The savepoint retry is a Postgres unique-violation path; on SQLite "
        "these races would pass without exercising it."
    ),
)

WORKFLOWS_URL = "/api/v1/agent-workflows"


def _key() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _body(agent_name: str, *, display_name: str = "A chain") -> dict[str, Any]:
    return {
        "display_name": display_name,
        "steps": [{"agent_name": agent_name, "brief": "do the thing"}],
    }


def _rows(db_engine: Any, workflow_key: str) -> list[tuple[Any, ...]]:
    with db_engine.begin() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT namespace_key, workflow_key, display_name "
                    "  FROM agent_workflows WHERE workflow_key = :key"
                ),
                {"key": workflow_key},
            ).fetchall()
        )


async def test_two_simultaneous_creates_of_one_key_leave_exactly_one_row(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The loser applies its values as an update instead of crashing.

    Both callers hold ADMIN and both asked for a legitimate thing. Neither
    should be told the server broke, and neither should end up with a second
    row nobody can see.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    key = _key()

    responses = await asyncio.gather(
        client.put(f"{WORKFLOWS_URL}/{key}", json=_body("alice_agent_one", display_name="Alice")),
        client.put(f"{WORKFLOWS_URL}/{key}", json=_body("bob_agent_one", display_name="Bob")),
    )

    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]
    assert len(_rows(db_engine, key)) == 1
    stored = (await client.get(f"{WORKFLOWS_URL}/{key}")).json()["workflow"]
    assert stored["display_name"] in {"Alice", "Bob"}
    assert [step["agent_name"] for step in stored["steps"]] in (
        ["alice_agent_one"],
        ["bob_agent_one"],
    )


async def test_eight_simultaneous_writes_produce_one_row_and_no_server_error(
    live_server: LiveServer, db_engine: Any
) -> None:
    """A wider version of the same race, because one overlap can be luck."""
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    key = _key()

    responses = await asyncio.gather(
        *(
            client.put(
                f"{WORKFLOWS_URL}/{key}",
                json=_body(f"racing_agent_{index}", display_name=f"Writer {index}"),
            )
            for index in range(8)
        )
    )

    assert {response.status_code for response in responses} == {200}, [
        response.text for response in responses
    ]
    assert len(_rows(db_engine, key)) == 1
    created = [response.json()["created"] for response in responses]
    assert created.count(True) == 1, "exactly one caller created the row"


async def test_a_losing_insert_recovers_into_an_update_rather_than_raising(
    db_engine: Any,
) -> None:
    """The savepoint branch, forced rather than hoped for.

    The two HTTP races above assert the *outcome* - one row, no 500 - and both
    would still pass if the requests happened to serialize, which means neither
    proves the recovery path runs. This drives it directly: one session inserts
    and holds the transaction open, a second reads before that commit, finds
    nothing, and inserts. Postgres blocks the second insert until the first
    commits and then raises the unique violation, which is exactly the sequence
    the savepoint exists for.
    """
    key = _key()
    async with AsyncSessionTest() as first, AsyncSessionTest() as second:
        await AgentWorkflowsService(first).upsert_workflow(
            namespace_key="default",
            workflow_key=key,
            display_name="First",
            team_slug=None,
            steps=[AgentWorkflowStep(agent_name="first_agent_one", brief="a")],
        )

        loser = asyncio.create_task(
            AgentWorkflowsService(second).upsert_workflow(
                namespace_key="default",
                workflow_key=key,
                display_name="Second",
                team_slug=None,
                steps=[AgentWorkflowStep(agent_name="second_agent_one", brief="b")],
            )
        )
        # Long enough for the second session to have read, found nothing and
        # blocked on its own insert.
        await asyncio.sleep(0.2)
        await first.commit()

        workflow, created = await loser
        await second.commit()

    assert created is False, "the loser reported an update, not a creation"
    assert workflow.display_name == "Second"
    assert len(_rows(db_engine, key)) == 1


async def test_a_write_racing_a_delete_ends_with_one_answer_or_none(
    live_server: LiveServer, db_engine: Any
) -> None:
    """Neither ordering may leave two rows or a 500.

    A delete landing between another caller's read and its insert is the same
    collision from the other side, and the outcome an operator has to be able
    to reason about is "the workflow is either there once or not at all".
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    key = _key()
    seeded = await client.put(f"{WORKFLOWS_URL}/{key}", json=_body("seed_agent_one"))
    assert seeded.status_code == 200, seeded.text

    write, delete = await asyncio.gather(
        client.put(f"{WORKFLOWS_URL}/{key}", json=_body("later_agent_one")),
        client.delete(f"{WORKFLOWS_URL}/{key}"),
    )

    assert write.status_code in {200, 404, 409}, write.text
    assert delete.status_code in {200, 404, 409}, delete.text
    assert len(_rows(db_engine, key)) <= 1


async def test_the_same_key_in_two_namespaces_does_not_contend(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The unique constraint is on ``(namespace_key, workflow_key)``.

    A constraint on the key alone would make one tenant's configuration block
    another's, and it would only show up under load.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    key = _key()

    first = await client.put(f"{WORKFLOWS_URL}/{key}", json=_body("shared_agent_one"))
    assert first.status_code == 200, first.text
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_workflows (namespace_key, workflow_key, display_name, steps) "
                "VALUES ('ns-other', :key, 'Theirs', '[]'::jsonb)"
            ),
            {"key": key},
        )

    assert len(_rows(db_engine, key)) == 2
    listed = (await client.get(WORKFLOWS_URL)).json()["workflows"]
    assert [workflow["workflow_key"] for workflow in listed] == [key]


async def test_a_plan_read_races_a_workflow_rewrite_without_a_500(
    live_server: LiveServer,
) -> None:
    """The dispatcher reads a plan for every task it claims, and an admin can
    rewrite the workflow at any moment. The read has to answer either the old
    shape or the new one, never a stack trace."""
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    key = _key()
    await client.put(f"{WORKFLOWS_URL}/{key}", json=_body("first_agent_one"))

    ref = f"ref-{uuid.uuid4().hex[:12]}"
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": ref, "title": "a title"}],
    }
    preview = await client.post(
        "/api/v1/agent-tasks/import",
        json={"scope": scope, "mode": "preview", "workflow_key": key},
    )
    commit = await client.post(
        "/api/v1/agent-tasks/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
            "workflow_key": key,
        },
    )
    assert commit.status_code == 200, commit.text
    task_key = commit.json()["task_keys"][0]

    reads, rewrite = await asyncio.gather(
        asyncio.gather(
            *(client.get(f"/api/v1/agent-tasks/{task_key}/plan") for _ in range(4))
        ),
        client.put(f"{WORKFLOWS_URL}/{key}", json=_body("second_agent_one")),
    )

    assert rewrite.status_code == 200, rewrite.text
    assert {response.status_code for response in reads} == {200}, [r.text for r in reads]
    for response in reads:
        agents = [step["agent_name"] for step in response.json()["plan"]["steps"]]
        assert agents in (["first_agent_one"], ["second_agent_one"])
