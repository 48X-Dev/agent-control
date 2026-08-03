"""The byte cap, and where it actually lives.

The status code proves nothing here. A handler that reads the whole body and
then answers 413 and a middleware that abandons the request at the cap return
exactly the same response, and only one of them keeps an unbounded POST off the
server's temp filesystem. So the assertions below are on how many bytes the
framework buffered, counted at ``UploadFile.write`` - the method Starlette's
multipart parser calls for every chunk of the file part, spooling to a real
file past a megabyte.

That is the U1 proof-by-absence from section 11 of the plan, in its stronger
form: not only no attachment row and no blob row, but no buffer either.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import starlette.datastructures as starlette_data
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import TEST_ADMIN_API_KEY, engine
from .test_agent_attachments_endpoints import (
    _SESSIONS_URL,
    _XHR,
    PDF_BYTES,
    make_session,
    upload,
)

_CAP = 4096
_WELL_OVER_THE_CAP = PDF_BYTES + b"\x00" * (4 * 1024 * 1024)


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    monkeypatch.setattr(executor_settings, "attachment_max_bytes", _CAP)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


@pytest.fixture
def buffered_bytes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every byte the framework writes into an ``UploadFile``."""
    counted = [0]
    original = starlette_data.UploadFile.write

    async def counting_write(self: Any, data: bytes) -> Any:
        counted[0] += len(data)
        return await original(self, data)

    monkeypatch.setattr(starlette_data.UploadFile, "write", counting_write)
    return counted


def _too_large_total() -> float:
    return (
        REGISTRY.get_sample_value(
            "agent_control_server_attachment_uploads_total", {"result": "too_large"}
        )
        or 0.0
    )


def _row_counts() -> tuple[int, int]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM agent_session_attachments")
        ).scalar()
        blobs = conn.execute(
            text("SELECT count(*) FROM agent_session_attachment_blobs")
        ).scalar()
    return int(rows or 0), int(blobs or 0)


# ---------------------------------------------------------------------------
# Proof by absence
# ---------------------------------------------------------------------------


def test_an_oversize_body_is_never_buffered(
    client: TestClient, buffered_bytes: list[int]
) -> None:
    """The assertion the response cannot make.

    Four megabytes against a four-kilobyte cap. If any of it reaches an
    ``UploadFile``, the cap is being applied after the fact and the temp
    filesystem is reachable by anyone who can reach the route.
    """
    session_key = make_session()

    resp = upload(client, session_key, data=_WELL_OVER_THE_CAP)

    assert resp.status_code == 413, resp.text
    assert buffered_bytes[0] == 0, (
        f"the framework buffered {buffered_bytes[0]} bytes before the refusal"
    )


def test_a_body_within_the_cap_is_still_buffered_and_stored(
    client: TestClient, buffered_bytes: list[int]
) -> None:
    """The control group. Without it, a middleware that refused everything
    would pass the test above."""
    session_key = make_session()

    resp = upload(client, session_key)

    assert resp.status_code == 201, resp.text
    assert buffered_bytes[0] == len(PDF_BYTES)


def test_an_oversize_upload_leaves_no_row_and_no_blob(client: TestClient) -> None:
    upload(client, make_session(), data=_WELL_OVER_THE_CAP)

    assert _row_counts() == (0, 0)


# ---------------------------------------------------------------------------
# The boundary itself
#
# ``attachment_max_bytes`` is a ceiling on a file, and the middleware can only
# see a body: the file plus a boundary, two sets of part headers, the filename
# and ``declared_name``. Compared naively the two are the same number and a file
# of exactly the ceiling is refused - by a message quoting the envelope's size,
# against a UI pre-check that measured the file and let it through.
#
# So the middleware bounds the body at the ceiling plus a fixed allowance and
# the exact per-file bound stays in the handler, which counts the file part on
# its own. The three cases below are what keep anyone from collapsing those two
# numbers back together.
# ---------------------------------------------------------------------------


def _file_of(size: int) -> bytes:
    """A well-formed PDF of exactly ``size`` bytes."""
    return PDF_BYTES + b"\x00" * (size - len(PDF_BYTES))


def test_a_file_of_exactly_the_ceiling_is_accepted(client: TestClient) -> None:
    """The regression. The envelope must not be charged to the file's budget."""
    body = _file_of(_CAP)

    resp = upload(client, make_session(), data=body)

    assert resp.status_code == 201, resp.text
    assert resp.json()["attachment"]["size_bytes"] == _CAP


def test_one_byte_over_the_ceiling_is_refused_by_the_file_count(
    client: TestClient,
) -> None:
    """And the refusal names the file's size, not the envelope's.

    This is the half of the pair that matters. The allowance would be a hole if
    nothing exact sat behind it, and a refusal quoting a number the operator
    cannot reconcile with the file on their disk is the defect this replaced.
    """
    resp = upload(client, make_session(), data=_file_of(_CAP + 1))

    assert resp.status_code == 413, resp.text
    assert f"is {_CAP + 1} bytes" in resp.json()["detail"]
    assert _row_counts() == (0, 0)


def test_a_body_past_the_allowance_is_still_refused_unbuffered(
    client: TestClient, buffered_bytes: list[int]
) -> None:
    """The allowance is a few kilobytes, not an exemption."""
    resp = upload(client, make_session(), data=_file_of(_CAP * 4))

    assert resp.status_code == 413, resp.text
    assert buffered_bytes[0] == 0


# ---------------------------------------------------------------------------
# The refusals themselves
# ---------------------------------------------------------------------------


