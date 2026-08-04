"""The decision routes under both auth providers. Plan 5.7 step 2 and 4.7.

Under ``HeaderAuthProvider`` the self-approval refusal must bind to the
plan's full width - the claiming credential *and* the ``created_by_hash`` of
any session belonging to the task - while a third, uninvolved credential
proves the refusal is scoped rather than general, and a plain non-admin key
proves ``agent_tasks.approve`` sits at AUTHENTICATED rather than ADMIN.

Under ``NoAuthProvider`` nobody has an identity, so the comparison cannot
bind: it must be skipped rather than refusing everyone on ``None == None``,
and skipped *out loud* - silently allowing everyone would look identical to
the invariant holding.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.auth import AuthenticatedClient, AuthLevel
from agent_control_server.config import auth_settings
from agent_control_server.services.caller_identity import hash_caller_id

from .conftest import TEST_ADMIN_API_KEY, TEST_API_KEY
from .test_agent_task_review_gate import (
    TASKS_URL,
    RecordingLinearClient,
    _accept,
    _commit_linear,
    _entry,
    _ref,
    _reject,
    _rows,
    _run_to_completion,
    linear,
)

__all__ = ["linear"]  # re-exported fixture

THIRD_API_KEY = "test-third-key-67890"


def _caller_hash(api_key: str) -> str:
    """The hash the routes record for this key, derived the same way they
    derive it: through ``AuthenticatedClient.key_id``, not a bare prefix."""
    client = AuthenticatedClient(api_key=api_key, is_admin=False, auth_level=AuthLevel.API_KEY)
    hashed = hash_caller_id(client.key_id)
    assert hashed is not None
    return hashed


# ---------------------------------------------------------------------------
# Self-approval, to the plan's full width, under the header provider
# ---------------------------------------------------------------------------


def test_a_credential_that_opened_a_session_for_the_task_may_not_decide(
    non_admin_client: TestClient,
    admin_client: TestClient,
    app: Any,
    linear: RecordingLinearClient,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5.7 step 2 names two sets: ``claimed_by_hash`` *and* the
    ``created_by_hash`` on any session belonging to the task. The admin key
    here never claimed anything - it only shows up on a session row - and
    must still be refused, while a third key involved in neither accepts."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(non_admin_client, key)
    admin_hash = _caller_hash(TEST_ADMIN_API_KEY)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_sessions (namespace_key, session_key, agent_name, "
                "executor_app_name, executor_user_id, executor_session_id, "
                "agent_task_id, created_by_hash) "
                "SELECT t.namespace_key, :skey, 'gatecheck_agent', 'app', "
                ":skey, :skey, t.id, :hash FROM agent_tasks t WHERE t.task_key = :key"
            ),
            {"skey": uuid.uuid4().hex, "hash": admin_hash, "key": key},
        )
    entry = _entry(admin_client, key)

    refused_accept = _accept(admin_client, key, entry)
    assert refused_accept.status_code == 409, refused_accept.text
    assert refused_accept.json()["error_code"] == "SELF_APPROVAL_REFUSED"

    refused_reject = _reject(admin_client, key, entry)
    assert refused_reject.status_code == 409, refused_reject.text
    assert refused_reject.json()["error_code"] == "SELF_APPROVAL_REFUSED"
    assert linear.state_updates == [], "a refused decision reached Linear"

    # A third credential, involved in neither running nor sessions, may decide.
    monkeypatch.setattr(auth_settings, "api_keys", f"{TEST_API_KEY},{THIRD_API_KEY}")
    cached = ("_parsed_api_keys", "_parsed_admin_api_keys", "_all_valid_keys", "_all_admin_keys")
    for attr in cached:
        auth_settings.__dict__.pop(attr, None)
    third = TestClient(app, headers={"X-API-Key": THIRD_API_KEY})
    assert _caller_hash(THIRD_API_KEY) not in {admin_hash, _caller_hash(TEST_API_KEY)}

    accepted = _accept(third, key, entry)
    assert accepted.status_code == 200, accepted.text
    assert len(linear.state_updates) == 1


def test_review_and_reject_require_a_credential_under_the_header_provider(
    unauthenticated_client: TestClient, linear: RecordingLinearClient
) -> None:
    assert unauthenticated_client.get(f"{TASKS_URL}/review").status_code == 401
    response = unauthenticated_client.post(
        f"{TASKS_URL}/{'a' * 32}/reject", json={"writeback_id": 1}
    )
    assert response.status_code == 401, response.text


def test_a_non_admin_key_may_accept_work_it_did_not_run(
    non_admin_client: TestClient,
    admin_client: TestClient,
    linear: RecordingLinearClient,
) -> None:
    """``agent_tasks.approve`` sits at AUTHENTICATED, not ADMIN: if approving
    needed an admin, one admin would approve everything and read nothing."""
    key = _commit_linear(admin_client, _ref())
    _run_to_completion(admin_client, key)
    entry = _entry(non_admin_client, key)

    response = _accept(non_admin_client, key, entry)

    assert response.status_code == 200, response.text
    assert len(linear.state_updates) == 1


# ---------------------------------------------------------------------------
# NoAuthProvider: skipped, not refused, and not silent
# ---------------------------------------------------------------------------


def _no_auth_client(app: Any) -> TestClient:
    from agent_control_server.auth_framework.core import set_authorizer
    from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider

    set_authorizer(NoAuthProvider())
    return TestClient(app)


def test_accept_under_no_auth_warns_that_the_refusal_cannot_bind(
    client: TestClient,
    linear: RecordingLinearClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skipping the comparison must be loud: allowing everyone *silently*
    would look identical to the invariant holding."""
    anonymous = _no_auth_client(client.app)
    key = _commit_linear(anonymous, _ref())
    _run_to_completion(anonymous, key)
    entry = _entry(anonymous, key)

    review_logger = "agent_control_server.services.agent_task_review"
    with caplog.at_level(logging.WARNING, logger=review_logger):
        response = _accept(anonymous, key, entry)

    assert response.status_code == 200, response.text
    assert any(
        "credential checks are disabled" in record.getMessage()
        for record in caplog.records
    ), "the skipped comparison left no warning"


def test_reject_under_no_auth_proceeds_because_nothing_has_an_identity(
    client: TestClient, linear: RecordingLinearClient, db_engine: Any
) -> None:
    anonymous = _no_auth_client(client.app)
    key = _commit_linear(anonymous, _ref())
    _run_to_completion(anonymous, key)
    entry = _entry(anonymous, key)

    response = _reject(anonymous, key, entry, reason="read it, not convinced")

    assert response.status_code == 200, response.text
    proposal = [r for r in _rows(db_engine, key) if r["kind"] == "status_change"]
    assert [r["status"] for r in proposal] == ["rejected"]
    assert linear.state_updates == [], "a rejection closed the issue"
    assert len(linear.comments) == 1, "only the step's own comment was ever posted"
