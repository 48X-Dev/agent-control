"""Server-side guarantees the team detail page depends on.

The detail page issues exactly three reads for a slug: the team itself, the
agents filtered to it, and its Linear milestones. These tests pin the parts of
that contract the page would break on -- the fields each panel renders, a 404
shaped so the client can tell "no such team" from "the server fell over", the
independence of the agent list from Linear, and the write the link form makes.

Nothing here calls Linear. The milestone service is substituted with a scripted
stand-in, and a sentinel string stands in for a credential so the assertions
that it never escapes are meaningful.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_control_server.services.linear_client import LinearError, LinearMilestone
from agent_control_server.services.linear_milestones import (
    LinearMilestoneService,
    get_milestone_service,
)

_TEAMS_URL = "/api/v1/teams"
_AGENTS_URL = "/api/v1/agents"

SENTINEL_KEY = "lin_api_DETAILCONTRACTSENTINEL987"

# Fields the header, the agent panel and a milestone row read. A rename on the
# server should fail here rather than as a blank pane in the browser.
_HEADER_FIELDS = frozenset({"slug", "display_name", "description", "member_count"})
_AGENT_ROW_FIELDS = frozenset({"agent_name", "active_controls_count"})
_MILESTONE_FIELDS = frozenset(
    {
        "id",
        "name",
        "target_date",
        "status",
        "progress",
        "project_name",
        "project_url",
    }
)
# Every documented value of `status`. The page switches on this exhaustively,
# so a sixth value appearing without a UI branch would render nothing at all.
_MILESTONE_STATES = frozenset({"not_configured", "not_linked", "error", "empty", "ok"})


class ScriptedLinearClient:
    """Replays one scripted result. Records calls; never touches the network."""

    def __init__(self, result: list[LinearMilestone] | Exception) -> None:
        self._result = result
        self.calls: list[str] = []

    async def fetch_milestones(self, team_key: str) -> list[LinearMilestone]:
        self.calls.append(team_key)
        if isinstance(self._result, Exception):
            raise self._result
        return list(self._result)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def use_service(app: FastAPI) -> Iterator[Callable[[LinearMilestoneService], None]]:
    """Swap the milestone service for the duration of one test."""

    def _install(service: LinearMilestoneService) -> None:
        app.dependency_overrides[get_milestone_service] = lambda: service

    yield _install
    app.dependency_overrides.pop(get_milestone_service, None)


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
    display_name: str = "Engineering",
    description: str | None = None,
    linear_team_key: str | None = None,
) -> str:
    body: dict[str, Any] = {"display_name": display_name}
    if description is not None:
        body["description"] = description
    if linear_team_key is not None:
        body["linear_team_key"] = linear_team_key
    resp = client.put(_TEAMS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["slug"])


def _add_member(client: TestClient, slug: str, agent_name: str) -> None:
    _register_agent(client, agent_name)
    resp = client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    assert resp.status_code == 200, resp.text


def _milestone(identifier: str = "m1", name: str = "Beta") -> LinearMilestone:
    return LinearMilestone(
        id=identifier,
        name=name,
        description="Ship the beta",
        target_date=dt.date(2026, 9, 1),
        status="unstarted",
        progress=0.25,
        project_id="p1",
        project_name="Platform",
        project_url="https://linear.app/acme/project/platform",
    )


def _service(result: list[LinearMilestone] | Exception) -> LinearMilestoneService:
    return LinearMilestoneService(client=ScriptedLinearClient(result))


def _unconfigured_service() -> LinearMilestoneService:
    """A server with no Linear credential: the client is never built."""
    return LinearMilestoneService(client=None)


# =============================================================================
# The three reads the page makes
# =============================================================================


def test_a_read_only_caller_can_load_every_panel(
    non_admin_client: TestClient, client: TestClient, use_service
) -> None:
    # Given: a populated, linked team created by an admin
    use_service(_service([_milestone()]))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")
    agent_name = _agent_name()
    _add_member(client, slug, agent_name)

    # When: a non-admin makes exactly the three calls the detail page makes
    team = non_admin_client.get(f"{_TEAMS_URL}/{slug}")
    agents = non_admin_client.get(_AGENTS_URL, params={"team": slug, "limit": 20})
    milestones = non_admin_client.get(f"{_TEAMS_URL}/{slug}/milestones")

    # Then: all three answer with the data their panels need
    assert team.status_code == 200, team.text
    assert team.json()["display_name"] == "Engineering"
    assert agents.status_code == 200, agents.text
    assert [a["agent_name"] for a in agents.json()["agents"]] == [agent_name]
    assert milestones.status_code == 200, milestones.text
    assert milestones.json()["status"] == "ok"


def test_header_payload_carries_every_field_it_renders(client: TestClient) -> None:
    slug = _create_team(
        client,
        display_name="Sales & Outreach",
        description="Owns pipeline and prospecting.",
    )
    _add_member(client, slug, _agent_name())

    body = client.get(f"{_TEAMS_URL}/{slug}").json()

    assert _HEADER_FIELDS <= set(body)
    assert body["slug"] == "sales-outreach"
    assert body["display_name"] == "Sales & Outreach"
    assert body["description"] == "Owns pipeline and prospecting."
    assert body["member_count"] == 1


def test_a_team_without_a_description_returns_null_rather_than_omitting_it(
    client: TestClient,
) -> None:
    # The header skips the description line on null; a missing key would be an
    # undefined read instead.
    slug = _create_team(client, display_name="Operations")

    body = client.get(f"{_TEAMS_URL}/{slug}").json()

    assert "description" in body
    assert body["description"] is None


def test_agent_rows_carry_the_fields_each_row_renders(client: TestClient) -> None:
    slug = _create_team(client, display_name="Engineering")
    agent_name = _agent_name()
    _add_member(client, slug, agent_name)

    row = client.get(_AGENTS_URL, params={"team": slug}).json()["agents"][0]

    assert _AGENT_ROW_FIELDS <= set(row)
    assert row["agent_name"] == agent_name
    # The badge reads this even when nothing is attached, so it must be a
    # number rather than null.
    assert row["active_controls_count"] == 0


# =============================================================================
# An unknown slug
# =============================================================================


def test_unknown_slug_is_a_404_carrying_its_status_in_the_body(
    client: TestClient,
) -> None:
    # The client reads the status off the parsed body, not off the Response, so
    # a 404 without it would fall through to the generic error state.
    resp = client.get(f"{_TEAMS_URL}/no-such-team")

    assert resp.status_code == 404
    assert resp.json()["status"] == 404


def test_milestones_of_an_unknown_slug_are_a_404_not_a_not_linked_200(
    client: TestClient, use_service
) -> None:
    # Given: a working Linear
    use_service(_service([_milestone()]))

    # When: the milestone panel asks about a team that does not exist
    resp = client.get(f"{_TEAMS_URL}/no-such-team/milestones")

    # Then: it 404s rather than answering with one of the five states, so the
    # page renders "team not found" instead of an empty milestone panel.
    assert resp.status_code == 404
    assert resp.json()["status"] == 404


def test_agents_of_an_unknown_slug_are_an_empty_page_not_a_404(
    client: TestClient,
) -> None:
    # The agent panel would otherwise show its error alert alongside the
    # not-found page. An unknown slug simply matches nobody.
    resp = client.get(_AGENTS_URL, params={"team": "no-such-team"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["agents"] == []


def test_an_unauthenticated_page_load_is_rejected_on_all_three_reads(
    unauthenticated_client: TestClient, client: TestClient, use_service
) -> None:
    # The page must not partially render for a signed-out visitor.
    use_service(_service([_milestone()]))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")

    assert unauthenticated_client.get(f"{_TEAMS_URL}/{slug}").status_code == 401
    assert unauthenticated_client.get(_AGENTS_URL, params={"team": slug}).status_code == 401
    assert unauthenticated_client.get(f"{_TEAMS_URL}/{slug}/milestones").status_code == 401


# =============================================================================
# Linear cannot take the agent panel down
# =============================================================================


def test_an_unreachable_linear_leaves_the_agent_read_untouched(
    client: TestClient, use_service
) -> None:
    # Given: a linked team whose Linear is refusing connections
    use_service(_service(LinearError("unreachable")))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")
    agent_name = _agent_name()
    _add_member(client, slug, agent_name)

    # When: the page makes both reads
    milestones = client.get(f"{_TEAMS_URL}/{slug}/milestones")
    agents = client.get(_AGENTS_URL, params={"team": slug})

    # Then: the milestone read reports the failure inside a 200, and the agent
    # read is completely unaffected
    assert milestones.status_code == 200, milestones.text
    assert milestones.json()["status"] == "error"
    assert agents.status_code == 200, agents.text
    assert [a["agent_name"] for a in agents.json()["agents"]] == [agent_name]


def test_a_linear_failure_never_returns_a_5xx(client: TestClient, use_service) -> None:
    # A 5xx would put the whole panel into the query's error branch, which is
    # reserved for Agent Control's own failures.
    use_service(_service(LinearError("boom")))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")

    resp = client.get(f"{_TEAMS_URL}/{slug}/milestones")

    assert resp.status_code == 200, resp.text
    assert resp.json()["milestones"] == []


# =============================================================================
# The five milestone states, as the page's switch sees them
# =============================================================================


def test_every_state_answers_with_a_known_status_and_a_list(
    client: TestClient, use_service
) -> None:
    # Given: one linked team and one unlinked team
    use_service(_service([]))
    unlinked = _create_team(client, display_name="Marketing")
    linked = _create_team(client, display_name="Operations", linear_team_key="OPS")

    seen: dict[str, str] = {}

    def record(slug: str) -> dict[str, Any]:
        resp = client.get(f"{_TEAMS_URL}/{slug}/milestones")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] in _MILESTONE_STATES, body["status"]
        assert isinstance(body["milestones"], list)
        seen[body["status"]] = slug
        return body

    # not_linked and empty, from the service that returns nothing
    assert record(unlinked)["status"] == "not_linked"
    assert record(linked)["status"] == "empty"

    # ok
    use_service(_service([_milestone()]))
    ok_body = record(linked)
    assert ok_body["status"] == "ok"
    assert len(ok_body["milestones"]) == 1

    # error, from a Linear that refuses
    use_service(_service(LinearError("down")))
    error_slug = _create_team(client, display_name="Support", linear_team_key="SUP")
    assert record(error_slug)["status"] == "error"

    # not_configured, from a server holding no Linear credential
    use_service(_unconfigured_service())
    assert record(unlinked)["status"] == "not_configured"

    # Then: every branch the page switches on is reachable, and each answered
    # with a `milestones` list rather than a null the UI would crash mapping.
    assert set(seen) == _MILESTONE_STATES


def test_a_milestone_row_carries_every_field_it_renders(client: TestClient, use_service) -> None:
    use_service(_service([_milestone()]))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")

    row = client.get(f"{_TEAMS_URL}/{slug}/milestones").json()["milestones"][0]

    assert _MILESTONE_FIELDS <= set(row)
    assert row["name"] == "Beta"
    assert row["target_date"] == "2026-09-01"
    assert row["progress"] == 0.25
    assert row["project_url"] == "https://linear.app/acme/project/platform"


def test_a_milestone_with_no_date_or_progress_returns_nulls_not_omissions(
    client: TestClient, use_service
) -> None:
    # The row renders "No target date" on null and hides the progress bar; a
    # missing key would be an undefined read in both places.
    bare = LinearMilestone(
        id="m2",
        name="Untargeted",
        description=None,
        target_date=None,
        status=None,
        progress=None,
        project_id=None,
        project_name=None,
        project_url=None,
    )
    use_service(_service([bare]))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")

    row = client.get(f"{_TEAMS_URL}/{slug}/milestones").json()["milestones"][0]

    assert row["target_date"] is None
    assert row["progress"] is None
    assert row["project_name"] is None


def test_the_linked_key_comes_back_for_the_panel_badge(client: TestClient, use_service) -> None:
    use_service(_service([_milestone()]))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")

    body = client.get(f"{_TEAMS_URL}/{slug}/milestones").json()

    assert body["linear_team_key"] == "ENG"
    assert body["slug"] == slug


def test_an_unlinked_team_reports_a_null_key_rather_than_omitting_it(
    client: TestClient, use_service
) -> None:
    use_service(_service([]))
    slug = _create_team(client, display_name="Marketing")

    body = client.get(f"{_TEAMS_URL}/{slug}/milestones").json()

    assert body["status"] == "not_linked"
    assert body["linear_team_key"] is None
    assert "cached" in body


# =============================================================================
# A team with no agents
# =============================================================================


def test_an_empty_team_reads_back_as_a_200_with_no_agents(client: TestClient) -> None:
    slug = _create_team(client, display_name="Marketing")

    team = client.get(f"{_TEAMS_URL}/{slug}")
    agents = client.get(_AGENTS_URL, params={"team": slug})

    assert team.status_code == 200, team.text
    assert team.json()["member_count"] == 0
    assert team.json()["members"] == []
    assert agents.status_code == 200, agents.text
    assert agents.json()["agents"] == []
    assert agents.json()["pagination"]["has_more"] is False


def test_an_empty_linked_team_still_answers_its_milestones(client: TestClient, use_service) -> None:
    # Having no agents says nothing about having milestones; the two panels
    # must not be coupled.
    use_service(_service([_milestone()]))
    slug = _create_team(client, display_name="Marketing", linear_team_key="MKT")

    agents = client.get(_AGENTS_URL, params={"team": slug})
    milestones = client.get(f"{_TEAMS_URL}/{slug}/milestones")

    assert agents.json()["agents"] == []
    assert milestones.json()["status"] == "ok"


# =============================================================================
# The link form's write
# =============================================================================


def test_the_link_form_write_links_the_team_and_reports_the_new_key(
    client: TestClient, use_service
) -> None:
    use_service(_service([_milestone()]))
    slug = _create_team(client, display_name="Marketing")
    assert client.get(f"{_TEAMS_URL}/{slug}/milestones").json()["status"] == "not_linked"

    # When: the form submits exactly the body it sends
    patched = client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": "MKT"})

    # Then: the write succeeds and the refetched panel is linked
    assert patched.status_code == 200, patched.text
    assert patched.json()["linear_team_key"] == "MKT"
    assert client.get(f"{_TEAMS_URL}/{slug}/milestones").json()["status"] == "ok"


def test_a_non_admin_link_attempt_is_a_403_the_form_can_name(
    non_admin_client: TestClient, client: TestClient
) -> None:
    # The form prints a "needs an admin API key" message on exactly 403.
    slug = _create_team(client, display_name="Marketing")

    resp = non_admin_client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": "MKT"})

    assert resp.status_code == 403
    assert client.get(f"{_TEAMS_URL}/{slug}").json()["linear_team_key"] is None


def test_a_key_the_form_would_reject_is_also_rejected_by_the_server(
    client: TestClient,
) -> None:
    # Client-side validation is convenience, not the boundary.
    slug = _create_team(client, display_name="Marketing")

    resp = client.patch(f"{_TEAMS_URL}/{slug}", json={"linear_team_key": "not a key"})

    assert resp.status_code == 422
    assert client.get(f"{_TEAMS_URL}/{slug}").json()["linear_team_key"] is None


# =============================================================================
# Nothing the page receives carries a credential
# =============================================================================


def test_no_response_the_page_reads_contains_the_linear_credential(
    client: TestClient, use_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_control_server.config import linear_settings

    monkeypatch.setattr(linear_settings, "api_key", SecretStr(SENTINEL_KEY))
    use_service(_service([_milestone()]))
    slug = _create_team(client, display_name="Engineering", linear_team_key="ENG")
    _add_member(client, slug, _agent_name())

    bodies = [
        client.get(f"{_TEAMS_URL}/{slug}").text,
        client.get(_AGENTS_URL, params={"team": slug}).text,
        client.get(f"{_TEAMS_URL}/{slug}/milestones").text,
        client.get(_TEAMS_URL).text,
        client.get("/openapi.json").text,
    ]

    for body in bodies:
        assert SENTINEL_KEY not in body
        assert "lin_api_" not in body
