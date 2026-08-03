"""The download route's headers, which are where the XSS control actually lives.

This is a server test rather than a UI test on purpose. A browser decides
whether to render or to save from the response headers and nothing else, so a
console that "never renders attachments" is a claim about a client that a
different client does not have to honour. The header is the control.

The accepted-type gate is not the control either. It happens to refuse HTML
today, and a later phase widening the set - to ``text/csv``, say, or to
``image/svg+xml`` - must not silently turn every stored file into stored
cross-site scripting against the console's own origin. So the bytes below are
inserted straight into the table, past the gate, which is exactly the state a
widened gate or a direct database write would produce.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import engine
from .test_agent_attachments_endpoints import make_session

_SESSIONS_URL = "/api/v1/agent-sessions"

HOSTILE_HTML = b"<script>alert(document.cookie)</script>"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def _insert_attachment(
    session_key: str, *, display_name: str, data: bytes, mime: str
) -> str:
    """Write one attachment directly, past every gate the route applies."""
    attachment_key = uuid.uuid4().hex
    digest = hashlib.sha256(data).hexdigest()
    with engine.begin() as conn:
        session_id = conn.execute(
            text("SELECT id FROM agent_sessions WHERE session_key = :key"),
            {"key": session_key},
        ).scalar_one()
        attachment_id = conn.execute(
            text(
                "INSERT INTO agent_session_attachments "
                "(namespace_key, session_id, attachment_key, display_name, "
                " display_name_normalized, original_name_sha256, declared_mime, "
                " sniffed_mime, size_bytes, source_sha256, status, origin) "
                "VALUES ('default', :sid, :key, :name, false, :namehash, :mime, "
                "        :mime, :size, :sha, 'ready', 'operator_upload') "
                "RETURNING id"
            ),
            {
                "sid": session_id,
                "key": attachment_key,
                "name": display_name,
                "namehash": digest,
                "mime": mime,
                "size": len(data),
                "sha": digest,
            },
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO agent_session_attachment_blobs "
                "(namespace_key, attachment_id, variant, content_type, size_bytes, sha256, data) "
                "VALUES ('default', :aid, 'original', :mime, :size, :sha, :data)"
            ),
            {
                "aid": attachment_id,
                "mime": mime,
                "size": len(data),
                "sha": digest,
                "data": data,
            },
        )
    return attachment_key


def test_html_is_served_as_an_octet_stream_download(client: TestClient) -> None:
    session_key = make_session()
    key = _insert_attachment(
        session_key, display_name="payload.html", data=HOSTILE_HTML, mime="text/html"
    )

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert resp.headers["x-content-type-options"] == "nosniff"
    # The stored type never reaches the wire. A browser that trusted it would
    # render this file on the console's own origin.
    assert "text/html" not in resp.headers["content-type"]


def test_a_quote_and_a_crlf_in_the_name_cannot_open_a_header(
    client: TestClient,
) -> None:
    """Header injection, closed twice.

    Normalization already removes these characters on the way in. This asserts
    the second place, because an injection reachable through a single
    normalization bug is not a risk worth carrying for one line of code.
    """
    session_key = make_session()
    key = _insert_attachment(
        session_key,
        display_name='evil"; filename="x.html\r\nSet-Cookie: a=b',
        data=HOSTILE_HTML,
        mime="text/html",
    )

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "Set-Cookie" not in resp.headers
    # Exactly one quoted parameter: the ASCII fallback. Anything else means the
    # supplied name closed it and opened another.
    assert disposition.count('"') == 2


def test_a_unicode_name_survives_as_rfc_5987(client: TestClient) -> None:
    """Defusing the name must not mean losing it. A person downloading a file
    called ``rapport-financier-é.pdf`` should get that filename."""
    session_key = make_session()
    key = _insert_attachment(
        session_key,
        display_name="rapport-financier-é.pdf",
        data=b"%PDF-1.7\n",
        mime="application/pdf",
    )

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert "filename*=UTF-8''rapport-financier-%C3%A9.pdf" in (
        resp.headers["content-disposition"]
    )
