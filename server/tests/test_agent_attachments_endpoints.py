"""The upload, list, read, download and delete routes.

Sessions are inserted directly rather than opened through the executor path.
Nothing in this module is about opening a session, and routing every test
through a fake executor would make the failures here read as executor failures.

The PDF below is a real one only in the sense that matters to this server: it
starts with ``%PDF-``. Nothing on this path opens a document, so a longer
fixture would assert nothing extra and would hide that fact.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import engine

_SESSIONS_URL = "/api/v1/agent-sessions"
_XHR = {"X-Requested-With": "XMLHttpRequest"}

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 64


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def make_session(
    *,
    namespace_key: str = "default",
    created_by_hash: str | None = None,
    agent_task_id: int | None = None,
    in_flight_trace_id: str | None = None,
) -> str:
    """Insert one session row and return its key."""
    session_key = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_sessions "
                "(namespace_key, session_key, agent_name, executor_kind, "
                " executor_app_name, executor_user_id, executor_session_id, "
                " status, created_by_hash, agent_task_id, in_flight_trace_id) "
                "VALUES (:ns, :key, :agent, 'google_adk', 'app', :user, :sid, "
                "        'active', :hash, :task, :trace)"
            ),
            {
                "ns": namespace_key,
                "key": session_key,
                "agent": f"agent-{uuid.uuid4().hex[:8]}",
                "user": f"{namespace_key}:{uuid.uuid4().hex}",
                "sid": uuid.uuid4().hex,
                "hash": created_by_hash,
                "task": agent_task_id,
                "trace": in_flight_trace_id,
            },
        )
    return session_key


def upload(
    client: TestClient,
    session_key: str,
    *,
    data: bytes = PDF_BYTES,
    filename: str = "spec.pdf",
    content_type: str = "application/pdf",
    declared_name: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    return client.post(
        f"{_SESSIONS_URL}/{session_key}/attachments",
        files={"file": (filename, data, content_type)},
        data={"declared_name": declared_name or filename},
        headers=_XHR if headers is None else headers,
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_upload_then_read_it_back(client: TestClient) -> None:
    session_key = make_session()

    created = upload(client, session_key)
    assert created.status_code == 201, created.text
    body = created.json()
    attachment = body["attachment"]

    assert body["deduplicated"] is False
    assert attachment["display_name"] == "spec.pdf"
    assert attachment["sniffed_mime"] == "application/pdf"
    assert attachment["declared_mime"] == "application/pdf"
    assert attachment["mime_mismatch"] is False
    assert attachment["size_bytes"] == len(PDF_BYTES)
    assert attachment["status"] == "ready"
    assert attachment["origin"] == "operator_upload"
    # Null because nothing here opens the file, and a page count guessed from a
    # byte count would be a number somebody would later spend against.
    assert attachment["page_count"] is None
    assert attachment["estimated_tokens"] is None

    key = attachment["attachment_key"]
    fetched = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["attachment"]["attachment_key"] == key

    listed = client.get(f"{_SESSIONS_URL}/{session_key}/attachments").json()
    assert listed["count"] == 1
    assert listed["total_bytes"] == len(PDF_BYTES)


def test_download_returns_the_bytes_unchanged(client: TestClient) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert resp.status_code == 200, resp.text
    assert resp.content == PDF_BYTES


def test_the_same_bytes_twice_return_the_same_attachment(client: TestClient) -> None:
    """A dedupe hit, not a conflict.

    Someone who pressed the button twice, or whose connection dropped after the
    write, wants their file - not an error explaining that they already have it.
    """
    session_key = make_session()

    first = upload(client, session_key).json()
    second = upload(client, session_key, filename="renamed.pdf").json()

    assert second["deduplicated"] is True
    assert second["attachment"]["attachment_key"] == first["attachment"]["attachment_key"]
    # The first name wins: the row was not rewritten, so nothing about the
    # stored file changed because a second caller called it something else.
    assert second["attachment"]["display_name"] == "spec.pdf"
    assert client.get(f"{_SESSIONS_URL}/{session_key}/attachments").json()["count"] == 1


def test_the_same_bytes_on_two_sessions_are_two_attachments(client: TestClient) -> None:
    """Content uniqueness is per session, and that is a privacy decision.

    Per namespace, a dedupe hit would tell a caller in a shared namespace that
    somebody else had already uploaded a given file, which is a content oracle
    over a hash.
    """
    first_session = make_session()
    second_session = make_session()

    first = upload(client, first_session).json()
    second = upload(client, second_session).json()

    assert second["deduplicated"] is False
    assert second["attachment"]["attachment_key"] != first["attachment"]["attachment_key"]


# ---------------------------------------------------------------------------
# The type gate
# ---------------------------------------------------------------------------


def test_a_zip_declared_as_a_pdf_is_refused_as_a_zip(client: TestClient) -> None:
    session_key = make_session()

    resp = upload(
        client, session_key, data=ZIP_BYTES, filename="deck.pdf", content_type="application/pdf"
    )

    assert resp.status_code == 415, resp.text
    body = resp.json()
    assert body["error_code"] == "ATTACHMENT_REJECTED"
    # Both types named. Being told only "rejected" leaves a caller with nothing
    # to act on, and the useful sentence here is the export advice.
    assert "application/zip" in body["detail"]
    assert "application/pdf" in body["detail"]
    assert "PDF" in body["hint"]


def test_a_type_outside_the_accepted_set_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_accepted_mimes", {"application/pdf"})
    session_key = make_session()

    resp = upload(client, session_key, data=PNG_BYTES, content_type="image/png")

    assert resp.status_code == 415, resp.text
    assert "image/png" in resp.json()["detail"]


def test_an_unrecognized_body_is_refused(client: TestClient) -> None:
    """A body no magic number matches is refused rather than guessed at.

    A text-shaped fallback would make the sniff a suggestion, and the whole
    point of sniffing is that it is the thing that decides.
    """
    session_key = make_session()

    resp = upload(client, session_key, data=b"just some words", content_type="application/pdf")

    assert resp.status_code == 415, resp.text
    body = resp.json()
    assert "unrecognized" in body["detail"]
    # Markdown, CSV and plain text all land here, and section 9 of the plan
    # names one remedy for them. A list of accepted types tells somebody
    # holding a .md file what they cannot do and not what they can.
    assert "paste it into the message" in body["hint"]


def test_a_declared_type_that_contradicts_the_bytes_is_recorded(client: TestClient) -> None:
    session_key = make_session()

    resp = upload(client, session_key, data=PNG_BYTES, content_type="application/pdf")

    assert resp.status_code == 201, resp.text
    attachment = resp.json()["attachment"]
    assert attachment["sniffed_mime"] == "image/png"
    assert attachment["declared_mime"] == "application/pdf"
    assert attachment["mime_mismatch"] is True


# ---------------------------------------------------------------------------
# Size and shape
# ---------------------------------------------------------------------------


def test_a_body_over_the_cap_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_max_bytes", 128)
    session_key = make_session()

    resp = upload(client, session_key, data=PDF_BYTES + b"\x00" * 4096)

    assert resp.status_code == 413, resp.text
    assert resp.json()["error_code"] == "ATTACHMENT_TOO_LARGE"


def test_an_oversize_upload_leaves_no_row_and_no_blob(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof by absence: the 413 is returned by a correct implementation and by
    one that writes first and rolls back badly, so the status says nothing."""
    monkeypatch.setattr(executor_settings, "attachment_max_bytes", 128)
    session_key = make_session()

    upload(client, session_key, data=PDF_BYTES + b"\x00" * 4096)

    with engine.begin() as conn:
        attachments = conn.execute(
            text("SELECT count(*) FROM agent_session_attachments")
        ).scalar()
        blobs = conn.execute(
            text("SELECT count(*) FROM agent_session_attachment_blobs")
        ).scalar()
    assert attachments == 0
    assert blobs == 0


