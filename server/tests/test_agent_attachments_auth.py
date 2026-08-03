"""Who may put bytes into somebody's conversation.

Three questions, and answering one says nothing about the others.

*Which tier.* ``agent_attachments.write`` sits at AUTHENTICATED. Nothing else
pins that - ``test_auth_framework`` asserts every operation has an entry, not
what the entry says - so a tier relaxed in a one-line diff would otherwise pass
the whole suite.

*Which operation.* An admin key satisfies every tier at once, so an admin-only
test cannot tell the write operation from the content-read one. The denying
authorizer refuses exactly one and allows the rest.

*Which session.* The half that matters most, and the half a suite that seeds
``created_by_hash`` explicitly cannot see at all. **Every case below runs under
both providers**, because the default one leaves that column NULL on every row
and the two rules the write path adds are precisely the ones that behave
differently depending on whether attribution was possible.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_control_models.errors import ErrorCode
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.auth_framework import (
    Operation,
    Principal,
    set_authorizer,
)
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider
from agent_control_server.config import executor_settings
from agent_control_server.errors import ForbiddenError
from agent_control_server.services.attachment_quota import reset_attachment_quota
from agent_control_server.services.caller_identity import hash_caller_id

from .conftest import TEST_API_KEY, engine
from .test_agent_attachments_endpoints import (
    PDF_BYTES,
    PNG_BYTES,
    make_session,
    upload,
)

_SESSIONS_URL = "/api/v1/agent-sessions"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


class DenyingAuthorizer:
    """Admin for every operation except the ones it was told to refuse."""

    def __init__(self, *denied: Operation) -> None:
        self._denied = frozenset(denied)
        self.seen: list[Operation] = []

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del request, context
        self.seen.append(operation)
        if operation in self._denied:
            raise ForbiddenError(
                error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
                detail=f"{operation.value} denied",
            )
        return Principal(namespace_key="default", is_admin=True)


# ---------------------------------------------------------------------------
# The tier and the operation
# ---------------------------------------------------------------------------


def test_the_write_operation_is_registered_and_authenticated() -> None:
    """An unregistered operation makes the server refuse to start, which is the
    guard this inherits. What it adds is the tier, which nothing else pins."""
    assert (
        DEFAULT_OPERATION_ACCESS[Operation.AGENT_ATTACHMENTS_WRITE]
        is AccessLevel.AUTHENTICATED
    )


def test_upload_and_delete_depend_on_the_write_operation(client: TestClient) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    authorizer = DenyingAuthorizer(Operation.AGENT_ATTACHMENTS_WRITE)
    set_authorizer(authorizer)

    assert upload(client, session_key, data=PNG_BYTES, content_type="image/png").status_code == 403
    assert client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").status_code == 403
    assert Operation.AGENT_ATTACHMENTS_WRITE in authorizer.seen


def test_the_three_reads_depend_on_the_content_read_operation(client: TestClient) -> None:
    """Reads sit with the transcript, not with a second attachment operation.

    Minting ``agent_attachments.read`` beside ``agent_sessions.content_read``
    would document a boundary that does not exist: an attachment's name and its
    bytes are the same sensitivity class as the conversation they appear in.
    """
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSION_CONTENT_READ))

    assert client.get(f"{_SESSIONS_URL}/{session_key}/attachments").status_code == 403
    assert client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").status_code == 403
    assert (
        client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content").status_code
        == 403
    )


def test_a_read_still_works_when_only_the_write_operation_is_denied(
    client: TestClient,
) -> None:
    """The split is real in both directions, which one denial alone cannot show."""
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    set_authorizer(DenyingAuthorizer(Operation.AGENT_ATTACHMENTS_WRITE))

    assert client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").status_code == 200


# ---------------------------------------------------------------------------
# Creator scoping, under a provider that resolves callers
# ---------------------------------------------------------------------------


def _non_admin_hash() -> str:
    """The hash the non-admin test key resolves to under the header provider.

    ``key_id`` is the first eight characters and an ellipsis, never the key, so
    the stored hash is over that prefix rather than over the credential.
    """
    return hash_caller_id(TEST_API_KEY[:8] + "...")


def test_a_non_creator_cannot_upload_into_somebody_elses_session(
    non_admin_client: TestClient,
) -> None:
    session_key = make_session(created_by_hash=hash_caller_id("someone-else"))

    resp = upload(non_admin_client, session_key)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "AUTH_INSUFFICIENT_PRIVILEGES"


def test_the_creator_can_upload_into_their_own_session(
    non_admin_client: TestClient,
) -> None:
    session_key = make_session(created_by_hash=_non_admin_hash())

    resp = upload(non_admin_client, session_key)

    assert resp.status_code == 201, resp.text


def test_an_admin_can_upload_into_anybody_s_session(client: TestClient) -> None:
    session_key = make_session(created_by_hash=hash_caller_id("someone-else"))

    assert upload(client, session_key).status_code == 201


# ---------------------------------------------------------------------------
# The two call-site conditions, under both providers
#
# These are the cases the plan's corrections exist for, and each one behaves
# differently depending on whether the provider resolves a caller. Running them
# under one provider proves half of each.
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_auth_client(app: Any) -> TestClient:
    """A client under the provider a default deployment actually runs.

    ``NoAuthProvider`` authorizes every operation and leaves ``caller_id``
    None, so ``hash_caller_id(None)`` is None and no session it opens carries a
    creator. That is the deployment this feature is developed on, so the rules
    below have to be correct in it and not only in a configured one.
    """
    set_authorizer(NoAuthProvider())
    return TestClient(app, raise_server_exceptions=True)


def test_under_no_auth_an_unattributed_session_still_accepts_an_upload(
    no_auth_client: TestClient,
) -> None:
    """The case that would otherwise 403 the whole feature on the machine it is
    built on.

    A rule refusing every NULL-creator upload is the obvious way to close the
    unattributed-session hole, and it is wrong: under the default provider
    *every* session has a NULL creator, so it would refuse every upload in the
    deployment. The rule fires only when attribution was possible.
    """
    session_key = make_session(created_by_hash=None)

    resp = upload(no_auth_client, session_key)

    assert resp.status_code == 201, resp.text


def test_under_a_resolving_provider_an_unattributed_session_refuses_an_upload(
    non_admin_client: TestClient,
) -> None:
    """The other half. A provider that resolves callers and a session with no
    creator means attribution was possible and did not happen, which is worth
    refusing rather than writing into."""
    session_key = make_session(created_by_hash=None)

    resp = upload(non_admin_client, session_key)

    assert resp.status_code == 403, resp.text
    assert "cannot be attributed" in resp.json()["detail"]


def test_a_dispatch_task_session_refuses_a_bystander_under_no_auth(
    no_auth_client: TestClient,
) -> None:
    """The condition that does not depend on attribution at all.

    ``require_content_access`` returns early on a NULL creator, *before* its
    dispatch-task branch, so under this provider the shared predicate does not
    refuse this. Checking ``agent_task_id`` directly is what makes "files reach
    a task session through the path that opened it, never a bystander" true
    under every provider rather than only under a configured one.
    """
    session_key = make_session(created_by_hash=None, agent_task_id=4242)

    resp = upload(no_auth_client, session_key)

    assert resp.status_code == 403, resp.text
    assert "dispatch task" in resp.json()["detail"]


def test_a_dispatch_task_session_refuses_a_bystander_under_the_header_provider(
    non_admin_client: TestClient,
) -> None:
    session_key = make_session(
        created_by_hash=hash_caller_id("the-dispatcher"), agent_task_id=4242
    )

    resp = upload(non_admin_client, session_key)

    assert resp.status_code == 403, resp.text
    assert "dispatch task" in resp.json()["detail"]


def test_the_holder_of_a_dispatch_task_session_may_still_upload(
    non_admin_client: TestClient,
) -> None:
    """Refusing bystanders must not refuse the process that opened the session,
    which is how a tracker's files reach a task at all."""
    session_key = make_session(created_by_hash=_non_admin_hash(), agent_task_id=4242)

    assert upload(non_admin_client, session_key).status_code == 201


