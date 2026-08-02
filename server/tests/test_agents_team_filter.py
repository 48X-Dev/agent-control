"""HTTP-level coverage for the ``?team=`` filter on ``GET /api/v1/agents``.

Exercises the filter against real Postgres through ``TestClient``: membership
scoping, intersection with ``name``, behaviour across a page boundary, both
authorization tiers, and namespace isolation.

The isolation cases are the point of this file. The filter reaches ``agents``
through ``team_members`` and ``teams``, so a dropped ``namespace_key`` predicate
on any of the three would surface another tenant's agents through a listing that
looks perfectly ordinary.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_models.teams import TEAM_SLUG_MAX_LENGTH
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_control_server.auth_framework import Operation, Principal, set_authorizer

_AGENTS_URL = "/api/v1/agents"
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


def _agent_name(infix: str = "") -> str:
    """Return a name long enough to satisfy agent-name normalization."""
    prefix = f"agent-{infix}-" if infix else "agent-"
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _register_agent(client: TestClient, agent_name: str) -> None:
    resp = client.post(
        f"{_AGENTS_URL}/initAgent",
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


def _create_team(client: TestClient, display_name: str) -> str:
    resp = client.put(_TEAMS_URL, json={"display_name": display_name})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["slug"])


def _add_member(client: TestClient, slug: str, agent_name: str) -> dict[str, Any]:
    resp = client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _list_agents(client: TestClient, **params: Any) -> dict[str, Any]:
    resp = client.get(_AGENTS_URL, params=params)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _names(body: dict[str, Any]) -> list[str]:
    return [agent["agent_name"] for agent in body["agents"]]


def _unique_display_name(prefix: str = "Team") -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


# =============================================================================
# Happy path
# =============================================================================


def test_team_filter_returns_only_members(client: TestClient) -> None:
    # Given: two agents in a team and one registered outside it
    member_one = _agent_name()
    member_two = _agent_name()
    outsider = _agent_name()
    for name in (member_one, member_two, outsider):
        _register_agent(client, name)

    slug = _create_team(client, _unique_display_name("Operations"))
    _add_member(client, slug, member_one)
    _add_member(client, slug, member_two)

    # When: the listing is filtered by that team's slug
    filtered = _list_agents(client, team=slug)

    # Then: only members come back, and total agrees with the rows returned
    assert sorted(_names(filtered)) == sorted([member_one, member_two])
    assert filtered["pagination"]["total"] == 2
    assert filtered["pagination"]["has_more"] is False
    assert filtered["pagination"]["next_cursor"] is None

    # And: the unfiltered listing still shows the outsider
    assert outsider in _names(_list_agents(client))
    assert _list_agents(client)["pagination"]["total"] == 3


def test_team_filter_matches_slug_not_display_name(client: TestClient) -> None:
    # Given: a team whose display name differs from its derived slug
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = client.put(_TEAMS_URL, json={"display_name": "Sales & Outreach"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "sales-outreach"
    _add_member(client, "sales-outreach", agent_name)

    # When/Then: the slug matches
    assert _names(_list_agents(client, team="sales-outreach")) == [agent_name]

    # And: the display name does not, because matching is literal rather than
    # slugified. Silently coercing it would make the parameter ambiguous.
    for not_a_slug in ("Sales & Outreach", "Sales-Outreach", "sales outreach"):
        body = _list_agents(client, team=not_a_slug)
        assert body["agents"] == [], not_a_slug
        assert body["pagination"]["total"] == 0, not_a_slug


def test_agent_in_two_teams_appears_under_both_slugs(client: TestClient) -> None:
    # Given: one agent that belongs to two teams
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    first = _create_team(client, _unique_display_name("Marketing"))
    second = _create_team(client, _unique_display_name("Engineering"))
    _add_member(client, first, agent_name)
    _add_member(client, second, agent_name)

    # When/Then: either slug finds it, exactly once
    for slug in (first, second):
        body = _list_agents(client, team=slug)
        assert _names(body) == [agent_name]
        assert body["pagination"]["total"] == 1


def test_repeated_membership_add_does_not_duplicate_the_agent_row(
    client: TestClient,
) -> None:
    # Given: an agent added to the same team twice. Membership is idempotent by
    # design, so the repeat is a 200 with added=false rather than a 409.
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    slug = _create_team(client, _unique_display_name("Operations"))

    first = _add_member(client, slug, agent_name)
    second = _add_member(client, slug, agent_name)
    assert first["added"] is True
    assert second["added"] is False
    assert second["joined_at"] == first["joined_at"]

    # When/Then: the filter returns one row, not two, and total agrees
    body = _list_agents(client, team=slug)
    assert _names(body) == [agent_name]
    assert body["pagination"]["total"] == 1


def test_removed_member_drops_out_of_the_team_filter(client: TestClient) -> None:
    # Given: a team with one member
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    slug = _create_team(client, _unique_display_name("Operations"))
    _add_member(client, slug, agent_name)
    assert _names(_list_agents(client, team=slug)) == [agent_name]

    # When: the membership is removed
    remove = client.delete(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    assert remove.status_code == 200, remove.text
    assert remove.json()["removed"] is True

    # Then: the filter no longer matches, but the agent itself still exists
    empty = _list_agents(client, team=slug)
    assert empty["agents"] == []
    assert empty["pagination"]["total"] == 0
    assert agent_name in _names(_list_agents(client))


def test_deleting_a_team_empties_its_filter(client: TestClient) -> None:
    # Given: a team with a member
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    slug = _create_team(client, _unique_display_name("Marketing"))
    _add_member(client, slug, agent_name)

    # When: the team is force-deleted
    deleted = client.delete(f"{_TEAMS_URL}/{slug}", params={"force": "true"})
    assert deleted.status_code == 200, deleted.text

    # Then: the slug now behaves like any unknown slug, and the agent survives
    body = _list_agents(client, team=slug)
    assert body["agents"] == []
    assert body["pagination"]["total"] == 0
    assert agent_name in _names(_list_agents(client))


# =============================================================================
# Unknown slug and validation failures
# =============================================================================


def test_unknown_team_slug_returns_an_empty_page_not_404(client: TestClient) -> None:
    # Given: a registered agent and no team at all
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    # When: the listing is filtered by a slug that names no team
    body = _list_agents(client, team="no-such-team")

    # Then: an empty page rather than an error
    assert body["agents"] == []
    assert body["pagination"]["total"] == 0
    assert body["pagination"]["has_more"] is False
    assert body["pagination"]["next_cursor"] is None

    # And: the team resource itself still 404s for the same slug, so the empty
    # page is a property of the filter and not of missing teams generally.
    assert client.get(f"{_TEAMS_URL}/no-such-team").status_code == 404


def test_empty_team_param_is_rejected(client: TestClient) -> None:
    # Given/When: an explicitly empty team parameter
    resp = client.get(_AGENTS_URL, params={"team": ""})

    # Then: a validation error, not a silently ignored filter
    assert resp.status_code == 422, resp.text


def test_overlong_team_param_is_rejected(client: TestClient) -> None:
    resp = client.get(_AGENTS_URL, params={"team": "a" * (TEAM_SLUG_MAX_LENGTH + 1)})
    assert resp.status_code == 422, resp.text


def test_team_param_at_max_length_is_accepted(client: TestClient) -> None:
    # The boundary itself is valid; it simply matches no team.
    body = _list_agents(client, team="a" * TEAM_SLUG_MAX_LENGTH)
    assert body["agents"] == []
    assert body["pagination"]["total"] == 0


def test_omitting_team_param_lists_every_agent(client: TestClient) -> None:
    # Given: one agent in a team and one outside it
    member = _agent_name()
    outsider = _agent_name()
    _register_agent(client, member)
    _register_agent(client, outsider)
    slug = _create_team(client, _unique_display_name("Engineering"))
    _add_member(client, slug, member)

    # When/Then: no team parameter means no membership restriction
    body = _list_agents(client)
    assert sorted(_names(body)) == sorted([member, outsider])
    assert body["pagination"]["total"] == 2


# =============================================================================
# Combination with the name filter
# =============================================================================


def test_team_filter_intersects_with_name_filter(client: TestClient) -> None:
    # Given: members and non-members that share a name fragment
    member_alpha = _agent_name("alpha")
    member_beta = _agent_name("beta")
    outsider_alpha = _agent_name("alpha")
    for name in (member_alpha, member_beta, outsider_alpha):
        _register_agent(client, name)

    slug = _create_team(client, _unique_display_name("Sales"))
    _add_member(client, slug, member_alpha)
    _add_member(client, slug, member_beta)

    # When: both filters are supplied
    both = _list_agents(client, team=slug, name="alpha")

    # Then: the result is the intersection, and total reflects both filters
    assert _names(both) == [member_alpha]
    assert both["pagination"]["total"] == 1

    # And: each filter alone is strictly wider
    assert sorted(_names(_list_agents(client, team=slug))) == sorted([member_alpha, member_beta])
    assert sorted(_names(_list_agents(client, name="alpha"))) == sorted(
        [member_alpha, outsider_alpha]
    )


def test_name_filter_matching_only_non_members_yields_empty_page(
    client: TestClient,
) -> None:
    # Given: a team whose members share no fragment with the outsider
    member = _agent_name("inside")
    outsider = _agent_name("outside")
    _register_agent(client, member)
    _register_agent(client, outsider)
    slug = _create_team(client, _unique_display_name("Operations"))
    _add_member(client, slug, member)

    # When/Then: the intersection is empty even though each filter alone matches
    body = _list_agents(client, team=slug, name="outside")
    assert body["agents"] == []
    assert body["pagination"]["total"] == 0
    assert _names(_list_agents(client, name="outside")) == [outsider]


# =============================================================================
# Pagination
# =============================================================================


@pytest.mark.parametrize("limit", [1, 2])
def test_team_filter_paginates_across_a_page_boundary(client: TestClient, limit: int) -> None:
    # Given: three members interleaved with two non-members, so a leaking page
    # query would pull an outsider into one of the pages
    members = [_agent_name() for _ in range(3)]
    outsiders = [_agent_name() for _ in range(2)]
    slug = _create_team(client, _unique_display_name("Engineering"))
    for index, member in enumerate(members):
        _register_agent(client, member)
        _add_member(client, slug, member)
        if index < len(outsiders):
            _register_agent(client, outsiders[index])

    # When: the filtered listing is walked page by page
    seen: list[str] = []
    pages = 0
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"team": slug, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        body = _list_agents(client, **params)
        pages += 1
        assert body["pagination"]["total"] == 3
        assert len(body["agents"]) <= limit
        seen.extend(_names(body))
        cursor = body["pagination"]["next_cursor"]
        if not body["pagination"]["has_more"]:
            assert cursor is None
            break
        assert cursor is not None
        assert pages < 10, "cursor walk did not terminate"

    # Then: every member is seen exactly once and no outsider ever appears
    assert sorted(seen) == sorted(members)
    assert pages > 1, "the walk must actually cross a page boundary"
    assert not set(seen) & set(outsiders)


def test_cursor_from_a_filtered_page_stays_filtered(client: TestClient) -> None:
    # Given: two members and an outsider registered between them
    first = _agent_name()
    outsider = _agent_name()
    second = _agent_name()
    slug = _create_team(client, _unique_display_name("Marketing"))
    _register_agent(client, first)
    _add_member(client, slug, first)
    _register_agent(client, outsider)
    _register_agent(client, second)
    _add_member(client, slug, second)

    # When: the first filtered page is fetched at limit 1 and its cursor followed
    page_one = _list_agents(client, team=slug, limit=1)
    assert page_one["pagination"]["has_more"] is True
    cursor = page_one["pagination"]["next_cursor"]
    assert cursor is not None
    page_two = _list_agents(client, team=slug, limit=1, cursor=cursor)

    # Then: the second page is still restricted to members
    assert page_two["pagination"]["has_more"] is False
    assert sorted(_names(page_one) + _names(page_two)) == sorted([first, second])
    assert outsider not in _names(page_one) + _names(page_two)


# =============================================================================
# Authorization tiers
# =============================================================================


def test_team_filter_is_readable_by_a_non_admin_credential(
    client: TestClient, non_admin_client: TestClient
) -> None:
    # Given: a team populated with an admin credential
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    slug = _create_team(client, _unique_display_name("Operations"))
    _add_member(client, slug, agent_name)

    # When/Then: reading the filtered listing only needs an authenticated caller
    resp = non_admin_client.get(_AGENTS_URL, params={"team": slug})
    assert resp.status_code == 200, resp.text
    assert _names(resp.json()) == [agent_name]


def test_membership_writes_are_rejected_for_a_non_admin_credential(
    client: TestClient, non_admin_client: TestClient
) -> None:
    # Given: an agent and a team that the non-admin caller can already read
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    slug = _create_team(client, _unique_display_name("Engineering"))

    # When: the non-admin tries to change membership
    add = non_admin_client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    remove = non_admin_client.delete(f"{_TEAMS_URL}/{slug}/members/{agent_name}")

    # Then: both writes are forbidden and the filter is unchanged
    assert add.status_code == 403, add.text
    assert remove.status_code == 403, remove.text
    assert _list_agents(client, team=slug)["agents"] == []


def test_team_filter_rejects_an_unauthenticated_caller(
    unauthenticated_client: TestClient,
) -> None:
    resp = unauthenticated_client.get(_AGENTS_URL, params={"team": "operations"})
    assert resp.status_code == 401, resp.text


# =============================================================================
# Namespace isolation
# =============================================================================


def test_team_filter_does_not_leak_agents_from_another_namespace(
    app: FastAPI,
) -> None:
    # Given: the same team slug in two namespaces, each with its own member
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_a = _agent_name()
    agent_b = _agent_name()
    _register_agent(ns_a, agent_a)
    _register_agent(ns_b, agent_b)
    slug_a = _create_team(ns_a, "Engineering")
    slug_b = _create_team(ns_b, "Engineering")
    assert slug_a == slug_b == "engineering"
    _add_member(ns_a, "engineering", agent_a)
    _add_member(ns_b, "engineering", agent_b)

    # When/Then: each namespace sees only its own member under the shared slug
    from_a = _list_agents(ns_a, team="engineering")
    from_b = _list_agents(ns_b, team="engineering")
    assert _names(from_a) == [agent_a]
    assert _names(from_b) == [agent_b]
    assert from_a["pagination"]["total"] == 1
    assert from_b["pagination"]["total"] == 1


def test_team_filter_is_empty_when_the_membership_lives_in_another_namespace(
    app: FastAPI,
) -> None:
    # Given: one agent name registered in both namespaces and a team of the same
    # slug in both, but a membership only in namespace B. Nothing distinguishes
    # the two rows except namespace_key, so this is the case a dropped predicate
    # would surface.
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_name = _agent_name()
    _register_agent(ns_a, agent_name)
    _register_agent(ns_b, agent_name)
    _create_team(ns_a, "Operations")
    _create_team(ns_b, "Operations")
    _add_member(ns_b, "operations", agent_name)

    # When/Then: namespace A's filter is empty despite the identically named
    # agent and identically slugged team next door
    from_a = _list_agents(ns_a, team="operations")
    assert from_a["agents"] == []
    assert from_a["pagination"]["total"] == 0

    # And: namespace A can still see its own agent unfiltered
    assert _names(_list_agents(ns_a)) == [agent_name]
    assert _names(_list_agents(ns_b, team="operations")) == [agent_name]


def test_team_filter_ignores_a_team_that_exists_only_in_another_namespace(
    app: FastAPI,
) -> None:
    # Given: a team that exists only in namespace B, and an agent in namespace A
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_a = _agent_name()
    agent_b = _agent_name()
    _register_agent(ns_a, agent_a)
    _register_agent(ns_b, agent_b)
    slug = _create_team(ns_b, "Sales & Outreach")
    _add_member(ns_b, slug, agent_b)

    # When/Then: namespace A gets an empty page for a slug it has no team for
    body = _list_agents(ns_a, team=slug)
    assert body["agents"] == []
    assert body["pagination"]["total"] == 0
    assert ns_a.get(f"{_TEAMS_URL}/{slug}").status_code == 404
