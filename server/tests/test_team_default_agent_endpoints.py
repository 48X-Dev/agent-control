"""``default_agent_name`` on ``/teams``: the second and last source of an agent.

A workflow step that names nobody falls back to this field, so it is the other
half of section 8's rule. One invariant holds the whole thing together and every
test here is about keeping it true on the way in *and* on the way out:

**The default has to be a member of the team.** The field answers "who runs a
workflow step that named nobody", and answering it with an agent nobody put on
this team is a way to run an agent under a team's configuration without joining
the team. Enforced on the write, and re-enforced when the membership that made
it legal is removed - an invariant checked only on the way in is an invariant
that quietly stops holding.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient

TEAMS_URL = "/api/v1/teams"
AGENTS_URL = "/api/v1/agents"


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


def _patch(client: TestClient, slug: str, **body: Any) -> Any:
    return client.patch(f"{TEAMS_URL}/{slug}", json=body)


def _default_of(client: TestClient, slug: str) -> str | None:
    response = client.get(f"{TEAMS_URL}/{slug}")
    assert response.status_code == 200, response.text
    value = response.json()["default_agent_name"]
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# Setting it
# ---------------------------------------------------------------------------


def test_a_member_can_be_made_the_teams_default(client: TestClient) -> None:
    name = _agent(client)
    slug = _team(client, members=[name])

    response = _patch(client, slug, default_agent_name=name)

    assert response.status_code == 200, response.text
    assert response.json()["default_agent_name"] == name
    assert _default_of(client, slug) == name


def test_a_non_member_is_refused_as_the_default(client: TestClient) -> None:
    """Otherwise the field is a way to run an agent under a team's controls
    without joining the team."""
    outsider = _agent(client)
    slug = _team(client, members=[_agent(client)])

    response = _patch(client, slug, default_agent_name=outsider)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "AGENT_NOT_IN_TEAM"
    assert _default_of(client, slug) is None


def test_a_team_being_created_cannot_be_given_a_default(client: TestClient) -> None:
    """A new team has no members, so the membership check could never pass.
    Refused with the reason rather than silently accepted."""
    response = client.put(
        TEAMS_URL,
        json={"display_name": f"Team {uuid.uuid4().hex[:8]}", "default_agent_name": _agent(client)},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "AGENT_NOT_IN_TEAM"


def test_omitting_the_field_leaves_it_alone(client: TestClient) -> None:
    name = _agent(client)
    slug = _team(client, members=[name])
    _patch(client, slug, default_agent_name=name)

    _patch(client, slug, display_name="Renamed")

    assert _default_of(client, slug) == name


def test_an_explicit_null_clears_it(client: TestClient) -> None:
    name = _agent(client)
    slug = _team(client, members=[name])
    _patch(client, slug, default_agent_name=name)

    response = _patch(client, slug, default_agent_name=None)

    assert response.status_code == 200, response.text
    assert _default_of(client, slug) is None


# ---------------------------------------------------------------------------
# Keeping it true
# ---------------------------------------------------------------------------


def test_removing_the_default_agent_from_the_team_clears_the_default(
    client: TestClient,
) -> None:
    """Leaving it would let the removal look like it took effect while workflow
    steps naming no agent kept resolving to the agent somebody had just taken
    off the team."""
    name = _agent(client)
    slug = _team(client, members=[name])
    _patch(client, slug, default_agent_name=name)

    removed = client.delete(f"{TEAMS_URL}/{slug}/members/{name}")

    assert removed.status_code == 200, removed.text
    assert _default_of(client, slug) is None


def test_removing_a_different_member_leaves_the_default_alone(
    client: TestClient,
) -> None:
    kept, going = _agent(client), _agent(client)
    slug = _team(client, members=[kept, going])
    _patch(client, slug, default_agent_name=kept)

    client.delete(f"{TEAMS_URL}/{slug}/members/{going}")

    assert _default_of(client, slug) == kept


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def test_a_non_admin_cannot_set_the_default_agent(
    client: TestClient, non_admin_client: TestClient
) -> None:
    """Choosing the agent a workflow step falls back to is choosing the blast
    radius, so it sits at the tier that writes the workflow itself."""
    name = _agent(client)
    slug = _team(client, members=[name])

    response = _patch(non_admin_client, slug, default_agent_name=name)

    assert response.status_code == 403, response.text
    assert _default_of(client, slug) is None
