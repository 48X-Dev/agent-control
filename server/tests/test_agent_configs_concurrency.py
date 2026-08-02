"""Two admins editing one configuration at the same time, over real sockets.

``TestClient`` serializes requests, so it cannot express this race at all. The
whole point of ``expected_version`` is what happens when two writes overlap, and
a test that cannot produce an overlap is asserting nothing.

The race is a read-modify-write: every write reads ``current_version``, compares
it, and increments it. Without ``SELECT ... FOR UPDATE`` two requests both read
version 7, both pass the check, and both write - so one admin's paragraph
disappears with no signal in the UI. The change is in the history, but nobody
reads history until behaviour breaks, which may be days later.

``SELECT ... FOR UPDATE`` is a **no-op on SQLite**, so every case here skips
rather than passing vacuously when the suite is not pointed at PostgreSQL. A
test that silently proves nothing about a locking bug is worse than no test.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from agent_control_models.agent_configs import AgentModelOption
from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_control_server.config import db_config, model_settings

from .conftest import TEST_ADMIN_API_KEY, LiveServer

pytestmark = pytest.mark.skipif(
    make_url(db_config.get_url()).get_backend_name() != "postgresql",
    reason=(
        "SELECT ... FOR UPDATE is a no-op on SQLite, so these races would pass "
        "without proving the lock exists."
    ),
)

_ECONOMY = AgentModelOption(
    id="gpt-5.4-mini",
    label="GPT 5.4 mini",
    provider="openai_compatible",
    cost_tier="economy",
)
_PREMIUM = AgentModelOption(
    id="gpt-5.6-sol",
    label="GPT 5.6 sol",
    provider="openai_compatible",
    cost_tier="premium",
)


@pytest.fixture(autouse=True)
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY, _PREMIUM])


def _url(agent_name: str, suffix: str = "") -> str:
    return f"/api/v1/agents/{agent_name}/config{suffix}"


async def _register(client: httpx.AsyncClient) -> str:
    name = f"agent-{uuid.uuid4().hex[:12]}"
    resp = await client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": name,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert resp.status_code == 200, resp.text
    return name


def _version_rows(db_engine: Any, agent_name: str) -> list[tuple[Any, ...]]:
    with db_engine.begin() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT version_num, event_type, body, model_id "
                    "  FROM agent_config_versions "
                    " WHERE agent_name = :n ORDER BY version_num"
                ),
                {"n": agent_name},
            ).fetchall()
        )


async def test_two_overlapping_saves_at_one_version_yield_one_200_and_one_409(
    live_server: LiveServer,
) -> None:
    """One integer on the wire buys a loud failure instead of a quiet one.

    Last-write-wins was the alternative, and on a free-text field it destroys a
    colleague's paragraph with no signal at all.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    agent = await _register(client)
    first = await client.put(_url(agent), json={"body": "Base.", "expected_version": 0})
    assert first.status_code == 200, first.text

    responses = await asyncio.gather(
        client.put(_url(agent), json={"body": "Alice's edit.", "expected_version": 1}),
        client.put(_url(agent), json={"body": "Bob's edit.", "expected_version": 1}),
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]

    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["error_code"] == "AGENT_CONFIG_VERSION_CONFLICT"

    final = (await client.get(_url(agent))).json()
    assert final["current_version"] == 2
    assert final["body"] in {"Alice's edit.", "Bob's edit."}


async def test_a_prompt_edit_racing_a_model_edit_still_resolves_to_one_winner(
    live_server: LiveServer,
) -> None:
    """One row and one version, so unrelated-looking edits really do conflict.

    That is the trade the single-operation design makes deliberately: two
    operations racing one counter would produce 409s between edits that share
    nothing, and both would land at ADMIN anyway.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    agent = await _register(client)
    await client.put(
        _url(agent),
        json={"body": "Base.", "model_id": _ECONOMY.id, "expected_version": 0},
    )

    responses = await asyncio.gather(
        client.put(_url(agent), json={"body": "New prompt.", "expected_version": 1}),
        client.put(_url(agent), json={"model_id": _PREMIUM.id, "expected_version": 1}),
    )

    assert sorted(r.status_code for r in responses) == [200, 409]

    final = (await client.get(_url(agent))).json()
    assert final["current_version"] == 2
    # Whichever won, the other field is untouched rather than half-applied.
    if final["body"] == "New prompt.":
        assert final["model_id"] == _ECONOMY.id
    else:
        assert final["body"] == "Base."
        assert final["model_id"] == _PREMIUM.id


async def test_eight_simultaneous_saves_produce_exactly_one_winner(
    live_server: LiveServer, db_engine: Any
) -> None:
    """No lost update, no duplicate version number, and no 500.

    Without the row lock the losers do not fail cleanly: they collide on the
    unique constraint over ``(namespace_key, agent_name, version_num)`` and
    surface to an operator as a server error for behaviour that is entirely
    legitimate.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    agent = await _register(client)
    await client.put(_url(agent), json={"body": "Base.", "expected_version": 0})

    responses = await asyncio.gather(
        *(
            client.put(
                _url(agent), json={"body": f"Edit {n}.", "expected_version": 1}
            )
            for n in range(8)
        )
    )

    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1, [r.text for r in responses]
    assert set(statuses) == {200, 409}

    rows = _version_rows(db_engine, agent)
    assert [row[0] for row in rows] == [1, 2]


async def test_two_first_saves_on_one_agent_do_not_both_insert(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The row lock cannot serialize the *first* write, so something else must.

    ``SELECT ... FOR UPDATE`` against a row that does not exist yet locks
    nothing: both requests read version 0, both pass the check, and both insert.
    The loser has to come back as the same 409 every other concurrent write
    produces, not as a 500 and a poisoned transaction.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    agent = await _register(client)

    responses = await asyncio.gather(
        client.put(_url(agent), json={"body": "Alice first.", "expected_version": 0}),
        client.put(_url(agent), json={"body": "Bob first.", "expected_version": 0}),
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]
    assert all(r.status_code != 500 for r in responses)

    rows = _version_rows(db_engine, agent)
    assert [row[0] for row in rows] == [1]


async def test_a_clear_racing_a_save_resolves_deterministically(
    live_server: LiveServer,
) -> None:
    """Clearing takes a body precisely so it can join this ordering.

    Otherwise "stop using the managed prompt" could land on top of a save it
    never saw, and the operator who cleared would not know their colleague's
    edit had just been thrown away.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    agent = await _register(client)
    await client.put(_url(agent), json={"body": "Base.", "expected_version": 0})

    responses = await asyncio.gather(
        client.put(_url(agent), json={"body": "Still editing.", "expected_version": 1}),
        client.post(_url(agent, ":clear-prompt"), json={"expected_version": 1}),
    )

    assert sorted(r.status_code for r in responses) == [200, 409]

    final = (await client.get(_url(agent))).json()
    assert final["current_version"] == 2
    assert (final["body"] is None) != (final["body"] == "Still editing.")


async def test_two_restores_of_the_same_version_do_not_both_apply(
    live_server: LiveServer, db_engine: Any
) -> None:
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    agent = await _register(client)
    await client.put(_url(agent), json={"body": "One.", "expected_version": 0})
    await client.put(_url(agent), json={"body": "Two.", "expected_version": 1})

    responses = await asyncio.gather(
        client.post(_url(agent, "/versions/1:restore"), json={"expected_version": 2}),
        client.post(_url(agent, "/versions/1:restore"), json={"expected_version": 2}),
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    rows = _version_rows(db_engine, agent)
    assert [row[0] for row in rows] == [1, 2, 3]
