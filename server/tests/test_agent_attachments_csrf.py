"""The two things standing between a cross-origin form and a victim's session.

The upload route is the first in this server to accept ``multipart/form-data``,
which is the one content type a cross-origin HTML form can send with no
preflight. Two controls cover it and neither is visible from the other's file,
which is why they are asserted together here.

**The cookie is ``samesite=lax``.** That is what stops the form-post today, and
nothing recorded the dependency until this test. Anyone loosening it to
``none`` - for an iframe embed, for a subdomain console - would open
cross-origin file injection into a logged-in operator's conversation, silently.
This fails loudly instead.

**The route requires a custom header.** Belt as well as braces, because it holds
regardless of cookie policy: a custom request header forces a preflight, and a
cross-origin form cannot send one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import TEST_API_KEY
from .test_agent_attachments_endpoints import PDF_BYTES, make_session, upload

_SESSIONS_URL = "/api/v1/agent-sessions"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def test_the_session_cookie_is_still_samesite_lax(app: object) -> None:
    """The assumption the whole CSRF posture rests on, asserted so it cannot
    change quietly.

    Addressed as ``localhost`` because login over plain HTTP is refused
    anywhere else, and the cookie attributes are the subject here rather than
    the transport.
    """
    localhost_client = TestClient(app, base_url="http://localhost")

    resp = localhost_client.post("/api/login", json={"api_key": TEST_API_KEY})

    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers["set-cookie"].lower()
    assert "samesite=lax" in set_cookie
    assert "httponly" in set_cookie


def test_a_form_post_with_no_custom_header_is_refused(client: TestClient) -> None:
    """What a cross-origin HTML form can actually send: a multipart body with
    no headers of its own choosing."""
    session_key = make_session()

    resp = client.post(
        f"{_SESSIONS_URL}/{session_key}/attachments",
        files={"file": ("spec.pdf", PDF_BYTES, "application/pdf")},
        data={"declared_name": "spec.pdf"},
    )

    assert resp.status_code == 400, resp.text
    assert "X-Requested-With" in resp.json()["detail"]


def test_the_same_post_with_the_header_succeeds(client: TestClient) -> None:
    """The refusal above must be about the header and nothing else, or it is
    testing that uploads are broken."""
    assert upload(client, make_session()).status_code == 201
