import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from starlette.types import ASGIApp

from agent_control_engine import discover_evaluators
from agent_control_server.config import auth_settings, db_config
from agent_control_server.db import Base
from agent_control_server.main import app as fastapi_app

import agent_control_server.models  # ensure models are imported so tables are registered

# Discover evaluators at test session start
discover_evaluators()

# Test API keys
TEST_API_KEY = "test-api-key-12345"
TEST_ADMIN_API_KEY = "test-admin-key-12345"

# Create sync engine for tests (schema creation/cleanup)
engine = create_engine(db_config.get_url(), echo=False)

# Create async engine for async tests
async_engine = create_async_engine(db_config.get_url(), echo=False)
AsyncSessionTest = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _truncate_all_tables() -> None:
    """Clear all tables in the configured test database."""
    with engine.begin() as conn:
        schema = "public" if conn.dialect.name == "postgresql" else None
        table_names = inspect(conn).get_table_names(schema=schema)
        if not table_names:
            return

        if conn.dialect.name == "postgresql":
            qualified_tables = ", ".join(f'"{schema}"."{table_name}"' for table_name in table_names)
            conn.execute(text(f"TRUNCATE TABLE {qualified_tables} RESTART IDENTITY CASCADE"))
            return

        reflected_metadata = MetaData()
        reflected_metadata.reflect(bind=conn)
        for table in reversed(reflected_metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(scope="session")
def db_engine():
    """Provide the sqlalchemy engine for tests."""
    return engine


@pytest.fixture(scope="session")
def app():
    """Provide the FastAPI app."""
    return fastapi_app


@pytest.fixture(scope="session", autouse=True)
def db_schema() -> None:
    # Ensure test database exists (PostgreSQL)
    if engine.dialect.name == "postgresql":
        admin_url = (
            f"postgresql+{db_config.driver}://{db_config.user}:{db_config.password}@"
            f"{db_config.host}:{db_config.port}/postgres"
        )
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_config.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_config.database}"'))
        admin_engine.dispose()

    # Recreate tables for tests in the configured database.
    reflected_metadata = MetaData()
    reflected_metadata.reflect(bind=engine)
    reflected_metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def setup_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable auth with test keys for all tests by default."""
    monkeypatch.setattr(auth_settings, "api_key_enabled", True)
    monkeypatch.setattr(auth_settings, "api_keys", TEST_API_KEY)
    monkeypatch.setattr(auth_settings, "admin_api_keys", TEST_ADMIN_API_KEY)
    # Clear cached properties so they recompute with monkeypatched values
    for attr in ("_parsed_api_keys", "_parsed_admin_api_keys", "_all_valid_keys", "_all_admin_keys"):
        auth_settings.__dict__.pop(attr, None)


@pytest.fixture()
def client(app: object) -> TestClient:
    """Default test client with admin API key header."""
    return TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-API-Key": TEST_ADMIN_API_KEY},
    )


@pytest.fixture()
def non_admin_client(app: object) -> TestClient:
    """Test client with non-admin API key header."""
    return TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-API-Key": TEST_API_KEY},
    )


@pytest.fixture()
def admin_client(app: object) -> TestClient:
    """Test client with admin API key header."""
    return TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-API-Key": TEST_ADMIN_API_KEY},
    )


@pytest.fixture()
def unauthenticated_client(app: object) -> TestClient:
    """Test client without API key header."""
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def clean_db():
    _truncate_all_tables()
    yield


@pytest.fixture(autouse=True)
def _install_default_authorizer():
    """Install the default HeaderAuthProvider for the duration of each test.

    The framework's authorizer is normally wired by the FastAPI lifespan,
    but TestClient bypasses lifespan unless used as a context manager.
    Installing it here keeps tests isolated and matches the local-credential
    flow. ``clear_authorizers`` runs both around the test so any
    operation-specific overrides installed by a test cannot leak.
    """
    from agent_control_server.auth_framework.core import (
        clear_authorizers,
        set_authorizer,
    )
    from agent_control_server.auth_framework.providers import HeaderAuthProvider

    clear_authorizers()
    set_authorizer(HeaderAuthProvider())
    yield
    clear_authorizers()


@pytest.fixture
async def async_db():
    """Provide async database session for tests."""
    async with AsyncSessionTest() as session:
        yield session
        await session.rollback()


@pytest.fixture
def postgres_event_store():
    """Provide PostgresEventStore for observability tests."""
    from agent_control_server.observability import PostgresEventStore

    return PostgresEventStore(AsyncSessionTest)


@pytest.fixture
def setup_observability(postgres_event_store):
    """Set up observability store and ingestor on app.state."""
    from agent_control_server.observability import DirectEventIngestor

    ingestor = DirectEventIngestor(postgres_event_store)
    fastapi_app.state.event_store = postgres_event_store
    fastapi_app.state.event_ingestor = ingestor
    yield postgres_event_store
    # Clean up app.state
    del fastapi_app.state.event_store
    del fastapi_app.state.event_ingestor


# ---------------------------------------------------------------------------
# Live server
#
# ``TestClient`` and ``httpx.ASGITransport`` both buffer a whole response
# before handing it back, so anything about a response's shape over time -
# frame ordering, heartbeats, idle timeout, a terminal error frame after a
# 200, a client that hangs up mid-stream, connection-pool occupancy - is
# unassertable through them. Those assertions need a real socket.
#
# Rule: no streaming assertion may use ``TestClient`` or
# ``httpx.ASGITransport``. Use ``live_server`` / ``live_client`` instead.
#
# The same factory starts an arbitrary ASGI app, so a stub upstream (a fake
# executor that stalls, half-writes a body, or drops the connection) is
# started the same way as the real app.
# ---------------------------------------------------------------------------

LIVE_SERVER_HOST = "127.0.0.1"
LIVE_SERVER_STARTUP_TIMEOUT = 10.0
LIVE_SERVER_GRACEFUL_TIMEOUT = 5.0
LIVE_SERVER_SHUTDOWN_TIMEOUT = 15.0
LIVE_CLIENT_TIMEOUT = 10.0

# uvicorn accepts exactly these three lifespan modes.
LifespanMode = Literal["auto", "on", "off"]


class _UnsignalledServer(uvicorn.Server):
    """A uvicorn server that leaves the process signal handlers alone.

    ``uvicorn.Server.serve`` swaps in its own SIGINT/SIGTERM handlers whenever
    it runs on the main thread, which is where pytest runs. Left alone it
    would swallow Ctrl-C for the duration of the test run, and two overlapping
    live servers would restore the original handlers in the wrong order.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class LiveServer:
    """A uvicorn server on an ephemeral loopback port, in this event loop.

    Created via the ``live_server_factory`` fixture, which owns the shutdown.
    """

    def __init__(
        self,
        app: ASGIApp,
        host: str,
        port: int,
        stack: contextlib.AsyncExitStack,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._stack = stack

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url_for(self, path: str) -> str:
        """Absolute URL for a server-relative path such as ``/api/v1/agents``."""
        return f"{self.base_url}/{path.lstrip('/')}"

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        """An ``httpx.AsyncClient`` bound to this server, closed at teardown.

        Every keyword argument is forwarded to ``httpx.AsyncClient``, so a test
        that needs a single connection in the pool (to assert occupancy) or a
        short read timeout (to assert a stalled upstream) can say so.
        """
        kwargs.setdefault("base_url", self.base_url)
        kwargs.setdefault("timeout", httpx.Timeout(LIVE_CLIENT_TIMEOUT))
        client = httpx.AsyncClient(**kwargs)
        self._stack.push_async_callback(client.aclose)
        return client


class LiveServerFactory(Protocol):
    async def __call__(self, app: ASGIApp, *, lifespan: LifespanMode = "off") -> LiveServer: ...


class LiveServerContext(Protocol):
    def __call__(
        self, app: ASGIApp, *, lifespan: LifespanMode = "off"
    ) -> AbstractAsyncContextManager[LiveServer]: ...


async def _await_started(server: uvicorn.Server, serve_task: "asyncio.Task[None]") -> None:
    deadline = time.monotonic() + LIVE_SERVER_STARTUP_TIMEOUT
    while not server.started:
        if serve_task.done():
            serve_task.result()  # re-raise whatever killed startup
            raise RuntimeError("Live server exited before it finished starting.")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Live server did not start within {LIVE_SERVER_STARTUP_TIMEOUT}s."
            )
        await asyncio.sleep(0.01)


