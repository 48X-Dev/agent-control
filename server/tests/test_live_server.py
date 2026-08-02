"""Coverage for the ``live_server`` fixture family itself.

Two things are being proved here. First, that the real application behaves
over a real socket the way it does under ``TestClient`` - same routing, same
middleware, same auth - so a test moved onto the live server for streaming
reasons does not have to be rewritten. Second, and the reason the fixture
exists at all, that response bodies reach the client incrementally, which is
the one property ``TestClient`` and ``httpx.ASGITransport`` cannot show
because both buffer the whole response before returning it.

Third, that the fixture cleans up after itself: no task left running, no port
left bound, and a stalled handler cancelled inside a bounded window rather
than hanging the suite. Later phases point this same factory at a stub
executor that misbehaves on purpose, so teardown has to survive one.
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid
from typing import Any

import httpx
import pytest
from agent_control_models.errors import ErrorCode
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.errors import ForbiddenError

from .conftest import (
    LIVE_SERVER_GRACEFUL_TIMEOUT,
    LIVE_SERVER_HOST,
    LIVE_SERVER_SHUTDOWN_TIMEOUT,
    TEST_ADMIN_API_KEY,
    TEST_API_KEY,
    LiveServer,
    LiveServerContext,
    LiveServerFactory,
)

AGENTS_URL = "/api/v1/agents"
TEAMS_URL = "/api/v1/teams"
VERSION_HEADER = "X-Agent-Control-Server-Version"


async def test_health_is_served_over_a_real_socket(live_client: httpx.AsyncClient) -> None:
    response = await live_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    # The version middleware runs on the live server too, not just in-process.
    assert response.headers[VERSION_HEADER]


async def test_authenticated_endpoint_is_reachable_over_a_real_socket(
    live_client: httpx.AsyncClient,
) -> None:
    response = await live_client.get(AGENTS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["agents"] == []
    assert body["pagination"]["total"] == 0


async def test_missing_api_key_is_rejected_over_a_real_socket(live_server: LiveServer) -> None:
    unauthenticated = live_server.client()

    response = await unauthenticated.get(AGENTS_URL)

    assert response.status_code == 401


async def test_absolute_urls_point_at_the_bound_port(live_server: LiveServer) -> None:
    assert live_server.url_for(AGENTS_URL) == f"{live_server.base_url}{AGENTS_URL}"

    async with httpx.AsyncClient() as raw_client:
        response = await raw_client.get(
            live_server.url_for(AGENTS_URL),
            headers={"X-API-Key": TEST_ADMIN_API_KEY},
        )

    assert response.status_code == 200


def _dripping_app(released: asyncio.Event) -> ASGIApp:
    """An ASGI app that sends one chunk, waits, then sends the rest.

    The wait is gated on the test rather than on a sleep, so "the first chunk
    arrived before the response finished" is an ordering fact and not a timing
    guess.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "http"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"first\n", "more_body": True})
        await released.wait()
        await send({"type": "http.response.body", "body": b"second\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    return app


async def test_response_chunks_arrive_before_the_response_completes(
    live_server_factory: LiveServerFactory,
) -> None:
    released = asyncio.Event()
    server = await live_server_factory(_dripping_app(released))
    client = server.client()

    async with client.stream("GET", "/") as response:
        assert response.status_code == 200
        lines = response.aiter_lines()

        # This would deadlock through TestClient or ASGITransport: neither
        # yields anything until the app has finished the whole body, and the
        # app does not finish until the event below is set.
        assert await asyncio.wait_for(anext(lines), timeout=5.0) == "first"
        assert not released.is_set()

        released.set()
        assert await asyncio.wait_for(anext(lines), timeout=5.0) == "second"


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def test_server_releases_its_port_when_the_context_exits(
    live_server_context: LiveServerContext,
) -> None:
    async with live_server_context(_ok_app) as server:
        port = server.port
        client = server.client()
        assert (await client.get("/")).status_code == 204

    async with httpx.AsyncClient() as raw_client:
        with pytest.raises(httpx.ConnectError):
            await raw_client.get(f"http://127.0.0.1:{port}/", timeout=5.0)


async def test_two_servers_run_side_by_side_on_distinct_ports(
    live_server_factory: LiveServerFactory,
) -> None:
    first = await live_server_factory(_ok_app)
    second = await live_server_factory(_ok_app)

    assert first.port != second.port
    assert (await first.client().get("/")).status_code == 204
    assert (await second.client().get("/")).status_code == 204


# =============================================================================
# Parity with TestClient
#
# The fixture is only useful if moving a test onto it changes nothing except
# the transport. Each of these drives the same route both ways in the same
# test and compares the results rather than restating them.
# =============================================================================


def _display_name() -> str:
    return f"Team {uuid.uuid4().hex[:8]}"


def _without_volatile_fields(payload: Any) -> Any:
    """Drop the one field that differs between two runs of the same request.

    Error envelopes carry ``metadata.timestamp``, which is wall-clock and so
    never matches across two calls. Everything else in the envelope must.
    """
    if not isinstance(payload, dict) or "metadata" not in payload:
        return payload
    metadata = dict(payload["metadata"])
    assert metadata.pop("timestamp", None), payload
    return {**payload, "metadata": metadata}


async def test_a_read_matches_testclient(
    client: TestClient, live_client: httpx.AsyncClient
) -> None:
    created = client.put(TEAMS_URL, json={"display_name": _display_name()})
    assert created.status_code == 200, created.text
    slug = created.json()["slug"]

    over_socket = await live_client.get(f"{TEAMS_URL}/{slug}")
    in_process = client.get(f"{TEAMS_URL}/{slug}")

    assert over_socket.status_code == in_process.status_code == 200
    assert over_socket.json() == in_process.json()
    assert over_socket.json()["slug"] == slug


async def test_a_404_matches_testclient(
    client: TestClient, live_client: httpx.AsyncClient
) -> None:
    missing = f"absent-{uuid.uuid4().hex[:8]}"

    over_socket = await live_client.get(f"{TEAMS_URL}/{missing}")
    in_process = client.get(f"{TEAMS_URL}/{missing}")

    assert over_socket.status_code == in_process.status_code == 404
    assert _without_volatile_fields(over_socket.json()) == _without_volatile_fields(
        in_process.json()
    )
    assert over_socket.json()["error_code"] == ErrorCode.TEAM_NOT_FOUND.value


async def test_a_validation_error_matches_testclient(
    client: TestClient, live_client: httpx.AsyncClient
) -> None:
    # ``display_name`` is required, so the request never reaches the service.
    over_socket = await live_client.put(TEAMS_URL, json={})
    in_process = client.put(TEAMS_URL, json={})

    assert over_socket.status_code == in_process.status_code == 422
    assert _without_volatile_fields(over_socket.json()) == _without_volatile_fields(
        in_process.json()
    )


# =============================================================================
# Authorization over a real socket
#
# The live server shares this process, so the per-test authorizer installed by
# ``_install_default_authorizer`` (and anything a test overrides it with)
# applies to it. That is worth proving rather than assuming: the fixture runs
# with ``lifespan="off"`` precisely so startup cannot overwrite it.
# =============================================================================


async def test_both_authorization_tiers_are_enforced_over_a_real_socket(
    live_server: LiveServer,
) -> None:
    non_admin = live_server.client(headers={"X-API-Key": TEST_API_KEY})

    # TEAMS_READ is the authenticated tier: a plain key is enough.
    assert (await non_admin.get(TEAMS_URL)).status_code == 200
    # TEAMS_WRITE is the admin tier: the same key is refused.
    assert (
        await non_admin.put(TEAMS_URL, json={"display_name": _display_name()})
    ).status_code == 403


class _TeamWriteDeniedAuthorizer:
    """Authorizes everything except ``TEAMS_WRITE``."""

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del request, context
        if operation is Operation.TEAMS_WRITE:
            raise ForbiddenError(
                error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
                detail="write denied",
            )
        return Principal(namespace_key="default", is_admin=True)


async def test_a_restricted_authorizer_is_honoured_over_a_real_socket(
    live_server: LiveServer,
) -> None:
    set_authorizer(_TeamWriteDeniedAuthorizer())
    restricted = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})

    denied = await restricted.put(TEAMS_URL, json={"display_name": _display_name()})

    assert denied.status_code == 403, denied.text
    assert denied.json()["error_code"] == ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES.value
    # And the operation it did not restrict still works, so this is a
    # per-operation refusal rather than a blanket one.
    assert (await restricted.get(TEAMS_URL)).status_code == 200