def test_a_request_with_no_content_length_is_refused(client: TestClient) -> None:
    """A body whose size cannot be checked in advance is one whose size the
    sender declined to declare. Streaming the request omits the header."""
    session_key = make_session()

    def chunks():
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.pdf\"\r\n"
        yield b"Content-Type: application/pdf\r\n\r\n"
        yield PDF_BYTES
        yield b"\r\n--b--\r\n"

    resp = client.post(
        f"{_SESSIONS_URL}/{session_key}/attachments",
        content=chunks(),
        headers={**_XHR, "Content-Type": "multipart/form-data; boundary=b"},
    )

    assert resp.status_code == 413, resp.text
    assert "did not declare its length" in resp.json()["detail"]


def test_a_zero_byte_file_is_400(client: TestClient) -> None:
    session_key = make_session()

    resp = upload(client, session_key, data=b"")

    assert resp.status_code == 400, resp.text


def test_the_upload_route_requires_x_requested_with(client: TestClient) -> None:
    """The CSRF control, and it is a header rather than a token on purpose.

    ``multipart/form-data`` is the one content type a cross-origin HTML form
    can send with no preflight. Requiring a custom header forces one regardless
    of cookie policy.
    """
    session_key = make_session()

    resp = upload(client, session_key, headers={})

    assert resp.status_code == 400, resp.text
    assert "X-Requested-With" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_a_hostile_filename_is_normalized_and_flagged(client: TestClient) -> None:
    session_key = make_session()

    resp = upload(
        client,
        session_key,
        declared_name='../../etc/passwd" | source=operator‮.pdf',
    )

    attachment = resp.json()["attachment"]
    assert attachment["display_name_normalized"] is True
    assert '"' not in attachment["display_name"]
    assert "|" not in attachment["display_name"]
    assert "/" not in attachment["display_name"]
    assert "‮" not in attachment["display_name"]