async def _stop_live_server(
    server: uvicorn.Server,
    serve_task: "asyncio.Task[None]",
    sock: socket.socket,
) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, LIVE_SERVER_SHUTDOWN_TIMEOUT)
    except TimeoutError:
        # ``wait_for`` has already cancelled and awaited the task. Reaching
        # here means uvicorn's own graceful window did not get the connections
        # closed either, which is a leak worth failing on rather than hiding.
        raise AssertionError(
            f"Live server did not shut down within {LIVE_SERVER_SHUTDOWN_TIMEOUT}s; "
            "a request handler or streaming response is most likely still running."
        ) from None
    finally:
        sock.close()


async def _start_live_server(
    app: ASGIApp,
    stack: contextlib.AsyncExitStack,
    *,
    lifespan: LifespanMode = "off",
) -> LiveServer:
    # Bind first, then hand uvicorn the bound socket: the port is known
    # without a second process being able to take it in between.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LIVE_SERVER_HOST, 0))
    except BaseException:
        sock.close()
        raise
    port = int(sock.getsockname()[1])

    config = uvicorn.Config(
        app,
        host=LIVE_SERVER_HOST,
        port=port,
        # The tests share one event loop with the server, so uvicorn must not
        # swap the loop policy out from under them.
        loop="asyncio",
        # ``None`` leaves the pytest/root logging config alone; uvicorn's
        # default dictConfig would disable existing loggers and break caplog.
        log_config=None,
        access_log=False,
        lifespan=lifespan,
        timeout_graceful_shutdown=int(LIVE_SERVER_GRACEFUL_TIMEOUT),
    )
    server = _UnsignalledServer(config)
    serve_task = asyncio.create_task(server.serve(sockets=[sock]), name=f"live-server-{port}")
    stack.push_async_callback(_stop_live_server, server, serve_task, sock)
    await _await_started(server, serve_task)
    return LiveServer(app=app, host=LIVE_SERVER_HOST, port=port, stack=stack)