def test_reading_a_task_session_s_attachments_is_still_open_to_oversight(
    non_admin_client: TestClient, client: TestClient
) -> None:
    """Oversight of a task's session is read, halt and nudge; driving it is the
    holder's. That asymmetry is the whole reason the write path adds its own
    conditions instead of loosening the shared predicate."""
    session_key = make_session(
        created_by_hash=hash_caller_id("the-dispatcher"), agent_task_id=4242
    )
    upload(client, session_key)

    resp = non_admin_client.get(f"{_SESSIONS_URL}/{session_key}/attachments")

    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# Resurrecting a tombstone is a write, and is authorized as one
# ---------------------------------------------------------------------------


def _tombstoned_attachment(client: TestClient, session_key: str) -> str:
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    assert (
        client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").status_code
        == 200
    )
    return key


def _blob_count(attachment_key: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM agent_session_attachment_blobs b "
                    "  JOIN agent_session_attachments a ON a.id = b.attachment_id "
                    " WHERE a.attachment_key = :key"
                ),
                {"key": attachment_key},
            ).scalar()
            or 0
        )


def test_a_bystander_cannot_resurrect_somebody_elses_tombstone(
    client: TestClient, non_admin_client: TestClient
) -> None:
    """Proof by absence, because the 403 alone does not distinguish a refusal
    from a refusal that wrote the bytes first.

    Re-uploading known bytes against a tombstone is a way to test whether a
    given file was ever in somebody else's conversation, and it is a way to put
    bytes back into it. Both are refused at the same gate as a first upload.
    """
    session_key = make_session(created_by_hash=hash_caller_id("someone-else"))
    key = _tombstoned_attachment(client, session_key)

    resp = upload(non_admin_client, session_key)

    assert resp.status_code == 403, resp.text
    assert _blob_count(key) == 0