def test_the_refusal_is_typed_and_names_the_ceiling(client: TestClient) -> None:
    resp = upload(client, make_session(), data=_WELL_OVER_THE_CAP)

    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["error_code"] == "ATTACHMENT_TOO_LARGE"
    assert str(_CAP) in body["detail"]
    assert "AGENT_CONTROL_EXECUTOR_ATTACHMENT_MAX_BYTES" in body["hint"]


def test_the_refusal_moves_the_too_large_counter(client: TestClient) -> None:
    """The label existed and nothing incremented it, so a cap set too low and a
    deployment nobody attaches files to graphed identically."""
    before = _too_large_total()

    upload(client, make_session(), data=_WELL_OVER_THE_CAP)

    assert _too_large_total() == before + 1


def _multipart_body(data: bytes) -> bytes:
    """The wire form of what ``upload`` sends, built by hand.

    Needed because the lie under test is in the header, and no HTTP client will
    write a ``Content-Length`` that disagrees with the body it is sending.
    """
    return (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="declared_name"\r\n\r\n'
        b"spec.pdf\r\n"
        b"--b\r\n"
        b'Content-Disposition: form-data; name="file"; filename="spec.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n" + data + b"\r\n--b--\r\n"
    )


async def test_a_content_length_that_understates_the_body_is_aborted_on_the_count(
    app: Any,
) -> None:
    """A header that lies is what an attacker sends, so the count is the control.

    Driven against the whole mounted application rather than against the
    middleware wrapped around a stub, and the difference is the entire point of
    the case. FastAPI parses the multipart body during routing, inside a
    ``try/except Exception`` that turns anything raised there into an untyped
    ``400 There was an error parsing the body``. A middleware that abandoned the
    request by raising was therefore answering 400 on the mounted app while a
    stub-based test read 413, and ``result="too_large"`` stayed flat on the one
    path an attacker controls. A stub cannot see that: it has no body parser to
    swallow the exception.

    ``TestClient`` cannot be used either, because its transport writes an honest
    ``Content-Length`` and truncates the body to match, so the scope is built
    here instead.
    """
    session_key = make_session()
    body = _multipart_body(PDF_BYTES + b"\x00" * (4 * 1024 * 1024))
    chunks = [body[i : i + 65536] for i in range(0, len(body), 65536)]
    total_chunks = len(chunks)
    delivered: list[int] = []
    sent: list[dict[str, Any]] = []
    before = _too_large_total()

    async def receive() -> dict[str, Any]:
        if chunks:
            chunk = chunks.pop(0)
            delivered.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    path = f"/api/v1/agent-sessions/{session_key}/attachments"
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"multipart/form-data; boundary=b"),
                (b"content-length", b"128"),
                (b"x-requested-with", b"XMLHttpRequest"),
                (b"x-api-key", TEST_ADMIN_API_KEY.encode()),
            ],
        },
        receive,
        send,
    )

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, "the app's own answer was forwarded as well as the refusal"
    assert starts[0]["status"] == 413
    payload = json.loads(
        b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    )
    assert payload["error_code"] == "ATTACHMENT_TOO_LARGE"
    assert _too_large_total() == before + 1
    # One chunk past the cap and then nothing: the stream is abandoned rather
    # than drained, which is the difference between refusing a large body and
    # reading it in order to refuse it.
    assert sum(delivered) <= _CAP + 65536
    assert len(chunks) == total_chunks - 1, "the rest of the body was read anyway"
    assert _row_counts() == (0, 0)


def test_a_request_with_no_content_length_is_refused(client: TestClient) -> None:
    """A body whose size cannot be checked in advance is one whose size the
    sender declined to declare."""

    def chunks() -> Any:
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n"
        yield b"Content-Type: application/pdf\r\n\r\n"
        yield PDF_BYTES
        yield b"\r\n--b--\r\n"

    resp = client.post(
        f"{_SESSIONS_URL}/{make_session()}/attachments",
        content=chunks(),
        headers={**_XHR, "Content-Type": "multipart/form-data; boundary=b"},
    )

    assert resp.status_code == 413, resp.text
    assert "did not declare its length" in resp.json()["detail"]


def test_a_content_length_that_is_not_a_number_is_refused(client: TestClient) -> None:
    resp = client.post(
        f"{_SESSIONS_URL}/{make_session()}/attachments",
        content=b"x",
        headers={
            **_XHR,
            "Content-Type": "multipart/form-data; boundary=b",
            "Content-Length": "not-a-number",
        },
    )

    assert resp.status_code == 413, resp.text


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_the_limit_does_not_apply_to_other_routes(client: TestClient) -> None:
    """Scoped by path, and asserted so that widening it later is a decision.

    A four-kilobyte cap applied to every route would refuse an ordinary control
    definition, and the failure would read as a validation error somewhere far
    from here.
    """
    payload = {"name": "x" * 8192, "description": "y" * 8192}

    resp = client.post("/api/v1/agents/initAgent", json=payload)

    # 422 rather than merely "not 413": a validation error can only be produced
    # after the body was read and decoded, so this says the sixteen kilobytes
    # arrived. A bare ``!= 413`` would also pass against a 404.
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_the_upload_route_is_matched_whatever_the_session_key(client: TestClient) -> None:
    """The middleware matches a path pattern, so a key containing something
    path-like must still be covered rather than silently exempt."""
    from agent_control_server.middleware import is_attachment_upload

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/agent-sessions/deadbeef/attachments",
    }
    assert is_attachment_upload(scope) is True
    assert is_attachment_upload({**scope, "method": "GET"}) is False
    assert (
        is_attachment_upload(
            {**scope, "path": "/api/v1/agent-sessions/deadbeef/attachments/abc/content"}
        )
        is False
    )
