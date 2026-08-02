"""HTTP-level coverage for ``GET /observability/traces/{trace_id}``.

Exercises the route against real Postgres through ``TestClient``: hop ordering
and team labelling, the truncation contract, both authorization tiers, and
namespace isolation for the events and the team lookup alike.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_control_models.actions import ActionDecision
from agent_control_models.errors import ErrorCode
from agent_control_models.observability import BatchEventsRequest, ControlExecutionEvent
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.errors import ForbiddenError

_TRACES_URL = "/api/v1/observability/traces"
_EVENTS_URL = "/api/v1/observability/events"
_TEAMS_URL = "/api/v1/teams"

_BASE_TIME = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


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


class ReadDeniedAuthorizer:
    """Authorizer that refuses only ``OBSERVABILITY_READ``."""

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del request, context
        if operation is Operation.OBSERVABILITY_READ:
            raise ForbiddenError(
                error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
                detail="trace read denied",
            )
        return Principal(namespace_key="default", is_admin=True)


def _namespace_client(app: FastAPI, namespace_key: str) -> TestClient:
    return TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-Test-Namespace": namespace_key},
    )


def _agent_name() -> str:
    """Return a name long enough to satisfy agent-name normalization."""
    return f"agent-{uuid.uuid4().hex[:12]}"


def _trace_id() -> str:
    return uuid.uuid4().hex


def _event(
    *,
    trace_id: str,
    agent_name: str,
    span_id: str,
    timestamp: datetime,
    control_id: int = 1,
    control_name: str = "pii-check",
    action: ActionDecision = "observe",
    matched: bool = False,
) -> ControlExecutionEvent:
    return ControlExecutionEvent(
        trace_id=trace_id,
        span_id=span_id,
        agent_name=agent_name,
        control_id=control_id,
        control_name=control_name,
        check_stage="pre",
        applies_to="llm_call",
        action=action,
        matched=matched,
        confidence=0.9,
        timestamp=timestamp,
    )


def _ingest(client: TestClient, events: list[ControlExecutionEvent]) -> None:
    resp = client.post(
        _EVENTS_URL,
        json=BatchEventsRequest(events=events).model_dump(mode="json"),
    )
    assert resp.status_code == 202, resp.text


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


def _create_team(client: TestClient, *, display_name: str) -> str:
    resp = client.put(_TEAMS_URL, json={"display_name": display_name})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["slug"])


def _join_team(client: TestClient, *, slug: str, agent_name: str) -> None:
    resp = client.post(f"{_TEAMS_URL}/{slug}/members/{agent_name}")
    assert resp.status_code == 200, resp.text


def _place_on_team(client: TestClient, *, agent_name: str, display_name: str) -> str:
    _register_agent(client, agent_name)
    slug = _create_team(client, display_name=display_name)
    _join_team(client, slug=slug, agent_name=agent_name)
    return slug


# =============================================================================
# Happy path
# =============================================================================


def test_trace_returns_ordered_hops_with_team_labels(
    client: TestClient, setup_observability: object
) -> None:
    # Given: a three-hop trace whose agents sit on two teams and no team
    _ = setup_observability
    seller, engineer, freelancer = _agent_name(), _agent_name(), _agent_name()
    _place_on_team(client, agent_name=seller, display_name="Sales & Outreach")
    _place_on_team(client, agent_name=engineer, display_name="Engineering")
    _register_agent(client, freelancer)

    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=seller,
                span_id="span-01",
                timestamp=_BASE_TIME,
                control_name="outbound-tone",
                action="observe",
            ),
            _event(
                trace_id=trace_id,
                agent_name=engineer,
                span_id="span-02",
                timestamp=_BASE_TIME + timedelta(seconds=1),
                control_id=2,
                control_name="sql-injection",
                action="deny",
                matched=True,
            ),
            _event(
                trace_id=trace_id,
                agent_name=freelancer,
                span_id="span-03",
                timestamp=_BASE_TIME + timedelta(seconds=2),
                control_id=3,
                control_name="pii-check",
            ),
        ],
    )

    # When: the trace is read
    resp = client.get(f"{_TRACES_URL}/{trace_id}")

    # Then: hops come back oldest first, each labelled with its agent's team
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trace_id"] == trace_id
    assert body["hop_count"] == 3
    assert body["total_hop_count"] == 3
    assert body["truncated"] is False
    assert body["out_of_order"] is False
    assert body["limit"] == 200

    hops = body["hops"]
    assert [hop["agent_name"] for hop in hops] == [seller, engineer, freelancer]
    assert [hop["span_id"] for hop in hops] == ["span-01", "span-02", "span-03"]
    assert hops[0]["team"] == {
        "slug": "sales-outreach",
        "display_name": "Sales & Outreach",
    }
    assert hops[1]["team"] == {"slug": "engineering", "display_name": "Engineering"}
    assert hops[2]["team"] is None
    assert hops[1]["control_name"] == "sql-injection"
    assert hops[1]["action"] == "deny"
    assert hops[1]["matched"] is True
    assert all(hop["out_of_order"] is False for hop in hops)


def test_trace_with_exactly_one_hop(
    client: TestClient, setup_observability: object
) -> None:
    _ = setup_observability
    agent_name = _agent_name()
    _place_on_team(client, agent_name=agent_name, display_name="Operations")
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    resp = client.get(f"{_TRACES_URL}/{trace_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hop_count"] == 1
    assert body["total_hop_count"] == 1
    assert body["truncated"] is False
    assert body["out_of_order"] is False
    assert len(body["hops"]) == 1
    assert body["hops"][0]["team"]["slug"] == "operations"


def test_trace_excludes_hops_from_other_traces(
    client: TestClient, setup_observability: object
) -> None:
    # Given: two traces sharing one agent
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    wanted, other = _trace_id(), _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=wanted,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            ),
            _event(
                trace_id=other,
                agent_name=agent_name,
                span_id="span-02",
                timestamp=_BASE_TIME + timedelta(seconds=1),
            ),
            _event(
                trace_id=other,
                agent_name=agent_name,
                span_id="span-03",
                timestamp=_BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    # When/Then: each read is confined to its own trace
    first = client.get(f"{_TRACES_URL}/{wanted}").json()
    assert first["total_hop_count"] == 1
    assert [hop["span_id"] for hop in first["hops"]] == ["span-01"]

    second = client.get(f"{_TRACES_URL}/{other}").json()
    assert second["total_hop_count"] == 2
    assert [hop["span_id"] for hop in second["hops"]] == ["span-02", "span-03"]


def test_hop_from_an_unregistered_agent_is_returned_without_a_team(
    client: TestClient, setup_observability: object
) -> None:
    # Given: events for an agent name that was never registered
    _ = setup_observability
    trace_id = _trace_id()
    agent_name = _agent_name()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    # When/Then: the hop still reads back, unlabelled rather than missing
    resp = client.get(f"{_TRACES_URL}/{trace_id}")
    assert resp.status_code == 200, resp.text
    hop = resp.json()["hops"][0]
    assert hop["agent_name"] == agent_name
    assert hop["team"] is None


def test_team_label_follows_current_membership(
    client: TestClient, setup_observability: object
) -> None:
    # Given: a trace whose agent is on a team when the trace is first read
    _ = setup_observability
    agent_name = _agent_name()
    slug = _place_on_team(client, agent_name=agent_name, display_name="Operations")
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )
    labelled = client.get(f"{_TRACES_URL}/{trace_id}").json()
    assert labelled["hops"][0]["team"]["slug"] == slug

    # When: the agent leaves the team after the events were recorded
    assert client.delete(f"{_TEAMS_URL}/{slug}/members/{agent_name}").status_code == 200

    # Then: the same old trace now reads back unlabelled
    relabelled = client.get(f"{_TRACES_URL}/{trace_id}").json()
    assert relabelled["hops"][0]["team"] is None


def test_naive_client_timestamp_is_returned_as_utc(
    client: TestClient, setup_observability: object
) -> None:
    # Given: an event whose client clock reported no offset
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME.replace(tzinfo=None),
            )
        ],
    )

    # When/Then: the hop reads back with a usable offset instead of erroring
    resp = client.get(f"{_TRACES_URL}/{trace_id}")
    assert resp.status_code == 200, resp.text
    hop_time = datetime.fromisoformat(resp.json()["hops"][0]["timestamp"])
    assert hop_time.utcoffset() is not None


def test_agent_on_two_teams_is_labelled_with_the_first_slug_alphabetically(
    client: TestClient, setup_observability: object
) -> None:
    # Given: an agent that joined Operations after Engineering
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _join_team(
        client,
        slug=_create_team(client, display_name="Operations"),
        agent_name=agent_name,
    )
    _join_team(
        client,
        slug=_create_team(client, display_name="Engineering"),
        agent_name=agent_name,
    )

    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    # When/Then: the label is the alphabetically first slug, not the first joined
    hop = client.get(f"{_TRACES_URL}/{trace_id}").json()["hops"][0]
    assert hop["team"]["slug"] == "engineering"


# =============================================================================
# Ordering and the skew flag
# =============================================================================


def test_identical_timestamps_tiebreak_by_span_id_and_set_the_skew_flag(
    client: TestClient, setup_observability: object
) -> None:
    # Given: two hops the timestamps cannot separate
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-zz",
                timestamp=_BASE_TIME,
            ),
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-aa",
                timestamp=_BASE_TIME,
            ),
        ],
    )

    # When: the trace is read twice
    first = client.get(f"{_TRACES_URL}/{trace_id}").json()
    second = client.get(f"{_TRACES_URL}/{trace_id}").json()

    # Then: span_id settles the order, identically on both reads
    assert [hop["span_id"] for hop in first["hops"]] == ["span-aa", "span-zz"]
    assert [hop["span_id"] for hop in second["hops"]] == ["span-aa", "span-zz"]

    # And: the hop time could not place is flagged, and the response reports it
    assert first["hops"][0]["out_of_order"] is False
    assert first["hops"][1]["out_of_order"] is True
    assert first["out_of_order"] is True


def test_events_emitted_newest_first_come_back_oldest_first(
    client: TestClient, setup_observability: object
) -> None:
    # Given: a trace ingested in reverse chronological order
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-03",
                timestamp=_BASE_TIME + timedelta(seconds=2),
            ),
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            ),
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-02",
                timestamp=_BASE_TIME + timedelta(seconds=1),
            ),
        ],
    )

    # When: the trace is read
    body = client.get(f"{_TRACES_URL}/{trace_id}").json()

    # Then: timestamps sort the hops, so ingestion order leaves no trace and
    # nothing is flagged; only ties are detectable skew.
    assert [hop["span_id"] for hop in body["hops"]] == [
        "span-01",
        "span-02",
        "span-03",
    ]
    assert body["out_of_order"] is False


# =============================================================================
# Truncation
# =============================================================================


def test_limit_below_hop_count_truncates_and_reports_the_true_total(
    client: TestClient, setup_observability: object
) -> None:
    # Given: a five-hop trace
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id=f"span-{index:02d}",
                timestamp=_BASE_TIME + timedelta(seconds=index),
            )
            for index in range(1, 6)
        ],
    )

    # When: it is read with a cap of two
    resp = client.get(f"{_TRACES_URL}/{trace_id}", params={"limit": 2})

    # Then: the earliest two hops come back, and the clipping is reported
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [hop["span_id"] for hop in body["hops"]] == ["span-01", "span-02"]
    assert body["hop_count"] == 2
    assert body["total_hop_count"] == 5
    assert body["truncated"] is True
    assert body["limit"] == 2


def test_limit_equal_to_hop_count_is_not_truncated(
    client: TestClient, setup_observability: object
) -> None:
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id=f"span-{index:02d}",
                timestamp=_BASE_TIME + timedelta(seconds=index),
            )
            for index in range(1, 4)
        ],
    )

    body = client.get(f"{_TRACES_URL}/{trace_id}", params={"limit": 3}).json()

    assert body["hop_count"] == 3
    assert body["total_hop_count"] == 3
    assert body["truncated"] is False


# =============================================================================
# Validation and 404
# =============================================================================


def test_unknown_trace_id_returns_404(
    client: TestClient, setup_observability: object
) -> None:
    _ = setup_observability

    resp = client.get(f"{_TRACES_URL}/{_trace_id()}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == ErrorCode.RESOURCE_NOT_FOUND.value


def test_out_of_range_limit_returns_422(
    client: TestClient, setup_observability: object
) -> None:
    _ = setup_observability
    trace_id = _trace_id()

    assert (
        client.get(f"{_TRACES_URL}/{trace_id}", params={"limit": 0}).status_code == 422
    )
    assert (
        client.get(f"{_TRACES_URL}/{trace_id}", params={"limit": -1}).status_code == 422
    )
    assert (
        client.get(f"{_TRACES_URL}/{trace_id}", params={"limit": 1001}).status_code
        == 422
    )


def test_non_integer_limit_returns_422(
    client: TestClient, setup_observability: object
) -> None:
    _ = setup_observability

    resp = client.get(f"{_TRACES_URL}/{_trace_id()}", params={"limit": "all"})

    assert resp.status_code == 422, resp.text


def test_limit_at_the_maximum_is_accepted(
    client: TestClient, setup_observability: object
) -> None:
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    resp = client.get(f"{_TRACES_URL}/{trace_id}", params={"limit": 1000})

    assert resp.status_code == 200, resp.text
    assert resp.json()["limit"] == 1000


# =============================================================================
# Authorization tiers
# =============================================================================


def test_authenticated_non_admin_can_read_a_trace(
    client: TestClient, non_admin_client: TestClient, setup_observability: object
) -> None:
    # Given: a trace ingested by an admin caller
    _ = setup_observability
    agent_name = _agent_name()
    _place_on_team(client, agent_name=agent_name, display_name="Marketing")
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    # When/Then: a non-admin credential is enough to read it
    resp = non_admin_client.get(f"{_TRACES_URL}/{trace_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["hops"][0]["team"]["slug"] == "marketing"


def test_unauthenticated_client_is_rejected(
    unauthenticated_client: TestClient, setup_observability: object
) -> None:
    _ = setup_observability

    resp = unauthenticated_client.get(f"{_TRACES_URL}/{_trace_id()}")

    assert resp.status_code == 401, resp.text


def test_authorizer_denying_observability_read_returns_403(
    app: FastAPI, client: TestClient, setup_observability: object
) -> None:
    # Given: a trace that exists
    _ = setup_observability
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    trace_id = _trace_id()
    _ingest(
        client,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    # When: the authorizer refuses the read operation
    set_authorizer(ReadDeniedAuthorizer())
    resp = TestClient(app, raise_server_exceptions=True).get(f"{_TRACES_URL}/{trace_id}")

    # Then: the route is forbidden rather than leaking the trace
    assert resp.status_code == 403, resp.text


# =============================================================================
# Namespace isolation
# =============================================================================


def test_trace_in_one_namespace_is_invisible_from_another(
    app: FastAPI, setup_observability: object
) -> None:
    _ = setup_observability
    # Given: a trace ingested in namespace A
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_name = _agent_name()
    _register_agent(ns_a, agent_name)
    trace_id = _trace_id()
    _ingest(
        ns_a,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    # When/Then: namespace B cannot see it, and namespace A still can
    assert ns_b.get(f"{_TRACES_URL}/{trace_id}").status_code == 404
    owner = ns_a.get(f"{_TRACES_URL}/{trace_id}")
    assert owner.status_code == 200, owner.text
    assert owner.json()["total_hop_count"] == 1


def test_same_trace_id_in_two_namespaces_reads_as_two_traces(
    app: FastAPI, setup_observability: object
) -> None:
    _ = setup_observability
    # Given: the same trace ID used by unrelated agents in two namespaces
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_a, agent_b = _agent_name(), _agent_name()
    _register_agent(ns_a, agent_a)
    _register_agent(ns_b, agent_b)
    trace_id = _trace_id()
    _ingest(
        ns_a,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_a,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )
    _ingest(
        ns_b,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_b,
                span_id="span-02",
                timestamp=_BASE_TIME + timedelta(seconds=1),
            ),
            _event(
                trace_id=trace_id,
                agent_name=agent_b,
                span_id="span-03",
                timestamp=_BASE_TIME + timedelta(seconds=2),
            ),
        ],
    )

    # When/Then: neither namespace sees the other's hops
    body_a = ns_a.get(f"{_TRACES_URL}/{trace_id}").json()
    assert body_a["total_hop_count"] == 1
    assert [hop["agent_name"] for hop in body_a["hops"]] == [agent_a]

    body_b = ns_b.get(f"{_TRACES_URL}/{trace_id}").json()
    assert body_b["total_hop_count"] == 2
    assert {hop["agent_name"] for hop in body_b["hops"]} == {agent_b}


def test_team_membership_does_not_leak_across_namespaces(
    app: FastAPI, setup_observability: object
) -> None:
    _ = setup_observability
    # Given: an agent name that belongs to a team in namespace B only
    set_authorizer(HeaderNamespaceAuthorizer())
    ns_a = _namespace_client(app, "ns-a")
    ns_b = _namespace_client(app, "ns-b")

    agent_name = _agent_name()
    _register_agent(ns_a, agent_name)
    _place_on_team(ns_b, agent_name=agent_name, display_name="Engineering")

    trace_id = _trace_id()
    _ingest(
        ns_a,
        [
            _event(
                trace_id=trace_id,
                agent_name=agent_name,
                span_id="span-01",
                timestamp=_BASE_TIME,
            )
        ],
    )

    # When/Then: the hop read from namespace A carries no team
    hop = ns_a.get(f"{_TRACES_URL}/{trace_id}").json()["hops"][0]
    assert hop["agent_name"] == agent_name
    assert hop["team"] is None