def test_a_bystander_cannot_resurrect_a_task_sessions_tombstone_under_no_auth(
    client: TestClient, no_auth_client: TestClient
) -> None:
    """The same under the provider a default deployment runs, where there is no
    caller identity to scope by and ``agent_task_id`` is the whole rule."""
    session_key = make_session(created_by_hash=None)
    key = _tombstoned_attachment(no_auth_client, session_key)

    resp = upload(no_auth_client, session_key)

    # No task, no creator: this deployment is one trust domain, so it succeeds
    # and the bytes come back. Recorded here so that a later tightening of the
    # rule cannot quietly break the default deployment.
    assert resp.status_code == 201, resp.text
    assert _blob_count(key) == 1


def test_the_creator_may_resurrect_their_own_tombstone(
    non_admin_client: TestClient,
) -> None:
    session_key = make_session(created_by_hash=_non_admin_hash())
    key = _tombstoned_attachment(non_admin_client, session_key)

    resp = upload(non_admin_client, session_key)

    assert resp.status_code == 201, resp.text
    assert resp.json()["attachment"]["status"] == "ready"
    assert _blob_count(key) == 1


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


class NamespaceAuthorizer:
    """Maps ``X-Test-Namespace`` onto the principal, admin everywhere."""

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


def test_a_session_in_another_namespace_is_404_not_403(app: Any) -> None:
    """Whether a session exists elsewhere is not this caller's business, and the
    answer has to be the same either way."""
    set_authorizer(NamespaceAuthorizer())
    session_key = make_session(namespace_key="tenant-a")
    intruder = TestClient(
        app, raise_server_exceptions=True, headers={"X-Test-Namespace": "tenant-b"}
    )

    assert upload(intruder, session_key).status_code == 404
    assert intruder.get(f"{_SESSIONS_URL}/{session_key}/attachments").status_code == 404


def test_a_cross_namespace_download_is_404(app: Any) -> None:
    set_authorizer(NamespaceAuthorizer())
    session_key = make_session(namespace_key="tenant-a")
    owner = TestClient(
        app, raise_server_exceptions=True, headers={"X-Test-Namespace": "tenant-a"}
    )
    key = upload(owner, session_key).json()["attachment"]["attachment_key"]
    intruder = TestClient(
        app, raise_server_exceptions=True, headers={"X-Test-Namespace": "tenant-b"}
    )

    resp = intruder.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert resp.status_code == 404, resp.text
    assert PDF_BYTES not in resp.content


def test_an_unauthenticated_caller_is_refused(
    unauthenticated_client: TestClient,
) -> None:
    session_key = make_session()

    assert upload(unauthenticated_client, session_key).status_code == 401