def test_a_name_that_normalizes_to_nothing_gets_a_boring_one(client: TestClient) -> None:
    session_key = make_session()

    resp = upload(client, session_key, declared_name="‮‭​")

    attachment = resp.json()["attachment"]
    assert attachment["display_name"] == "attachment"
    assert attachment["display_name_normalized"] is True


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


def test_the_per_session_count_ceiling_refuses_with_the_number(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_max_per_session", 1)
    session_key = make_session()
    upload(client, session_key)

    resp = upload(client, session_key, data=PNG_BYTES, content_type="image/png")

    assert resp.status_code == 413, resp.text
    assert resp.json()["error_code"] == "QUOTA_EXCEEDED"
    assert "1 attachments" in resp.json()["detail"]


def test_the_per_session_byte_ceiling_refuses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_session_total_bytes", len(PDF_BYTES))
    session_key = make_session()
    upload(client, session_key)

    resp = upload(client, session_key, data=PNG_BYTES, content_type="image/png")

    assert resp.status_code == 413, resp.text
    assert resp.json()["error_code"] == "QUOTA_EXCEEDED"


def test_the_namespace_byte_ceiling_refuses_across_sessions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_namespace_total_bytes", len(PDF_BYTES))
    upload(client, make_session())

    resp = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert resp.status_code == 413, resp.text
    assert "namespace" in resp.json()["detail"]


def test_the_upload_rate_limiter_returns_429_with_retry_after(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_uploads_per_minute", 1)
    reset_attachment_quota()
    session_key = make_session()
    assert upload(client, session_key).status_code == 201

    resp = upload(client, session_key, data=PNG_BYTES, content_type="image/png")

    assert resp.status_code == 429, resp.text
    assert resp.json()["error_code"] == "QUOTA_EXCEEDED"
    # The number as well as the sentence: a client regexing an English hint
    # breaks the first time somebody rewords it.
    assert int(resp.headers["Retry-After"]) >= 0


def test_the_per_namespace_hourly_ceiling_refuses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_uploads_per_namespace_hour", 1)
    upload(client, make_session())

    resp = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert resp.status_code == 413, resp.text
    assert "in the last hour" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_removes_the_bytes_and_keeps_the_record(client: TestClient) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]

    resp = client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True
    assert "executor" in resp.json()["notice"]

    fetched = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").json()
    assert fetched["attachment"]["status"] == "tombstoned"
    assert fetched["attachment"]["size_bytes"] == len(PDF_BYTES)
    assert fetched["attachment"]["source_sha256"]

    with engine.begin() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM agent_session_attachment_blobs")).scalar()
            == 0
        )


