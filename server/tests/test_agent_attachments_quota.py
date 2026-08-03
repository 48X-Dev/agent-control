"""The upload rate limiter, and the two claims about it nothing else asserts.

A stored-bytes ceiling is not a rate. Flooding fills the namespace total in
seconds and the retention window then holds it there for a fortnight, so the
byte quotas alone turn a burst into a lasting outage of the feature for everyone
in the namespace. That is what this limiter is for, and it is the only ceiling
in the feature that is per credential rather than per namespace.

Two properties below are load-bearing and neither is visible in a response body:
the bucket is a separate instance from the turn quota, and it is keyed on the
credential rather than on the session.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import (
    get_attachment_quota,
    reset_attachment_quota,
)
from agent_control_server.services.turn_quota import get_turn_quota

from .test_agent_attachments_endpoints import (
    PNG_BYTES,
    make_session,
    upload,
)


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def test_uploads_and_turns_do_not_share_a_bucket() -> None:
    """A turn spends model quota; an upload spends disk.

    Sharing one counter would let somebody who attached three files then fail to
    send the message those files were for, which is the worst possible place to
    put that refusal.
    """
    uploads = get_attachment_quota(max_per_minute=5)
    turns = get_turn_quota(max_per_minute=5)

    assert uploads is not turns


def test_reconfiguring_the_ceiling_rebuilds_the_bucket() -> None:
    """Otherwise a settings change is inert until the process restarts, and the
    operator raising it under pressure sees nothing happen."""
    first = get_attachment_quota(max_per_minute=5)

    second = get_attachment_quota(max_per_minute=9)

    assert second is not first
    assert second.max_per_minute == 9


def test_the_ceiling_follows_the_credential_not_the_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per session, a runaway loop opens a new session and carries on.

    The second upload here goes to a different session and is still refused,
    which is the assertion: the bucket is ``(namespace_key, caller_hash)``.
    """
    monkeypatch.setattr(executor_settings, "attachment_uploads_per_minute", 1)
    reset_attachment_quota()
    assert upload(client, make_session()).status_code == 201

    refused = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert refused.status_code == 429, refused.text


def test_the_retry_after_header_and_the_body_agree(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two readers, one number. A proxy or a generated client may act on the
    header without ever looking at the payload, and they must not disagree."""
    monkeypatch.setattr(executor_settings, "attachment_uploads_per_minute", 1)
    reset_attachment_quota()
    upload(client, make_session())

    refused = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert refused.status_code == 429, refused.text
    body = refused.json()
    assert body["error_code"] == "QUOTA_EXCEEDED"
    assert int(refused.headers["Retry-After"]) == body["details"]["retry_after_seconds"]


def test_a_rate_limited_upload_writes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof by absence: the limiter runs before the type gate and before any
    write, so a refusal here must leave the session exactly as it was."""
    monkeypatch.setattr(executor_settings, "attachment_uploads_per_minute", 1)
    reset_attachment_quota()
    session_key = make_session()
    upload(client, session_key)

    upload(client, session_key, data=PNG_BYTES, content_type="image/png")

    listed = client.get(f"/api/v1/agent-sessions/{session_key}/attachments").json()
    assert listed["count"] == 1
