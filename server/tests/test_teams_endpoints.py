"""HTTP-level coverage for the ``/teams`` endpoints.

Exercises the routes against real Postgres through ``TestClient``: slug
derivation and upsert semantics, membership idempotency, both authorization
tiers, and namespace isolation.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from agent_control_server.auth_framework import Operation, Principal, set_authorizer

_TEAMS_URL = "/api/v1/teams"


class HeaderNamespaceAuthorizer:
    """Test authorizer mapping ``X-Test-Namespace`` to ``Principal.namespace_key``."""

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


def _namespace_client(app: FastAPI, namespace_key: str) -> TestClient:
    return TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-Test-Namespace": namespace_key},
    )


def _agent_name() -> str:
    """Return a name long enough to satisfy agent-name normalization."""
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
    slug: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"display_name": display_name}
    if slug is not None:
        body["slug"] = slug
    if description is not None:
        body["description"] = description
    resp = client.put(_TEAMS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _unique_display_name(prefix: str = "Team") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


# =============================================================================
# Happy path
# =============================================================================


def test_create_team_derives_slug_from_display_name(client: TestClient) -> None:
    # Given/When: a team is created from a display name with punctuation
    body = _create_team(client, display_name="Sales & Outreach")

    # Then: the slug is derived and the display name is kept verbatim
    assert body["created"] is True
    assert body["slug"] == "sales-outreach"
    assert isinstance(body["team_id"], int)

    detail = client.get(f"{_TEAMS_URL}/sales-outreach")
    assert detail.status_code == 200, detail.text
    team = detail.json()
    assert team["display_name"] == "Sales & Outreach"
    assert team["slug"] == "sales-outreach"
    assert team["namespace_key"] == "default"
    assert team["member_count"] == 0
    assert team["members"] == []


def test_create_the_four_named_teams(client: TestClient) -> None:
    # Given: the four groupings the feature was asked for
    expected = {
        "Sales & Outreach": "sales-outreach",
        "Operations": "operations",
        "Marketing": "marketing",
        "Engineering": "engineering",
    }

    # When: each is created
    created_slugs = {
        display_name: _create_team(client, display_name=display_name)["slug"]
        for display_name in expected
    }

    # Then: slugs match and all four are listed
    assert created_slugs == expected
    listed = client.get(_TEAMS_URL, params={"limit": 100})
    assert listed.status_code == 200, listed.text
    assert {team["slug"] for team in listed.json()["teams"]} == set(expected.values())


def test_explicit_slug_wins_over_derivation(client: TestClient) -> None:
    body = _create_team(client, display_name="Sales & Outreach", slug="revenue-team")
    assert body["slug"] == "revenue-team"
    assert client.get(f"{_TEAMS_URL}/sales-outreach").status_code == 404


def test_upsert_existing_team_updates_in_place(client: TestClient) -> None:
    # Given: an existing team with a description
    first = _create_team(
        client, display_name="Operations", description="runs the pipes"
    )

    # When: the same slug is upserted with a new display name and no description
    second = client.put(
        _TEAMS_URL, json={"display_name": "Ops & Support", "slug": "operations"}
    )
    assert second.status_code == 200, second.text

    # Then: no new team is created and replace semantics clear the description
    assert second.json()["created"] is False
    assert second.json()["team_id"] == first["team_id"]
    assert second.json()["slug"] == "operations"

    team = client.get(f"{_TEAMS_URL}/operations").json()
    assert team["display_name"] == "Ops & Support"
    assert team["description"] is None
    assert client.get(_TEAMS_URL).json()["pagination"]["total"] == 1


def test_get_team_returns_members_ordered_by_agent_name(client: TestClient) -> None:
    slug = _create_team(client, display_name=_unique_display_name())["slug"]
    names = sorted(_agent_name() for _ in range(3))
    for name in reversed(names):
        _register_agent(client, name)
        add = client.post(f"{_TEAMS_URL}/{slug}/members/{name}")
        assert add.status_code == 200, add.text

    team = client.get(f"{_TEAMS_URL}/{slug}").json()
    assert [member["agent_name"] for member in team["members"]] == names
    assert team["member_count"] == 3


def test_patch_updates_display_name_and_leaves_description(client: TestClient) -> None:
    slug = _create_team(
        client, display_name="Marketing", description="brand and demand"
    )["slug"]

    resp = client.patch(f"{_TEAMS_URL}/{slug}", json={"display_name": "Growth"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["slug"] == "marketing"
    assert body["display_name"] == "Growth"
    assert body["description"] == "brand and demand"


def test_patch_with_explicit_null_clears_description(client: TestClient) -> None:
    slug = _create_team(
        client, display_name="Engineering", description="builds things"
    )["slug"]

    resp = client.patch(f"{_TEAMS_URL}/{slug}", json={"description": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] is None
    assert resp.json()["display_name"] == "Engineering"
    assert client.get(f"{_TEAMS_URL}/{slug}").json()["description"] is None


def test_delete_empty_team_removes_it(client: TestClient) -> None:
    slug = _create_team(client, display_name=_unique_display_name())["slug"]

    resp = client.delete(f"{_TEAMS_URL}/{slug}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "removed_member_count": 0}
    assert client.get(f"{_TEAMS_URL}/{slug}").status_code == 404


def test_remove_member_removes_membership_not_agent(client: TestClient) -> None:
    slug = _create_team(client, display_name=_unique_display_name())["slug"]
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    resp = client.delete(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"removed": True}
    assert client.get(f"{_TEAMS_URL}/{slug}").json()["members"] == []
    assert client.get(f"/api/v1/agents/{agent_name}").status_code == 200


def test_remove_member_is_idempotent(client: TestClient) -> None:
    slug = _create_team(client, display_name=_unique_display_name())["slug"]
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    resp = client.delete(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"removed": False}


def test_list_teams_paginates_by_cursor(client: TestClient) -> None:
    slugs = [
        _create_team(client, display_name=_unique_display_name())["slug"]
        for _ in range(3)
    ]

    first = client.get(_TEAMS_URL, params={"limit": 2})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["teams"]) == 2
    assert first_body["pagination"]["total"] == 3
    assert first_body["pagination"]["has_more"] is True
    cursor = first_body["pagination"]["next_cursor"]
    assert cursor is not None

    second = client.get(_TEAMS_URL, params={"limit": 2, "cursor": cursor})
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert len(second_body["teams"]) == 1
    assert second_body["pagination"]["has_more"] is False
    assert second_body["pagination"]["next_cursor"] is None

    paged = [team["slug"] for team in first_body["teams"] + second_body["teams"]]
    # Newest first, and every team appears exactly once across the two pages.
    assert paged == list(reversed(slugs))


def test_list_teams_reports_member_counts(client: TestClient) -> None:
    populated = _create_team(client, display_name=_unique_display_name())["slug"]
    empty = _create_team(client, display_name=_unique_display_name())["slug"]
    for _ in range(2):
        agent_name = _agent_name()
        _register_agent(client, agent_name)
        client.post(f"{_TEAMS_URL}/{populated}/members/{agent_name}")

    teams = client.get(_TEAMS_URL).json()["teams"]
    counts = {team["slug"]: team["member_count"] for team in teams}
    assert counts == {populated: 2, empty: 0}


# =============================================================================
# Membership edge cases
# =============================================================================


def test_add_same_member_twice_is_idempotent(client: TestClient) -> None:
    # Given: an agent already in a team
    slug = _create_team(client, display_name=_unique_display_name())["slug"]
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    first = client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    assert first.status_code == 200, first.text
    assert first.json()["added"] is True

    # When: the same agent is added again
    second = client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    # Then: the call succeeds, reports no addition, and preserves joined_at
    assert second.status_code == 200, second.text
    assert second.json()["added"] is False
    assert second.json()["joined_at"] == first.json()["joined_at"]
    assert second.json()["team_id"] == first.json()["team_id"]
    assert client.get(f"{_TEAMS_URL}/{slug}").json()["member_count"] == 1


def test_agent_can_belong_to_two_teams(client: TestClient) -> None:
    # Given: two teams and one registered agent
    sales = _create_team(client, display_name="Sales & Outreach")["slug"]
    engineering = _create_team(client, display_name="Engineering")["slug"]
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    # When: the agent joins both
    assert client.post(f"{_TEAMS_URL}/{sales}/members/{agent_name}").status_code == 200
    assert (
        client.post(f"{_TEAMS_URL}/{engineering}/members/{agent_name}").status_code
        == 200
    )

    # Then: both teams list it, and leaving one leaves the other intact
    for slug in (sales, engineering):
        team = client.get(f"{_TEAMS_URL}/{slug}").json()
        assert [member["agent_name"] for member in team["members"]] == [agent_name]

    assert client.delete(f"{_TEAMS_URL}/{sales}/members/{agent_name}").status_code == 200
    assert client.get(f"{_TEAMS_URL}/{sales}").json()["members"] == []
    assert client.get(f"{_TEAMS_URL}/{engineering}").json()["member_count"] == 1


def test_add_unregistered_agent_returns_404(client: TestClient) -> None:
    slug = _create_team(client, display_name=_unique_display_name())["slug"]

    resp = client.post(f"{_TEAMS_URL}/{slug}/members/{_agent_name()}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "AGENT_NOT_FOUND"


# =============================================================================
# Conflicts
# =============================================================================


def test_delete_team_with_members_returns_409(client: TestClient) -> None:
    # Given: a team with one member
    slug = _create_team(client, display_name=_unique_display_name())["slug"]
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    # When: the team is deleted without force
    resp = client.delete(f"{_TEAMS_URL}/{slug}")

    # Then: the request conflicts and nothing is removed
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "TEAM_HAS_MEMBERS"
    assert client.get(f"{_TEAMS_URL}/{slug}").json()["member_count"] == 1


def test_force_delete_removes_team_and_memberships(client: TestClient) -> None:
    # Given: a team with two members
    slug = _create_team(client, display_name=_unique_display_name())["slug"]
    agent_names = [_agent_name() for _ in range(2)]
    for agent_name in agent_names:
        _register_agent(client, agent_name)
        client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    # When: the team is deleted with force
    resp = client.delete(f"{_TEAMS_URL}/{slug}", params={"force": "true"})

    # Then: the memberships go with it and the agents survive
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "removed_member_count": 2}
    assert client.get(f"{_TEAMS_URL}/{slug}").status_code == 404
    for agent_name in agent_names:
        assert client.get(f"/api/v1/agents/{agent_name}").status_code == 200


def test_force_delete_leaves_no_orphan_membership_rows(
    client: TestClient, db_engine: Engine
) -> None:
    """The membership rows go via ON DELETE CASCADE, not just from the response."""
    slug = _create_team(client, display_name=_unique_display_name())["slug"]
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    with db_engine.begin() as conn:
        before = conn.execute(text("SELECT count(*) FROM team_members")).scalar_one()
    assert before == 1

    assert (
        client.delete(f"{_TEAMS_URL}/{slug}", params={"force": "true"}).status_code
        == 200
    )

    with db_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM team_members")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM teams")).scalar_one() == 0


def test_reusing_a_slug_updates_rather_than_duplicating(client: TestClient) -> None:
    """PUT is an upsert, so a repeated slug is a 200 update, never a second row."""
    first = _create_team(client, display_name="Operations")
    assert first["created"] is True

    second = client.put(_TEAMS_URL, json={"display_name": "Operations"})

    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["team_id"] == first["team_id"]
    listing = client.get(_TEAMS_URL).json()
    assert listing["pagination"]["total"] == 1
    assert [team["slug"] for team in listing["teams"]] == ["operations"]


# =============================================================================
# Validation failures
# =============================================================================


def test_upsert_with_unslugifiable_display_name_returns_422(
    client: TestClient,
) -> None:
    resp = client.put(_TEAMS_URL, json={"display_name": "***"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
    assert client.get(_TEAMS_URL).json()["teams"] == []


def test_upsert_with_malformed_explicit_slug_returns_422(client: TestClient) -> None:
    resp = client.put(
        _TEAMS_URL, json={"display_name": "Engineering", "slug": "Not A Slug"}
    )

    assert resp.status_code == 422, resp.text


def test_upsert_with_empty_display_name_returns_422(client: TestClient) -> None:
    resp = client.put(_TEAMS_URL, json={"display_name": ""})

    assert resp.status_code == 422, resp.text


def test_patch_cannot_change_slug(client: TestClient) -> None:
    # Given: an existing team
    slug = _create_team(client, display_name="Engineering")["slug"]

    # When: a patch tries to rename the slug alongside a legitimate change
    resp = client.patch(
        f"{_TEAMS_URL}/{slug}",
        json={"slug": "platform", "display_name": "Platform"},
    )

    # Then: the request is rejected and neither field changed
    assert resp.status_code == 422, resp.text
    assert client.get(f"{_TEAMS_URL}/platform").status_code == 404
    team = client.get(f"{_TEAMS_URL}/{slug}").json()
    assert team["slug"] == "engineering"
    assert team["display_name"] == "Engineering"


def test_add_member_with_too_short_agent_name_returns_422(client: TestClient) -> None:
    slug = _create_team(client, display_name=_unique_display_name())["slug"]

    resp = client.post(f"{_TEAMS_URL}/{slug}/members/short")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_remove_member_with_too_short_agent_name_returns_422(
    client: TestClient,
) -> None:
    slug = _create_team(client, display_name=_unique_display_name())["slug"]

    resp = client.delete(f"{_TEAMS_URL}/{slug}/members/short")

    assert resp.status_code == 422, resp.text


def test_list_teams_with_malformed_cursor_returns_400(client: TestClient) -> None:
    resp = client.get(_TEAMS_URL, params={"cursor": "not-a-cursor"})

    assert resp.status_code == 400, resp.text


def test_list_teams_with_out_of_range_limit_returns_422(client: TestClient) -> None:
    assert client.get(_TEAMS_URL, params={"limit": 0}).status_code == 422
    assert client.get(_TEAMS_URL, params={"limit": 101}).status_code == 422


# =============================================================================
# Not found
# =============================================================================


def test_unknown_team_returns_404_on_every_route(client: TestClient) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    missing = "no-such-team"

    responses = {
        "get": client.get(f"{_TEAMS_URL}/{missing}"),
        "patch": client.patch(f"{_TEAMS_URL}/{missing}", json={"display_name": "X"}),
        "delete": client.delete(f"{_TEAMS_URL}/{missing}"),
        "add_member": client.post(f"{_TEAMS_URL}/{missing}/members/{agent_name}"),
        "remove_member": client.delete(f"{_TEAMS_URL}/{missing}/members/{agent_name}"),
    }

    for route, resp in responses.items():
        assert resp.status_code == 404, f"{route}: {resp.text}"
        assert resp.json()["error_code"] == "TEAM_NOT_FOUND", route


# =============================================================================
# Authorization tiers
# =============================================================================


def test_authenticated_non_admin_can_read_teams(
    non_admin_client: TestClient, client: TestClient
) -> None:
    slug = _create_team(client, display_name="Marketing")["slug"]

    assert non_admin_client.get(_TEAMS_URL).status_code == 200
    detail = non_admin_client.get(f"{_TEAMS_URL}/{slug}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["slug"] == "marketing"


def test_authenticated_non_admin_cannot_write_teams(
    non_admin_client: TestClient, client: TestClient
) -> None:
    # Given: an existing team and agent created by an admin
    slug = _create_team(client, display_name="Marketing")["slug"]
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    # When/Then: every write route rejects the non-admin caller
    assert non_admin_client.put(_TEAMS_URL, json={"display_name": "Ops"}).status_code == 403
    assert (
        non_admin_client.patch(
            f"{_TEAMS_URL}/{slug}", json={"display_name": "Growth"}
        ).status_code
        == 403
    )
    assert non_admin_client.delete(f"{_TEAMS_URL}/{slug}").status_code == 403
    assert (
        non_admin_client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}").status_code
        == 403
    )
    assert (
        non_admin_client.delete(f"{_TEAMS_URL}/{slug}/members/{agent_name}").status_code
        == 403
    )

    # And: nothing changed
    team = client.get(f"{_TEAMS_URL}/{slug}").json()
    assert team["display_name"] == "Marketing"
    assert team["members"] == []


def test_unauthenticated_client_is_rejected(unauthenticated_client: TestClient) -> None:
    assert unauthenticated_client.get(_TEAMS_URL).status_code == 401
    assert (
        unauthenticated_client.put(
            _TEAMS_URL, json={"display_name": "Engineering"}
        ).status_code
        == 401
    )


# =============================================================================
# Namespace isolation
# =============================================================================


def test_team_in_one_namespace_is_invisible_from_another(app: FastAPI) -> None:
    # Given: a team with a member in namespace A
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_name = _agent_name()
    _register_agent(ns_a, agent_name)
    slug = _create_team(ns_a, display_name="Sales & Outreach")["slug"]
    assert ns_a.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}").status_code == 200

    # When/Then: namespace B cannot see or touch it
    assert ns_b.get(_TEAMS_URL).json()["teams"] == []
    assert ns_b.get(_TEAMS_URL).json()["pagination"]["total"] == 0
    assert ns_b.get(f"{_TEAMS_URL}/{slug}").status_code == 404
    assert (
        ns_b.patch(f"{_TEAMS_URL}/{slug}", json={"display_name": "Hijack"}).status_code
        == 404
    )
    assert ns_b.delete(f"{_TEAMS_URL}/{slug}").status_code == 404
    assert (
        ns_b.delete(f"{_TEAMS_URL}/{slug}", params={"force": "true"}).status_code == 404
    )
    assert ns_b.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}").status_code == 404
    assert ns_b.delete(f"{_TEAMS_URL}/{slug}/members/{agent_name}").status_code == 404

    # And: namespace A is untouched
    team = ns_a.get(f"{_TEAMS_URL}/{slug}").json()
    assert team["display_name"] == "Sales & Outreach"
    assert team["member_count"] == 1


def test_same_slug_in_two_namespaces_are_distinct_teams(app: FastAPI) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    a_team = _create_team(ns_a, display_name="Operations", description="A side")
    b_team = _create_team(ns_b, display_name="Operations", description="B side")

    assert a_team["slug"] == b_team["slug"] == "operations"
    assert a_team["created"] is True
    assert b_team["created"] is True
    assert a_team["team_id"] != b_team["team_id"]

    from_a = ns_a.get(f"{_TEAMS_URL}/operations").json()
    from_b = ns_b.get(f"{_TEAMS_URL}/operations").json()
    assert from_a["namespace_key"] == "ns-a"
    assert from_b["namespace_key"] == "ns-b"
    assert from_a["description"] == "A side"
    assert from_b["description"] == "B side"


def test_agent_from_another_namespace_cannot_join_a_team(app: FastAPI) -> None:
    # Given: an agent registered only in namespace A and a team in namespace B
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_name = _agent_name()
    _register_agent(ns_a, agent_name)
    slug = _create_team(ns_b, display_name="Engineering")["slug"]

    # When: namespace B tries to add that agent
    resp = ns_b.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    # Then: the agent is not visible in namespace B
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "AGENT_NOT_FOUND"


def test_membership_counts_do_not_leak_across_namespaces(app: FastAPI) -> None:
    # Given: the same slug and the same agent name in both namespaces
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_name = _agent_name()
    _register_agent(ns_a, agent_name)
    _register_agent(ns_b, agent_name)
    _create_team(ns_a, display_name="Marketing")
    _create_team(ns_b, display_name="Marketing")

    # When: only namespace A adds the member
    assert ns_a.post(f"{_TEAMS_URL}/marketing/members/{agent_name}").status_code == 200

    # Then: namespace B's identically named team stays empty
    assert ns_b.get(f"{_TEAMS_URL}/marketing").json()["member_count"] == 0
    assert ns_b.get(f"{_TEAMS_URL}/marketing").json()["members"] == []
    assert ns_a.get(f"{_TEAMS_URL}/marketing").json()["member_count"] == 1
    assert [team["member_count"] for team in ns_b.get(_TEAMS_URL).json()["teams"]] == [0]