def test_downloading_a_tombstone_says_so_rather_than_404(client: TestClient) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert resp.status_code == 410, resp.text
    assert "reclaimed" in resp.json()["detail"]


def test_a_tombstone_stops_counting_against_the_session_total(client: TestClient) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")

    listed = client.get(f"{_SESSIONS_URL}/{session_key}/attachments").json()

    assert listed["count"] == 1
    assert listed["total_bytes"] == 0


def test_deleting_an_attachment_bound_to_the_running_turn_is_409(
    client: TestClient,
) -> None:
    trace_id = uuid.uuid4().hex
    session_key = make_session(in_flight_trace_id=trace_id)
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    with engine.begin() as conn:
        session_id, attachment_id = conn.execute(
            text(
                "SELECT s.id, a.id FROM agent_sessions s "
                "  JOIN agent_session_attachments a ON a.session_id = s.id "
                " WHERE s.session_key = :key"
            ),
            {"key": session_key},
        ).one()
        conn.execute(
            text(
                "INSERT INTO agent_turn_attachments "
                "(namespace_key, session_id, trace_id, attachment_id, position, verdict) "
                "VALUES ('default', :sid, :trace, :aid, 0, 'sent')"
            ),
            {"sid": session_id, "trace": trace_id, "aid": attachment_id},
        )

    resp = client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "TURN_IN_FLIGHT"


# ---------------------------------------------------------------------------
# Not found, and the feature switch
# ---------------------------------------------------------------------------


def test_an_unknown_session_is_404(client: TestClient) -> None:
    resp = upload(client, uuid.uuid4().hex)

    assert resp.status_code == 404, resp.text


def test_an_attachment_key_from_another_session_is_404(client: TestClient) -> None:
    """Scoped by session as well as by key, so a key that exists elsewhere in
    the namespace is indistinguishable from one that does not exist at all."""
    key = upload(client, make_session()).json()["attachment"]["attachment_key"]
    other_session = make_session()

    resp = client.get(f"{_SESSIONS_URL}/{other_session}/attachments/{key}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "ATTACHMENT_NOT_FOUND"


def test_every_route_is_503_while_attachments_are_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_key = make_session()
    monkeypatch.setattr(executor_settings, "attachments_enabled", False)

    assert upload(client, session_key).status_code == 503
    assert client.get(f"{_SESSIONS_URL}/{session_key}/attachments").status_code == 503


def test_every_route_is_503_while_the_executor_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_key = make_session()
    monkeypatch.setattr(executor_settings, "enabled", False)

    assert upload(client, session_key).status_code == 503
    assert client.get(f"{_SESSIONS_URL}/{session_key}/attachments").status_code == 503


def test_a_filename_never_reaches_the_log_at_info(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Content is never logged above DEBUG, and a filename is content.

    Asserted by absence over everything captured rather than by inspecting one
    call site, because the rule has to survive somebody adding a helpful
    ``logger.info`` to any of the five routes.
    """
    session_key = make_session()
    secret_name = "merger-terms-acme-2026.pdf"

    with caplog.at_level("INFO"):
        key = upload(client, session_key, declared_name=secret_name).json()[
            "attachment"
        ]["attachment_key"]
        client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")
        client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")
        client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")

    assert secret_name not in caplog.text
    assert "merger-terms" not in caplog.text
