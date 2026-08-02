"""The audit log: what a version row records, and what it must never lose.

Changing a prompt changes agent behaviour as much as changing a control, and
controls already have a version log. A behaviour-changing field with no history
next to a behaviour-changing field with history is an inconsistency somebody has
to explain to a customer during an incident.

Two properties matter more than the rest.

**Version numbers only ever go up.** Restoring copies a version's fields forward
as a *new* version. A shared history that can be rewritten is a history nobody
can reason about, and the rewind is exactly the operation somebody would reach
for to hide a change.

**Clearing does not delete anything.** The version rows' foreign key points at
``agents`` rather than at ``agent_configs`` precisely so that clearing a field
cannot destroy the history that makes clearing recoverable.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_models.agent_configs import AgentModelOption
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from agent_control_server.config import model_settings
from agent_control_server.services.agent_configs import compute_etag

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


def _agent_name() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def agent(client: TestClient) -> str:
    name = _agent_name()
    resp = client.post(
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


def _url(agent_name: str, suffix: str = "") -> str:
    return f"/api/v1/agents/{agent_name}/config{suffix}"


def _versions(client: TestClient, agent_name: str) -> list[dict[str, Any]]:
    resp = client.get(_url(agent_name, "/versions"), params={"limit": 200})
    assert resp.status_code == 200, resp.text
    return resp.json()["versions"]


def _detail(client: TestClient, agent_name: str, version_num: int) -> dict[str, Any]:
    resp = client.get(_url(agent_name, f"/versions/{version_num}"))
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


def _exercise_every_event_type(client: TestClient, agent_name: str) -> None:
    """Produce one row of each of the seven event types, in order."""
    client.put(
        _url(agent_name),
        json={"body": "One.", "model_id": _ECONOMY.id, "expected_version": 0},
    )  # created
    client.put(
        _url(agent_name), json={"body": "Two.", "expected_version": 1}
    )  # updated
    client.patch(
        _url(agent_name), json={"prompt_enabled": False, "expected_version": 2}
    )  # disabled
    client.patch(
        _url(agent_name), json={"prompt_enabled": True, "expected_version": 3}
    )  # enabled
    client.post(
        _url(agent_name, ":clear-model"), json={"expected_version": 4}
    )  # model_cleared
    client.post(
        _url(agent_name, ":clear-prompt"), json={"expected_version": 5}
    )  # prompt_cleared
    client.post(
        _url(agent_name, "/versions/1:restore"), json={"expected_version": 6}
    )  # restored


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------


def test_version_numbers_are_monotonic_across_every_kind_of_write(
    client: TestClient, agent: str
) -> None:
    _exercise_every_event_type(client, agent)

    numbers = [v["version_num"] for v in _versions(client, agent)]
    assert numbers == [7, 6, 5, 4, 3, 2, 1]
    assert client.get(_url(agent)).json()["current_version"] == 7


def test_a_restore_does_not_rewind_the_counter(client: TestClient, agent: str) -> None:
    client.put(_url(agent), json={"body": "One.", "expected_version": 0})
    client.put(_url(agent), json={"body": "Two.", "expected_version": 1})

    client.post(_url(agent, "/versions/1:restore"), json={"expected_version": 2})

    assert client.get(_url(agent)).json()["current_version"] == 3
    assert [v["version_num"] for v in _versions(client, agent)] == [3, 2, 1]


def test_the_seven_event_types_all_appear(client: TestClient, agent: str) -> None:
    """Every state change an operator can cause is nameable in the history."""
    _exercise_every_event_type(client, agent)

    assert [v["event_type"] for v in _versions(client, agent)] == [
        "restored",
        "prompt_cleared",
        "model_cleared",
        "enabled",
        "disabled",
        "updated",
        "created",
    ]


def test_clearing_names_which_field_went(client: TestClient, agent: str) -> None:
    """A single ``cleared`` value on a two-field row would be unreadable.

    The history panel would render "cleared" against a row whose prompt is
    perfectly intact, and the operator reading it during an incident would draw
    the wrong conclusion.
    """
    client.put(
        _url(agent),
        json={"body": "Body.", "model_id": _ECONOMY.id, "expected_version": 0},
    )
    client.post(_url(agent, ":clear-model"), json={"expected_version": 1})
    client.post(_url(agent, ":clear-prompt"), json={"expected_version": 2})

    rows = _versions(client, agent)
    assert rows[1]["event_type"] == "model_cleared"
    assert rows[0]["event_type"] == "prompt_cleared"


# ---------------------------------------------------------------------------
# What each row carries
# ---------------------------------------------------------------------------


def test_each_version_stores_the_full_body_rather_than_a_diff(
    client: TestClient, agent: str
) -> None:
    """Reconstructing text from a diff chain is a class of bug nobody needs.

    "From what to what" is answered by diffing consecutive rows, which the
    client does; the storage stays boring.
    """
    for n, body in enumerate(["First body.", "Second body.", "Third body."]):
        client.put(_url(agent), json={"body": body, "expected_version": n})

    assert _detail(client, agent, 1)["body"] == "First body."
    assert _detail(client, agent, 2)["body"] == "Second body."
    assert _detail(client, agent, 3)["body"] == "Third body."


def test_each_version_stores_the_model_that_was_live_at_that_point(
    client: TestClient, agent: str
) -> None:
    client.put(
        _url(agent), json={"model_id": _ECONOMY.id, "expected_version": 0}
    )
    client.put(
        _url(agent), json={"model_id": _PREMIUM.id, "expected_version": 1}
    )
    client.post(_url(agent, ":clear-model"), json={"expected_version": 2})

    assert _detail(client, agent, 1)["model_id"] == _ECONOMY.id
    assert _detail(client, agent, 2)["model_id"] == _PREMIUM.id
    assert _detail(client, agent, 3)["model_id"] is None


def test_a_version_row_carries_the_etag_that_was_current_when_it_was_written(
    client: TestClient, agent: str
) -> None:
    """So a stale etag reported by an agent can be matched to a version."""
    client.put(
        _url(agent),
        json={"body": "Body.", "model_id": _ECONOMY.id, "expected_version": 0},
    )

    row = _detail(client, agent, 1)
    assert row["etag"] == compute_etag(
        current_version=1, body="Body.", model_id=_ECONOMY.id
    )
    assert row["etag"] == client.get(_url(agent)).json()["etag"]


def test_a_note_is_preserved_on_the_row_that_carried_it(
    client: TestClient, agent: str
) -> None:
    client.put(
        _url(agent),
        json={"body": "Body.", "expected_version": 0, "note": "tightening the tone"},
    )
    client.post(
        _url(agent, ":clear-prompt"),
        json={"expected_version": 1, "note": "reverting to code"},
    )

    rows = _versions(client, agent)
    assert rows[1]["note"] == "tightening the tone"
    assert rows[0]["note"] == "reverting to code"


def test_origin_distinguishes_authored_from_copied_from_reported(
    client: TestClient, agent: str
) -> None:
    """The trace that survives the confused deputy source reporting introduces.

    Text an agent process reports about itself arrives under an AUTHENTICATED
    operation, so moving it into the editor moves untrusted text one click from
    the highest-trust field. The click is deliberate and the row says so.
    """
    client.put(
        _url(agent),
        json={"body": "Typed by hand.", "expected_version": 0, "origin": "authored"},
    )
    client.put(
        _url(agent),
        json={
            "body": "Copied from what the process reported.",
            "expected_version": 1,
            "origin": "copied_from_reported",
        },
    )

    rows = _versions(client, agent)
    assert rows[1]["origin"] == "authored"
    assert rows[0]["origin"] == "copied_from_reported"


def test_a_restore_is_recorded_as_a_restore_regardless_of_the_original_origin(
    client: TestClient, agent: str
) -> None:
    client.put(
        _url(agent),
        json={
            "body": "Copied.",
            "expected_version": 0,
            "origin": "copied_from_reported",
        },
    )
    client.put(_url(agent), json={"body": "Later.", "expected_version": 1})

    client.post(_url(agent, "/versions/1:restore"), json={"expected_version": 2})

    assert _versions(client, agent)[0]["origin"] == "restored"


def test_scan_findings_are_persisted_on_the_version_row(
    client: TestClient, agent: str
) -> None:
    """The record is the whole value, including that a human saved anyway."""
    body = "Deploy with AKIAIOSFODNN7EXAMPLE please."
    client.put(_url(agent), json={"body": body, "expected_version": 0})

    row = _versions(client, agent)[0]
    assert [f["code"] for f in row["scan_findings"]] == ["aws_access_key_id"]
    assert "AKIAIOSFODNN7EXAMPLE" not in str(row["scan_findings"])


def test_a_clean_save_records_an_empty_findings_list(
    client: TestClient, agent: str
) -> None:
    client.put(_url(agent), json={"body": "Perfectly ordinary.", "expected_version": 0})
    assert _versions(client, agent)[0]["scan_findings"] == []


def test_every_row_carries_the_credential_hash_that_wrote_it(
    client: TestClient, agent: str
) -> None:
    """A credential, not a person, and the column is labelled that way.

    Under the shipped default provider every dashboard caller hashes to the same
    value, so "which API key changed this" is answerable and "which human" is
    not. Claiming otherwise in the UI would be worse than saying so.
    """
    client.put(_url(agent), json={"body": "Body.", "expected_version": 0})
    assert _versions(client, agent)[0]["changed_by_hash"]


# ---------------------------------------------------------------------------
# History outliving the state it describes
# ---------------------------------------------------------------------------


def test_clearing_a_prompt_leaves_its_body_readable_in_the_history(
    client: TestClient, agent: str
) -> None:
    """Clearing is a state, not a row removal, and this is why it matters.

    An operator who clears a prompt by mistake gets it back from the history.
    Deleting the rows would make the recoverable operation unrecoverable.
    """
    client.put(_url(agent), json={"body": "Hard to retype.", "expected_version": 0})
    client.post(_url(agent, ":clear-prompt"), json={"expected_version": 1})

    assert client.get(_url(agent)).json()["body"] is None
    assert _detail(client, agent, 1)["body"] == "Hard to retype."

    client.post(_url(agent, "/versions/1:restore"), json={"expected_version": 2})
    assert client.get(_url(agent)).json()["body"] == "Hard to retype."


def test_a_cleared_version_row_records_the_state_after_the_clear(
    client: TestClient, agent: str
) -> None:
    """The row is the new state, not the old one, so ``has_body`` is false."""
    client.put(_url(agent), json={"body": "Body.", "expected_version": 0})
    client.post(_url(agent, ":clear-prompt"), json={"expected_version": 1})

    cleared = _detail(client, agent, 2)
    assert cleared["event_type"] == "prompt_cleared"
    assert cleared["body"] is None
    assert cleared["has_body"] is False


def test_deleting_the_agent_takes_the_configuration_and_its_history(
    client: TestClient, agent: str, db_engine: Engine
) -> None:
    """The agent row is the tenancy anchor; nothing outlives it.

    Asserted against the database rather than through an endpoint because both
    foreign keys point at ``agents`` and the cascade is the guarantee. There is
    no HTTP route that deletes an agent today, so a route-level test would be
    asserting something the product does not offer.
    """
    client.put(
        _url(agent),
        json={"body": "Body.", "model_id": _ECONOMY.id, "expected_version": 0},
    )
    assert _versions(client, agent)

    with db_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM agents WHERE namespace_key='default' AND name=:n"),
            {"n": agent},
        )
        remaining_configs = conn.execute(
            text("SELECT count(*) FROM agent_configs WHERE agent_name=:n"),
            {"n": agent},
        ).scalar_one()
        remaining_versions = conn.execute(
            text("SELECT count(*) FROM agent_config_versions WHERE agent_name=:n"),
            {"n": agent},
        ).scalar_one()

    assert remaining_configs == 0
    assert remaining_versions == 0
    assert client.get(_url(agent)).status_code == 404
