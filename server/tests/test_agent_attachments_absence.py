"""What the attachment routes must **not** do, proved by absence.

Following E2 (``test_google_adk_mcp_tools.py:403``) and H2
(``test_google_adk_adk_contract.py:443``). Every case here is one where the
response body cannot tell a correct implementation from a wrong one, so the
assertion is over something else: the SQL the request actually issued, the
executor calls it made, or the rows it left behind.

Four properties, each cheap to break and each invisible to a status-code test.

*Listing does not read bytes.* The two-table split in section 3.5 exists so that
rendering a transcript never pulls a 20MB ``bytea`` into memory, and one
careless ``selectinload`` would undo it while every existing assertion still
passed. The recorder below is proved to work by a control case that watches the
one route which **should** touch the blob table.

*Uploading spends no executor call.* Files land in Postgres and a turn is what
delivers them. An implementation that warmed a session on the executor during an
upload would return the same 201, and would make the "upload while a turn is in
flight" row of section 9 a 409 instead of a success.

*Every refusal stores nothing.* A 415, a 400 or a 413 returned after a write and
a sloppy rollback looks exactly like one returned before the write. U1 in
section 11 says so about the byte cap; the same argument covers the type gate,
the empty part, the missing CSRF header and the quotas, and each is asserted by
counting rows rather than by reading the response.

*A tombstone hands back no bytes.* The 410 is written prose either way; what
matters is that the reclaimed content is not in the response.

The refusal cases run under **both** providers. Under ``NoAuthProvider`` the
caller hash is None and the write path takes different branches to reach the
same refusal, and a suite that only ever runs the header provider proves half of
each.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from agent_control_server.auth_framework import set_authorizer
from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider
from agent_control_server.config import executor_settings
from agent_control_server.db import async_engine
from agent_control_server.services.attachment_quota import reset_attachment_quota
from agent_control_server.services.executor_factory import get_executor_client_factory
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from .conftest import engine
from .test_agent_attachments_endpoints import (
    PDF_BYTES,
    PNG_BYTES,
    ZIP_BYTES,
    make_session,
    upload,
)
from .test_agent_sessions_endpoints import FakeExecutorFactory

_SESSIONS_URL = "/api/v1/agent-sessions"
_BLOB_TABLE = "agent_session_attachment_blobs"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


@pytest.fixture()
def recorded_sql() -> Iterator[list[str]]:
    """Every statement the app issues on its own engine while this is installed.

    The listener sits on the async engine the request path uses, not on the
    test's sync engine, so fixture setup written with ``engine`` is invisible
    here and only what the route did is recorded.
    """
    statements: list[str] = []

    def record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)


@pytest.fixture()
def no_auth_client(app: Any) -> TestClient:
    set_authorizer(NoAuthProvider())
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(params=["header", "no_auth"])
def either_provider(
    request: pytest.FixtureRequest, client: TestClient, app: Any
) -> TestClient:
    """The same case under the two providers this feature has to hold under.

    The admin header client and ``NoAuthProvider`` reach the write path by
    different branches - one is an admin, the other has no caller identity at
    all - and both must refuse before writing anything.
    """
    if request.param == "header":
        return client
    set_authorizer(NoAuthProvider())
    return TestClient(app, raise_server_exceptions=True)


def _row_counts(session_key: str) -> tuple[int, int]:
    """``(attachment rows, blob rows)`` for one session."""
    with engine.begin() as conn:
        attachments = conn.execute(
            text(
                "SELECT count(*) FROM agent_session_attachments a "
                " JOIN agent_sessions s ON s.id = a.session_id "
                " WHERE s.session_key = :key"
            ),
            {"key": session_key},
        ).scalar_one()
        blobs = conn.execute(
            text(
                "SELECT count(*) FROM agent_session_attachment_blobs b "
                " JOIN agent_session_attachments a ON a.id = b.attachment_id "
                " JOIN agent_sessions s ON s.id = a.session_id "
                " WHERE s.session_key = :key"
            ),
            {"key": session_key},
        ).scalar_one()
    return int(attachments), int(blobs)


# ---------------------------------------------------------------------------
# Reading metadata never reads bytes
# ---------------------------------------------------------------------------


def test_listing_attachments_issues_no_statement_against_the_blob_table(
    client: TestClient, recorded_sql: list[str]
) -> None:
    """The response is identical either way. The cost is not.

    A list that joined the blobs in would still answer with no bytes in the
    body, would still pass every assertion in the endpoints suite, and would
    hold every attachment on the session in memory to do it.
    """
    session_key = make_session()
    upload(client, session_key)
    recorded_sql.clear()

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments")

    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1
    assert any("agent_session_attachments" in stmt for stmt in recorded_sql)
    assert [stmt for stmt in recorded_sql if _BLOB_TABLE in stmt] == []


def test_reading_one_attachment_issues_no_statement_against_the_blob_table(
    client: TestClient, recorded_sql: list[str]
) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    recorded_sql.clear()

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")

    assert resp.status_code == 200, resp.text
    assert [stmt for stmt in recorded_sql if _BLOB_TABLE in stmt] == []


def test_the_download_route_does_read_the_blob_table(
    client: TestClient, recorded_sql: list[str]
) -> None:
    """The control case, and the reason the two above mean anything.

    Without it a recorder that silently observed nothing would report the same
    empty list for every route on this router.
    """
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    recorded_sql.clear()

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert resp.status_code == 200, resp.text
    assert [stmt for stmt in recorded_sql if _BLOB_TABLE in stmt] != []


# ---------------------------------------------------------------------------
# An upload reaches no executor, and a turn in flight does not block one
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_outbound_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every request that leaves this process over httpx.

    ``TestClient`` drives the app through a sync transport, so watching
    ``AsyncClient`` catches the executor client and nothing else. This is the
    half a fake factory cannot cover: an implementation that built its own
    client instead of taking the injected one would satisfy the factory
    assertion and still spend the call.
    """
    sent: list[str] = []
    original = httpx.AsyncClient.send

    async def recording_send(self: Any, request: Any, **kwargs: Any) -> Any:
        sent.append(str(request.url))
        return await original(self, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", recording_send)
    return sent


def test_an_upload_while_a_turn_is_in_flight_calls_no_executor(
    client: TestClient, app: Any, no_outbound_http: list[str]
) -> None:
    """Section 9: the upload succeeds, because it touches no executor and takes
    no session lock. Binding is what a running turn blocks.

    Asserted on the executor rather than on the status, because an
    implementation that warmed the executor session during the upload would
    return the same 201 and would fail the moment a real turn was running.
    """
    factory = FakeExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    try:
        session_key = make_session(in_flight_trace_id="trace-that-is-running")

        resp = upload(client, session_key)
    finally:
        app.dependency_overrides.pop(get_executor_client_factory, None)

    assert resp.status_code == 201, resp.text
    assert factory.calls == []
    assert no_outbound_http == []


async def test_the_outbound_recorder_sees_a_request_when_there_is_one(
    no_outbound_http: list[str],
) -> None:
    """The control for the assertion above.

    An empty list is what a recorder that was never installed reports too, and
    the whole weight of the previous test rests on the difference.
    """
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as probe:
        await probe.get("http://agent-executor:8080/run")

    assert no_outbound_http == ["http://agent-executor:8080/run"]


def test_the_upload_leaves_the_running_turn_alone(client: TestClient) -> None:
    """The other half of the same row: the in-flight marker is not touched.

    An upload that took the turn lock would either block or clear this, and
    both are how a running invocation loses its own session.
    """
    session_key = make_session(in_flight_trace_id="trace-that-is-running")

    assert upload(client, session_key).status_code == 201

    with engine.begin() as conn:
        still_running = conn.execute(
            text("SELECT in_flight_trace_id FROM agent_sessions WHERE session_key = :k"),
            {"k": session_key},
        ).scalar_one()
    assert still_running == "trace-that-is-running"


# ---------------------------------------------------------------------------
# Every refusal stores nothing, under both providers
# ---------------------------------------------------------------------------


def test_a_refused_type_stores_no_row_and_no_blob(
    either_provider: TestClient,
) -> None:
    """415 is returned by an implementation that stores then deletes, too."""
    session_key = make_session()

    resp = upload(
        either_provider,
        session_key,
        data=ZIP_BYTES,
        filename="deck.pdf",
        content_type="application/pdf",
    )

    assert resp.status_code == 415, resp.text
    assert _row_counts(session_key) == (0, 0)


def test_an_empty_part_stores_no_row_and_no_blob(
    either_provider: TestClient,
) -> None:
    session_key = make_session()

    resp = upload(either_provider, session_key, data=b"")

    assert resp.status_code == 400, resp.text
    assert _row_counts(session_key) == (0, 0)


def test_an_upload_without_the_custom_header_stores_no_row_and_no_blob(
    either_provider: TestClient,
) -> None:
    """The CSRF refusal has to happen before the write, not after it.

    A cross-origin form post that stored the file and then answered 400 would
    have done the thing the header exists to prevent.
    """
    session_key = make_session()

    resp = upload(either_provider, session_key, headers={})

    assert resp.status_code == 400, resp.text
    assert _row_counts(session_key) == (0, 0)


def test_a_quota_refusal_stores_no_partial_row(
    either_provider: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused upload adds nothing, and the accepted one is still whole.

    Counting both is what separates "refused before the write" from "written,
    then rolled back so far that the previous upload's blob went with it".
    """
    monkeypatch.setattr(executor_settings, "attachment_max_per_session", 1)
    session_key = make_session()
    assert upload(either_provider, session_key).status_code == 201

    resp = upload(
        either_provider,
        session_key,
        data=PNG_BYTES,
        filename="screenshot.png",
        content_type="image/png",
    )

    assert resp.status_code == 413, resp.text
    assert _row_counts(session_key) == (1, 1)


# ---------------------------------------------------------------------------
# A tombstone hands back prose, never bytes
# ---------------------------------------------------------------------------


def test_a_tombstoned_download_carries_none_of_the_reclaimed_bytes(
    client: TestClient,
) -> None:
    """410 with a notice is the contract. The assertion is that the content of
    the file is nowhere in the response, since a route that reclaimed the row's
    status without reclaiming its blob would answer 410 and still leak."""
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    assert client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").status_code == 200

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert resp.status_code == 410, resp.text
    assert PDF_BYTES not in resp.content
    assert b"%PDF" not in resp.content
    assert _row_counts(session_key) == (1, 0)
