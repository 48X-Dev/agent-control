"""Server-side guarantees the teams overview page depends on.

The overview renders one card per team from ``GET /teams`` and fills the agent
name pills from ``GET /teams/{slug}``. These tests pin the parts of that
contract the UI would break on: the shape of a list row, an empty namespace
returning a list rather than an error, teams with no members, and payloads
large enough to push a card's layout.
"""

from __future__ import annotations

import uuid
from typing import Any

from agent_control_models.teams import (
    TEAM_DESCRIPTION_MAX_LENGTH,
    TEAM_DISPLAY_NAME_MAX_LENGTH,
)
from fastapi.testclient import TestClient

_TEAMS_URL = "/api/v1/teams"

# Fields the overview card reads off a list row. A rename or removal on the
# server should fail here rather than as a blank card in the browser.
_CARD_FIELDS = frozenset({"id", "slug", "display_name", "description", "member_count"})


def _agent_name() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


def _register_agent(client: TestClient, agent_name: str) -> None:
    resp = client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent_name,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert resp.status_code == 200, resp.text


def _create_team(
    client: TestClient,
    *,
    display_name: str,
    description: str | None = None,
) -> str:
    body: dict[str, Any] = {"display_name": display_name}
    if description is not None:
        body["description"] = description
    resp = client.put(_TEAMS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["slug"])


# =============================================================================
# Empty namespace
# =============================================================================


def test_list_teams_in_empty_namespace_returns_an_empty_page(
    client: TestClient,
) -> None:
    # Given: a namespace with no teams
    # When: the overview loads its list
    resp = client.get(_TEAMS_URL)

    # Then: it is a 200 with an empty list, not an error the UI must special-case
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["teams"] == []
    assert body["pagination"]["total"] == 0
    assert body["pagination"]["has_more"] is False
    assert body["pagination"]["next_cursor"] is None


def test_list_teams_with_ui_page_size_is_accepted(client: TestClient) -> None:
    # Given: the limit the overview requests (one page covers every team)
    _create_team(client, display_name="Engineering")

    resp = client.get(_TEAMS_URL, params={"limit": 100})

    assert resp.status_code == 200, resp.text
    assert resp.json()["pagination"]["limit"] == 100


# =============================================================================
# Card payload shape
# =============================================================================


def test_list_row_carries_every_field_the_card_renders(client: TestClient) -> None:
    # Given: a team with a description
    _create_team(
        client,
        display_name="Sales & Outreach",
        description="Owns pipeline and prospecting.",
    )

    # When: the overview lists teams
    row = client.get(_TEAMS_URL).json()["teams"][0]

    # Then: every field a card reads is present with the expected value
    assert _CARD_FIELDS <= set(row)
    assert row["slug"] == "sales-outreach"
    assert row["display_name"] == "Sales & Outreach"
    assert row["description"] == "Owns pipeline and prospecting."
    assert row["member_count"] == 0


def test_team_without_description_lists_null_rather_than_omitting_it(
    client: TestClient,
) -> None:
    # Given: a team created without a description
    _create_team(client, display_name="Operations")

    # When/Then: the field is present and null, so the card can skip the line
    row = client.get(_TEAMS_URL).json()["teams"][0]
    assert "description" in row
    assert row["description"] is None


# =============================================================================
# Teams with no members
# =============================================================================


def test_empty_team_lists_zero_members_and_reads_back_empty(
    client: TestClient,
) -> None:
    # Given: a team nobody has joined
    slug = _create_team(client, display_name="Marketing")

    # When: the overview reads the list and then the team itself
    row = client.get(_TEAMS_URL).json()["teams"][0]
    detail = client.get(f"{_TEAMS_URL}/{slug}")

    # Then: the count is zero and the member list is empty, not null
    assert row["member_count"] == 0
    assert detail.status_code == 200, detail.text
    assert detail.json()["members"] == []
    assert detail.json()["member_count"] == 0


def test_empty_and_populated_teams_coexist_on_one_page(client: TestClient) -> None:
    # Given: one team with members and one without
    populated = _create_team(client, display_name="Engineering")
    empty = _create_team(client, display_name="Marketing")
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    assert client.post(f"{_TEAMS_URL}/{populated}/members/{agent_name}").status_code == 200

    # When: the overview lists teams
    counts = {team["slug"]: team["member_count"] for team in client.get(_TEAMS_URL).json()["teams"]}

    # Then: both appear, and the empty one is a zero rather than a missing row
    assert counts == {populated: 1, empty: 0}


# =============================================================================
# Payloads large enough to stress the card
# =============================================================================


def test_long_display_name_and_description_round_trip_verbatim(
    client: TestClient,
) -> None:
    # Given: a display name and description at their documented maximums
    display_name = ("Extremely " + "Long " * 60 + "Team Name")[:TEAM_DISPLAY_NAME_MAX_LENGTH]
    description = "d" * TEAM_DESCRIPTION_MAX_LENGTH

    # When: the team is created and listed
    slug = _create_team(client, display_name=display_name, description=description)
    row = client.get(_TEAMS_URL).json()["teams"][0]

    # Then: the server stores and returns them untruncated. Fitting them into
    # a card is the UI's job; the API must not silently shorten them.
    assert row["display_name"] == display_name
    assert row["description"] == description
    assert len(row["display_name"]) == TEAM_DISPLAY_NAME_MAX_LENGTH
    assert len(row["description"]) == TEAM_DESCRIPTION_MAX_LENGTH
    assert len(slug) <= 255


def test_team_with_many_members_reports_the_full_count(client: TestClient) -> None:
    # Given: a team with more members than a card can show
    slug = _create_team(client, display_name="Engineering")
    names = sorted(_agent_name() for _ in range(12))
    for name in names:
        _register_agent(client, name)
        assert client.post(f"{_TEAMS_URL}/{slug}/members/{name}").status_code == 200

    # When: the overview reads the count and then the members
    row = client.get(_TEAMS_URL).json()["teams"][0]
    detail = client.get(f"{_TEAMS_URL}/{slug}").json()

    # Then: the count is the true total, and the members come back ordered so
    # the card's preview is stable between loads
    assert row["member_count"] == 12
    assert [m["agent_name"] for m in detail["members"]] == names


# =============================================================================
# The overview is readable without admin rights
# =============================================================================


def test_read_only_caller_can_load_the_whole_overview(
    non_admin_client: TestClient, client: TestClient
) -> None:
    # Given: a populated team created by an admin
    slug = _create_team(client, display_name="Operations")
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    # When: a non-admin makes exactly the two calls the overview makes
    listing = non_admin_client.get(_TEAMS_URL, params={"limit": 100})
    detail = non_admin_client.get(f"{_TEAMS_URL}/{slug}")

    # Then: both succeed with the data the cards need
    assert listing.status_code == 200, listing.text
    assert listing.json()["teams"][0]["member_count"] == 1
    assert detail.status_code == 200, detail.text
    assert [m["agent_name"] for m in detail.json()["members"]] == [agent_name]
