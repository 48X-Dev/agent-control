"""Table-level invariants for ``agent_runtimes`` and ``agent_sessions``, plus
the two things that protect the executor integration from the outside: the
compensating delete when a half-created session cannot be recorded, and the
startup refusals that stop an unprotected server from enabling it at all.

The constraint worth naming is ``uq_agent_sessions_executor_global``. It carries
no ``namespace_key``, and that is the entire cross-namespace boundary for
transcripts: the executor's own session store knows nothing about namespaces,
so if a row in namespace B could point at namespace A's executor session, every
namespace filter in the service layer would happily serve A's conversation to B.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from agent_control_server.config import (
    AuthSettings,
    ExecutorSettings,
    check_executor_startup_requirements,
)
from agent_control_server.errors import DatabaseError
from agent_control_server.services.executor_client import ExecutorSession


@pytest.fixture
def clean_tables(db_engine: Engine):
    def _truncate() -> None:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE agent_sessions, agent_runtimes, agents "
                    "RESTART IDENTITY CASCADE"
                )
            )

    _truncate()
    yield
    _truncate()


def _insert_agent(engine: Engine, *, namespace_key: str, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO agents (namespace_key, name) VALUES (:ns, :name)"),
            {"ns": namespace_key, "name": name},
        )


def _insert_session(
    engine: Engine,
    *,
    namespace_key: str,
    session_key: str,
    agent_name: str = "chat-agent-one",
    app_name: str = "my_agent",
    user_id: str = "default:user",
    executor_session_id: str = "sess-1",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_sessions (namespace_key, session_key, agent_name, "
                "executor_app_name, executor_user_id, executor_session_id) "
                "VALUES (:ns, :key, :agent, :app, :user, :sid)"
            ),
            {
                "ns": namespace_key,
                "key": session_key,
                "agent": agent_name,
                "app": app_name,
                "user": user_id,
                "sid": executor_session_id,
            },
        )


# ---------------------------------------------------------------------------
# Table invariants
# ---------------------------------------------------------------------------


def test_the_executor_triple_cannot_be_adopted_by_another_namespace(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_session(db_engine, namespace_key="alpha", session_key="a" * 32)

    with pytest.raises(IntegrityError):
        _insert_session(db_engine, namespace_key="beta", session_key="b" * 32)


def test_a_session_key_is_unique_within_a_namespace_and_free_across_them(
    db_engine: Engine, clean_tables: None
) -> None:
    shared_key = "c" * 32
    _insert_session(
        db_engine,
        namespace_key="alpha",
        session_key=shared_key,
        executor_session_id="sess-alpha",
    )
    # The same key under a second namespace is fine: the pair is what is unique.
    _insert_session(
        db_engine,
        namespace_key="beta",
        session_key=shared_key,
        executor_session_id="sess-beta",
    )

    with pytest.raises(IntegrityError):
        _insert_session(
            db_engine,
            namespace_key="alpha",
            session_key=shared_key,
            executor_session_id="sess-alpha-again",
        )


def test_a_binding_needs_a_registered_agent_and_goes_when_it_does(
    db_engine: Engine, clean_tables: None
) -> None:
    agent_name = f"chat-agent-{uuid.uuid4().hex[:8]}"

    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_runtimes "
                    "(namespace_key, agent_name, base_url, executor_app_name) "
                    "VALUES ('alpha', :agent, 'http://executor:8080', 'app')"
                ),
                {"agent": agent_name},
            )

    _insert_agent(db_engine, namespace_key="alpha", name=agent_name)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runtimes "
                "(namespace_key, agent_name, base_url, executor_app_name) "
                "VALUES ('alpha', :agent, 'http://executor:8080', 'app')"
            ),
            {"agent": agent_name},
        )
        conn.execute(
            text("DELETE FROM agents WHERE namespace_key = 'alpha' AND name = :agent"),
            {"agent": agent_name},
        )
        remaining = conn.execute(
            text("SELECT count(*) FROM agent_runtimes WHERE agent_name = :agent"),
            {"agent": agent_name},
        ).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# Compensation
# ---------------------------------------------------------------------------


class _RecordingClient:
    def __init__(self, deleted: list[tuple[str, str, str]]) -> None:
        self._deleted = deleted

    async def create_session(
        self, *, app_name: str, user_id: str, session_id: str, state: object
    ) -> ExecutorSession:
        del state
        return ExecutorSession(
            app_name=app_name, user_id=user_id, session_id=session_id
        )

    async def get_session(self, **_: object) -> ExecutorSession:  # pragma: no cover
        raise AssertionError("not used")

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        self._deleted.append((app_name, user_id, session_id))

    async def health(self) -> None:  # pragma: no cover
        return None

    async def aclose(self) -> None:  # pragma: no cover
        return None


class _RecordingFactory:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str, str]] = []

    def client_for(self, *, executor_kind: str, base_url: str) -> _RecordingClient:
        del executor_kind, base_url
        return _RecordingClient(self.deleted)

    async def aclose(self) -> None:  # pragma: no cover
        return None


async def test_a_failed_local_insert_deletes_the_executor_session(
    db_engine: Engine, clean_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor holds a conversation this server failed to record.

    Nothing would ever address it again, so the create is undone rather than
    left behind, and the caller is told the session was not created.
    """
    from agent_control_server.services import agent_sessions as service_module

    agent_name = f"chat-agent-{uuid.uuid4().hex[:8]}"
    _insert_agent(db_engine, namespace_key="default", name=agent_name)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runtimes "
                "(namespace_key, agent_name, base_url, executor_app_name) "
                "VALUES ('default', :agent, 'http://executor:8080', 'my_agent')"
            ),
            {"agent": agent_name},
        )

    async def _explode(self: object, **_: object) -> None:
        raise RuntimeError("local insert failed")

    monkeypatch.setattr(
        service_module.AgentSessionsService, "create_row", _explode, raising=True
    )

    factory = _RecordingFactory()
    settings = ExecutorSettings(enabled=True)
    with pytest.raises(DatabaseError):
        await service_module.open_session(
            namespace_key="default",
            created_by_hash="hash",
            agent_name=agent_name,
            title=None,
            team_slug=None,
            factory=factory,
            settings=settings,
        )

    assert len(factory.deleted) == 1
    app_name, user_id, _session_id = factory.deleted[0]
    assert app_name == "my_agent"
    assert user_id.startswith("default:")

    with db_engine.begin() as conn:
        rows = conn.execute(text("SELECT count(*) FROM agent_sessions")).scalar_one()
    assert rows == 0