class _HeaderNamespaceAuthorizer:
    """Maps ``X-Test-Namespace`` onto ``Principal.namespace_key``."""

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


async def test_namespaces_stay_isolated_over_a_real_socket(live_server: LiveServer) -> None:
    set_authorizer(_HeaderNamespaceAuthorizer())
    alpha = live_server.client(headers={"X-Test-Namespace": "alpha"})
    beta = live_server.client(headers={"X-Test-Namespace": "beta"})

    created = await alpha.put(TEAMS_URL, json={"display_name": _display_name()})
    assert created.status_code == 200, created.text
    slug = created.json()["slug"]

    assert (await beta.get(f"{TEAMS_URL}/{slug}")).status_code == 404
    assert (await beta.get(TEAMS_URL)).json()["teams"] == []
    # Alpha still sees its own team, so the 404 is scoping and not a
    # failed write.
    assert (await alpha.get(f"{TEAMS_URL}/{slug}")).status_code == 200
    assert [team["slug"] for team in (await alpha.get(TEAMS_URL)).json()["teams"]] == [slug]


# =============================================================================
# Fixture hygiene
# =============================================================================


def _marker_app(marker: bytes) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": marker})

    return app


async def _settle(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def test_teardown_leaves_no_tasks_running_and_frees_the_port(
    live_server_context: LiveServerContext,
) -> None:
    before = asyncio.all_tasks()

    async with live_server_context(_ok_app) as server:
        port = server.port
        assert (await server.client().get("/")).status_code == 204

    await _settle(lambda: not (asyncio.all_tasks() - before))
    leaked = {task.get_name() for task in asyncio.all_tasks() - before}
    assert not leaked, f"live server left tasks running: {leaked}"

    # An open listening socket would refuse this bind even with SO_REUSEADDR,
    # which a lingering TIME_WAIT connection would not.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((LIVE_SERVER_HOST, port))
    finally:
        probe.close()


async def test_repeated_use_never_answers_from_a_previous_server(
    live_server_context: LiveServerContext,
) -> None:
    """Sequential servers may be handed the same port back by the OS.

    What must not happen is a request landing on a server that has already
    been torn down, so each app here returns a body only it can return.
    """
    for marker in (b"first", b"second", b"third", b"fourth"):
        async with live_server_context(_marker_app(marker)) as server:
            response = await server.client().get("/")

            assert response.status_code == 200
            assert response.content == marker


async def _stalling_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Starts a response, then never finishes it - and ignores disconnects."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"opened\n", "more_body": True})
    await asyncio.sleep(3600)


async def test_teardown_is_bounded_when_a_handler_never_finishes(
    live_server_context: LiveServerContext,
) -> None:
    """A stalled handler must be cancelled by teardown, not outlive the test.

    This is the shape a stub executor takes in later phases, so the harness
    has to survive it. uvicorn's graceful window cancels the handler; the
    outer wait in ``_stop_live_server`` only fires if that fails, and it
    raising here would be a real failure rather than a flake.
    """
    started = time.monotonic()

    async with live_server_context(_stalling_app) as server:
        client = server.client(timeout=httpx.Timeout(5.0))
        async with client.stream("GET", "/") as response:
            assert response.status_code == 200
            assert await asyncio.wait_for(anext(response.aiter_lines()), timeout=5.0) == "opened"

    elapsed = time.monotonic() - started
    assert elapsed < LIVE_SERVER_SHUTDOWN_TIMEOUT, (
        f"teardown took {elapsed:.1f}s; the graceful window is "
        f"{LIVE_SERVER_GRACEFUL_TIMEOUT}s"
    )
