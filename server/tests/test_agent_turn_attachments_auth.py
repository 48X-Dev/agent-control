"""Who may put a stored file in front of a model, under both providers.

The upload path has its own authorization file. This one is about the other
half, which is a different question with a different answer: an attachment that
is already stored is *delivered* by ``POST /turns``, and that route runs under
``agent_sessions.run`` and the shared content predicate rather than under
``agent_attachments.write``. A caller who cannot start a turn on a session
cannot read its documents into a model call either, and nothing about naming a
key may open a second door onto a conversation.

**Every case here runs under a provider that resolves callers and under the one
a default deployment actually runs.** ``api_key_enabled`` defaults false, so
``NoAuthProvider`` authorizes every operation, leaves ``caller_id`` None and
puts NULL in ``created_by_hash`` on every session it opens. A rule that is
correct only when attribution happened would either refuse this whole feature on
the machine it is developed on or wave through a bystander on the machine it is
deployed on, and a suite running one provider proves half of each.

**The refusals are asserted by absence.** A 403 is returned by a correct
implementation and by one that resolved the attachment, rendered its text and
then refused; the second would have put a stranger's document into this
process, into a log line and one edit away from a model call. So each refusal
asserts on the executor's recorded calls and on the binding table, never on the
status code alone.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_models.attachment_converter_cache import (
    conversion_cache_key,
)
from agent_control_models.errors import ErrorCode
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.auth_framework import (
    Operation,
    Principal,
    set_authorizer,
)
from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider
from agent_control_server.config import executor_settings
from agent_control_server.errors import ForbiddenError
from agent_control_server.services.attachment_quota import reset_attachment_quota
from agent_control_server.services.caller_identity import hash_caller_id
from agent_control_server.services.executor_factory import get_executor_client_factory
from agent_control_server.services.turn_quota import reset_turn_quota

from .conftest import TEST_API_KEY, engine
from .test_agent_attachments_endpoints import PDF_BYTES, upload
from .test_agent_session_turns import (
    _EXECUTOR_APP,
    _EXECUTOR_BASE_URL,
    FakeTurnExecutorFactory,
)

_SESSIONS_URL = "/api/v1/agent-sessions"
_RUNTIMES_URL = "/api/v1/agent-runtimes"

SECRET_TEXT = "THE MERGER CLOSES ON THE FOURTEENTH"
"""Unmistakable, so its absence is a claim about this document and not about
whether some string happened to be missing."""


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_turn_quota()
    reset_attachment_quota()
    yield
    reset_turn_quota()
    reset_attachment_quota()


@pytest.fixture()
def fake_executor(app: FastAPI) -> Any:
    factory = FakeTurnExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


@pytest.fixture()
def no_auth_client(app: FastAPI) -> TestClient:
    """A client under the provider a default deployment runs.

    Every operation authorized, no caller id, so every session it opens carries
    a NULL creator - the shape the rules below have to be correct in.
    """
    set_authorizer(NoAuthProvider())
    return TestClient(app, raise_server_exceptions=True)


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


def _non_admin_hash() -> str:
    """The hash the non-admin test key resolves to under the header provider."""
    return hash_caller_id(TEST_API_KEY[:8] + "...")


def _bound_agent(client: TestClient, *, headers: dict[str, str] | None = None) -> str:
    """Register an agent and bind it to an executor, returning its name."""
    agent_name = f"agent-{uuid.uuid4().hex[:12]}"
    registered = client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent_name,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
        headers=headers,
    )
    assert registered.status_code == 200, registered.text
    bound = client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
        headers=headers,
    )
    assert bound.status_code == 200, bound.text
    return agent_name


def _session_row(
    agent_name: str,
    *,
    namespace_key: str = "default",
    created_by_hash: str | None = None,
    agent_task_id: int | None = None,
) -> str:
    """Insert one runnable session against an already-bound agent.

    Written directly rather than through ``POST /agent-sessions`` because the
    creator column is the subject: the route derives it from whoever called,
    and these tests need a session whose owner is somebody other than the
    caller under test.
    """
    session_key = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_sessions "
                "(namespace_key, session_key, agent_name, executor_kind, "
                " executor_app_name, executor_user_id, executor_session_id, "
                " status, created_by_hash, agent_task_id) "
                "VALUES (:ns, :key, :agent, 'google_adk', :app, :user, :sid, "
                "        'active', :hash, :task)"
            ),
            {
                "ns": namespace_key,
                "key": session_key,
                "agent": agent_name,
                "app": _EXECUTOR_APP,
                "user": f"{namespace_key}:{uuid.uuid4().hex}",
                "sid": uuid.uuid4().hex,
                "hash": created_by_hash,
                "task": agent_task_id,
            },
        )
    return session_key


def _seed_conversion(source_sha256: str, body: str, *, namespace_key: str = "default") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_attachment_conversions "
                "(namespace_key, cache_key, source_sha256, state, status, "
                " text_body, text_chars, meaningful_chars) "
                "VALUES (:ns, :key, :sha, 'done', 'text_layer_extracted', "
                "        :body, :chars, :chars)"
            ),
            {
                "ns": namespace_key,
                "key": conversion_cache_key(source_sha256),
                "sha": source_sha256,
                "body": body,
                "chars": len(body),
            },
        )


def _readable_attachment(
    client: TestClient,
    session_key: str,
    *,
    text_body: str = SECRET_TEXT,
    namespace_key: str = "default",
    headers: dict[str, str] | None = None,
    data: bytes = PDF_BYTES,
) -> str:
    """Upload a file and pretend the background worker already read it."""
    kwargs: dict[str, Any] = {"data": data}
    if headers is not None:
        kwargs["headers"] = headers
    created = upload(client, session_key, **kwargs)
    assert created.status_code == 201, created.text
    attachment = created.json()["attachment"]
    _seed_conversion(attachment["source_sha256"], text_body, namespace_key=namespace_key)
    return str(attachment["attachment_key"])


def _turn(client: TestClient, session_key: str, keys: list[str], **kwargs: Any) -> Any:
    return client.post(
        f"{_SESSIONS_URL}/{session_key}/turns",
        json={"message": "Read this", "attachment_keys": keys},
        **kwargs,
    )


def _binding_count(session_key: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM agent_turn_attachments b "
                    "  JOIN agent_sessions s ON s.id = b.session_id "
                    "   AND s.namespace_key = b.namespace_key "
                    " WHERE s.session_key = :key"
                ),
                {"key": session_key},
            ).scalar()
            or 0
        )


def _nothing_was_delivered(factory: Any, *, session_key: str) -> None:
    """The absence assertion these refusals are actually about.

    Over every recorded call rather than the last one: "the run we looked at
    did not carry it" is a weaker claim than "no run did", and a delivery path
    that resolved the document before refusing would satisfy the first.
    """
    assert factory.runs == []
    assert SECRET_TEXT not in repr(factory.runs)
    assert _binding_count(session_key) == 0


# ---------------------------------------------------------------------------
# Which operation
# ---------------------------------------------------------------------------


def test_a_turn_carrying_files_is_gated_by_the_run_operation(
    client: TestClient, fake_executor: Any
) -> None:
    """Naming an attachment must not route around the operation that costs money.

    ``agent_sessions.run`` exists because opening a chat and paying for a model
    call are different privileges. A file path that authorized itself under the
    attachment write operation would let a caller who may upload but may not run
    spend a model call, which is the split's whole point.
    """
    agent_name = _bound_agent(client)
    session_key = _session_row(agent_name)
    key = _readable_attachment(client, session_key)
    authorizer = DenyingAuthorizer(Operation.AGENT_SESSIONS_RUN)
    set_authorizer(authorizer)

    response = _turn(client, session_key, [key])

    assert response.status_code == 403, response.text
    assert Operation.AGENT_SESSIONS_RUN in authorizer.seen
    _nothing_was_delivered(fake_executor, session_key=session_key)


# ---------------------------------------------------------------------------
# Creator scoping, under a provider that resolves callers
# ---------------------------------------------------------------------------


def test_a_non_creator_cannot_read_somebody_elses_file_into_a_model_call(
    client: TestClient, non_admin_client: TestClient, fake_executor: Any
) -> None:
    """The refusal that has to happen before the document is resolved.

    An attachment key is a stable identifier for a stranger's document. The 403
    alone does not distinguish a session gate that ran first from one that
    loaded the file, rendered its text and then refused - and only one of those
    keeps the contents out of this process entirely.
    """
    agent_name = _bound_agent(client)
    session_key = _session_row(agent_name, created_by_hash=hash_caller_id("someone-else"))
    key = _readable_attachment(client, session_key)

    response = _turn(non_admin_client, session_key, [key])

    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "AUTH_INSUFFICIENT_PRIVILEGES"
    _nothing_was_delivered(fake_executor, session_key=session_key)


def test_the_creator_may_send_their_own_file(
    client: TestClient, non_admin_client: TestClient, fake_executor: Any
) -> None:
    """The other half of the scoping rule, which the refusal alone cannot show."""
    agent_name = _bound_agent(client)
    session_key = _session_row(agent_name, created_by_hash=_non_admin_hash())
    key = _readable_attachment(non_admin_client, session_key)

    response = _turn(non_admin_client, session_key, [key])

    assert response.status_code == 200, response.text
    assert SECRET_TEXT in fake_executor.runs[-1]["message"]


def test_an_admin_may_send_a_file_into_anybody_s_session(
    client: TestClient, fake_executor: Any
) -> None:
    agent_name = _bound_agent(client)
    session_key = _session_row(agent_name, created_by_hash=hash_caller_id("someone-else"))
    key = _readable_attachment(client, session_key)

    response = _turn(client, session_key, [key])

    assert response.status_code == 200, response.text
    assert SECRET_TEXT in fake_executor.runs[-1]["message"]


# ---------------------------------------------------------------------------
# The same rules under the provider a default deployment runs
# ---------------------------------------------------------------------------


def test_under_no_auth_an_unattributed_session_still_carries_its_files(
    no_auth_client: TestClient, fake_executor: Any
) -> None:
    """The case that would otherwise switch this feature off where it is built.

    Under ``NoAuthProvider`` every session in the deployment has a NULL creator,
    so a delivery rule that refused unattributed sessions - the obvious way to
    close the unattributed-session hole on the write path - would refuse every
    turn carrying a file on the default configuration.
    """
    agent_name = _bound_agent(no_auth_client)
    session_key = _session_row(agent_name, created_by_hash=None)
    key = _readable_attachment(no_auth_client, session_key)

    response = _turn(no_auth_client, session_key, [key])

    assert response.status_code == 200, response.text
    assert SECRET_TEXT in fake_executor.runs[-1]["message"]


def test_a_dispatch_task_session_refuses_a_bystanders_file_under_a_resolving_provider(
    client: TestClient, non_admin_client: TestClient, fake_executor: Any
) -> None:
    """Oversight of a task's session is read, halt and nudge; driving it is not.

    ``require_content_access(for_turn=True)`` is what says so, and this asserts
    the attachment path inherits it rather than reimplementing it: a bystander
    who may read a task's transcript still may not append a document to it.
    """
    agent_name = _bound_agent(client)
    session_key = _session_row(
        agent_name,
        created_by_hash=hash_caller_id("the-dispatcher"),
        agent_task_id=4242,
    )
    key = _readable_attachment(client, session_key)

    response = _turn(non_admin_client, session_key, [key])

    assert response.status_code == 403, response.text
    assert "dispatch task" in response.json()["detail"]
    _nothing_was_delivered(fake_executor, session_key=session_key)


def test_a_dispatch_task_session_accepts_a_files_turn_under_no_auth(
    no_auth_client: TestClient, fake_executor: Any
) -> None:
    """Pinned as it behaves, and it is not what the upload path does.

    ``require_content_access`` returns on a NULL creator *before* its
    dispatch-task branch, so under the provider a default deployment runs this
    turn is allowed. The upload route checks ``agent_task_id`` directly and
    refuses the same shape. The asymmetry is defensible - under this provider
    the deployment is one trust domain and there is no second caller to keep
    out - but it is a difference between two routes onto the same session, and
    a test is a better place to find that than a fleet conversation with a
    stranger's document in it. If the turn path grows the upload path's
    condition, this test is the one that should change with it.
    """
    agent_name = _bound_agent(no_auth_client)
    session_key = _session_row(agent_name, created_by_hash=None)
    key = _readable_attachment(no_auth_client, session_key)
    # The file arrives before the session is a task's, because the upload route
    # would refuse it afterwards - which is the asymmetry this test is about.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_sessions SET agent_task_id = 4242 WHERE session_key = :key"),
            {"key": session_key},
        )

    response = _turn(no_auth_client, session_key, [key])

    assert response.status_code == 200, response.text
    assert SECRET_TEXT in fake_executor.runs[-1]["message"]


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


def test_another_namespaces_attachment_key_never_reaches_a_turn(
    app: FastAPI, fake_executor: Any
) -> None:
    """Asserted on the executor, because the 404 is the easy half.

    A key is opaque and guessable only by having seen it, but a delivery path
    that resolved attachments by key before scoping them to the caller's
    namespace would answer this 404 *after* reading another tenant's document -
    and the tenant would never know it had been read.
    """
    set_authorizer(NamespaceAuthorizer())
    tenant_a = {"X-Test-Namespace": "tenant-a"}
    tenant_b = {"X-Test-Namespace": "tenant-b"}
    owner = TestClient(app, raise_server_exceptions=True, headers=tenant_a)
    intruder = TestClient(app, raise_server_exceptions=True, headers=tenant_b)

    their_session = _session_row(_bound_agent(owner, headers=tenant_a), namespace_key="tenant-a")
    their_key = _readable_attachment(
        owner,
        their_session,
        namespace_key="tenant-a",
        headers={"X-Requested-With": "XMLHttpRequest", **tenant_a},
    )
    my_session = _session_row(_bound_agent(intruder, headers=tenant_b), namespace_key="tenant-b")

    response = _turn(intruder, my_session, [their_key])

    assert response.status_code == 404, response.text
    assert fake_executor.runs == []
    assert SECRET_TEXT not in repr(fake_executor.runs)
    assert _binding_count(their_session) == 0


def test_an_unauthenticated_caller_cannot_deliver_a_file(
    client: TestClient, unauthenticated_client: TestClient, fake_executor: Any
) -> None:
    agent_name = _bound_agent(client)
    session_key = _session_row(agent_name)
    key = _readable_attachment(client, session_key)

    response = _turn(unauthenticated_client, session_key, [key])

    assert response.status_code == 401, response.text
    _nothing_was_delivered(fake_executor, session_key=session_key)