# ---------------------------------------------------------------------------
# The session-bound token
# ---------------------------------------------------------------------------


def test_the_session_token_is_bound_and_carries_no_raw_caller_id() -> None:
    """The token goes to a process running arbitrary agent code.

    So two things have to hold: it is scoped to this session and these two
    operations and nothing else, and its ``actor_id`` claim is a hash. Under
    the default provider ``caller_id`` is the first eight characters of a live
    API key, and a JWT payload is base64, not encryption.
    """
    from agent_control_server.auth_framework.config import (
        RuntimeAuthConfig,
        set_runtime_auth_config,
    )
    from agent_control_server.auth_framework.runtime_token import verify_runtime_token
    from agent_control_server.services.agent_sessions import (
        SESSION_TOKEN_SCOPES,
        build_seed_state,
        mint_session_runtime_token,
    )

    secret = "s" * 48
    set_runtime_auth_config(RuntimeAuthConfig(secret=secret, ttl_seconds=900))
    try:
        minted = mint_session_runtime_token(
            namespace_key="alpha",
            session_key="d" * 32,
            actor_id="0123456789abcdef",
        )
        assert minted is not None
        token, _expires_at = minted
        claims = verify_runtime_token(token, secret)
    finally:
        set_runtime_auth_config(None)

    assert claims.target_type == "agent_session"
    assert claims.target_id == "d" * 32
    assert claims.namespace_key == "alpha"
    assert tuple(claims.scopes) == SESSION_TOKEN_SCOPES
    assert "runtime.use" not in claims.scopes
    assert claims.actor_id == "0123456789abcdef"

    state = build_seed_state(
        namespace_key="alpha",
        agent_name="chat-agent-one",
        session_key="d" * 32,
        runtime_token=token,
        token_expires_at=None,
    )
    assert state["agent_control"]["runtime_token"] == token
    assert state["agent_control"]["session_key"] == "d" * 32


def test_no_runtime_auth_config_means_a_session_without_a_token() -> None:
    """A deployment that has not enabled runtime auth still gets to chat."""
    from agent_control_server.auth_framework.config import set_runtime_auth_config
    from agent_control_server.services.agent_sessions import (
        build_seed_state,
        mint_session_runtime_token,
    )

    set_runtime_auth_config(None)
    assert (
        mint_session_runtime_token(
            namespace_key="alpha", session_key="e" * 32, actor_id="hash"
        )
        is None
    )
    state = build_seed_state(
        namespace_key="alpha",
        agent_name="chat-agent-one",
        session_key="e" * 32,
        runtime_token=None,
        token_expires_at=None,
    )
    assert "runtime_token" not in state["agent_control"]


# ---------------------------------------------------------------------------
# Startup refusals
# ---------------------------------------------------------------------------


def _check(
    *,
    enabled: bool,
    api_key_enabled: bool,
    cors_origins: list[str],
    allow_credentials: bool = True,
    allow_insecure_local_dev: bool = False,
) -> None:
    executor = ExecutorSettings(
        enabled=enabled, allow_insecure_local_dev=allow_insecure_local_dev
    )
    auth = AuthSettings(api_key_enabled=api_key_enabled)
    check_executor_startup_requirements(
        executor=executor,
        auth=auth,
        cors_origins=cors_origins,
        allow_credentials=allow_credentials,
    )


def test_a_disabled_executor_imposes_nothing() -> None:
    _check(enabled=False, api_key_enabled=False, cors_origins=["*"])


def test_enabling_the_executor_without_api_keys_refuses_to_start() -> None:
    with pytest.raises(RuntimeError, match="AGENT_CONTROL_API_KEY_ENABLED"):
        _check(
            enabled=True, api_key_enabled=False, cors_origins=["http://localhost:4000"]
        )


def test_the_local_dev_opt_out_relaxes_only_the_api_key_refusal() -> None:
    _check(
        enabled=True,
        api_key_enabled=False,
        cors_origins=["http://localhost:4000"],
        allow_insecure_local_dev=True,
    )
    with pytest.raises(RuntimeError, match="CORS"):
        _check(
            enabled=True,
            api_key_enabled=False,
            cors_origins=["*"],
            allow_insecure_local_dev=True,
        )


def test_wildcard_cors_with_credentials_refuses_to_start() -> None:
    with pytest.raises(RuntimeError, match="CORS"):
        _check(enabled=True, api_key_enabled=True, cors_origins=["*"])


def test_named_origins_are_accepted() -> None:
    _check(
        enabled=True,
        api_key_enabled=True,
        cors_origins=["https://console.example.com"],
    )