@contextlib.asynccontextmanager
async def _serve_app(app: ASGIApp, *, lifespan: LifespanMode = "off") -> AsyncIterator[LiveServer]:
    async with contextlib.AsyncExitStack() as stack:
        yield await _start_live_server(app, stack, lifespan=lifespan)


@pytest.fixture
def live_server_context() -> LiveServerContext:
    """Serve an ASGI app for a bounded block, then stop it.

    Use this over ``live_server_factory`` when the test needs the server gone
    while the test is still running - killing a stub upstream mid-request, or
    asserting what a client does once the port stops answering.

        async with live_server_context(stub_app) as server:
            ...
    """
    return _serve_app


@pytest.fixture
async def live_server_factory() -> AsyncIterator[LiveServerFactory]:
    """Start ASGI apps on real ephemeral ports for the duration of one test.

    ``lifespan`` defaults to ``"off"`` so a live server behaves like the
    ``TestClient`` fixtures above, which also bypass lifespan: auth comes from
    ``_install_default_authorizer`` and observability from
    ``setup_observability``, per test, instead of from process-wide startup.
    Pass ``lifespan="on"`` to exercise startup and shutdown themselves.

    Teardown unwinds last-in-first-out, so clients handed out by
    ``LiveServer.client`` are closed before their server is stopped.
    """
    async with contextlib.AsyncExitStack() as stack:

        async def factory(app: ASGIApp, *, lifespan: LifespanMode = "off") -> LiveServer:
            return await stack.enter_async_context(_serve_app(app, lifespan=lifespan))

        yield factory


@pytest.fixture
async def live_server(live_server_factory: LiveServerFactory) -> LiveServer:
    """The real application, served over a loopback socket."""
    return await live_server_factory(fastapi_app)


@pytest.fixture
async def live_client(live_server: LiveServer) -> httpx.AsyncClient:
    """Admin-authenticated client bound to ``live_server``."""
    return live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
