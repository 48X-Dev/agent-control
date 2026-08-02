"""HTTP coverage for the ``linear_team_key`` field on ``/teams``.

PUT replaces and PATCH merges, and the two behave differently around an
omitted key; both are pinned here. Normalization, rejection of a malformed key,
and the authorization tier are covered alongside.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

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


def _put(client: TestClient, body: dict[str, Any]) -> Any:
    resp = client.put(_TEAMS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _key_of(client: TestClient, slug: str) -> str | None:
    resp = client.get(f"{_TEAMS_URL}/{slug}")
    assert resp.status_code == 200, resp.text
    return resp.json()["linear_team_key"]


# =============================================================================
# Create and replace
# =============================================================================


def test_create_team_stores_the_linear_key_upper_cased(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "  eng  "})["slug"]

    assert _key_of(client, slug) == "ENG"


def test_create_team_without_a_key_leaves_it_null(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering"})["slug"]

    assert _key_of(client, slug) is None


def test_upsert_omitting_the_key_clears_it(client: TestClient) -> None:
    """PUT replaces, so an omitted key unlinks the team."""
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    _put(client, {"display_name": "Engineering", "slug": slug})

    assert _key_of(client, slug) is None


def test_upsert_with_an_explicit_null_clears_the_key(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    _put(client, {"display_name": "Engineering", "slug": slug, "linear_team_key": None})

    assert _key_of(client, slug) is None


def test_upsert_can_change_the_key(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    _put(client, {"display_name": "Engineering", "slug": slug, "linear_team_key": "PLAT"})

    assert _key_of(client, slug) == "PLAT"


# =============================================================================
# Patch
# =============================================================================


def test_patch_omitting_the_key_leaves_it_alone(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    resp = client.patch(f"{_TEAMS_URL}/{slug}", json={"display_name": "Platform"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["linear_team_key"] == "ENG"
    assert _key_of(client, slug) == "ENG"


def test_patch_with_an_explicit_null_clears_the_key(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    resp = client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["linear_team_key"] is None
    assert _key_of(client, slug) is None


def test_patch_links_an_unlinked_team_and_normalizes_the_key(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering"})["slug"]

    resp = client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": "  eng "})

    assert resp.status_code == 200, resp.text
    assert resp.json()["linear_team_key"] == "ENG"
    assert _key_of(client, slug) == "ENG"


def test_patch_leaves_the_description_alone_when_only_the_key_changes(
    client: TestClient,
) -> None:
    slug = _put(
        client, {"display_name": "Engineering", "description": "Builds things"}
    )["slug"]

    client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": "ENG"})

    team = client.get(f"{_TEAMS_URL}/{slug}").json()
    assert team["description"] == "Builds things"
    assert team["linear_team_key"] == "ENG"


# =============================================================================
# Validation
# =============================================================================


def test_patch_with_a_malformed_key_returns_422_without_echoing_it(
    client: TestClient,
) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]
    malformed = "eng-team-secret"

    resp = client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": malformed})

    assert resp.status_code == 422, resp.text
    assert malformed not in resp.text
    assert _key_of(client, slug) == "ENG"


def test_upsert_with_a_malformed_key_returns_422(client: TestClient) -> None:
    resp = client.put(
        _TEAMS_URL, json={"display_name": "Engineering", "linear_team_key": "ENG TEAM"}
    )

    assert resp.status_code == 422, resp.text


def test_upsert_with_an_over_long_key_returns_422(client: TestClient) -> None:
    resp = client.put(
        _TEAMS_URL, json={"display_name": "Engineering", "linear_team_key": "A" * 21}
    )

    assert resp.status_code == 422, resp.text


def test_upsert_with_an_empty_key_returns_422(client: TestClient) -> None:
    resp = client.put(
        _TEAMS_URL, json={"display_name": "Engineering", "linear_team_key": ""}
    )

    assert resp.status_code == 422, resp.text


# =============================================================================
# Read paths, authorization and namespaces
# =============================================================================


def test_list_teams_reports_the_linear_key(client: TestClient) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    teams = client.get(_TEAMS_URL).json()["teams"]

    assert [t["linear_team_key"] for t in teams if t["slug"] == slug] == ["ENG"]


def test_two_teams_may_point_at_the_same_linear_team(client: TestClient) -> None:
    first = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]
    second = _put(client, {"display_name": "Platform", "linear_team_key": "ENG"})["slug"]

    assert first != second
    assert _key_of(client, first) == "ENG"
    assert _key_of(client, second) == "ENG"


def test_non_admin_cannot_set_the_linear_key(
    client: TestClient, non_admin_client: TestClient
) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    resp = non_admin_client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": "PLAT"})

    assert resp.status_code == 403, resp.text
    assert _key_of(client, slug) == "ENG"


def test_non_admin_can_read_the_linear_key(
    client: TestClient, non_admin_client: TestClient
) -> None:
    slug = _put(client, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]

    assert non_admin_client.get(f"{_TEAMS_URL}/{slug}").json()["linear_team_key"] == "ENG"


def test_the_same_linear_key_in_two_namespaces_stays_separate(app: FastAPI) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    first = TestClient(app, headers={"X-Test-Namespace": "ns-one"})
    second = TestClient(app, headers={"X-Test-Namespace": "ns-two"})

    slug = _put(first, {"display_name": "Engineering", "linear_team_key": "ENG"})["slug"]
    _put(second, {"display_name": "Engineering", "linear_team_key": "ENG"})

    second.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": None})

    assert _key_of(first, slug) == "ENG"
    assert _key_of(second, slug) is None
