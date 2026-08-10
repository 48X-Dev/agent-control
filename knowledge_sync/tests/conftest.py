"""A real corpus and a fake Drive, for the tests in here that want both.

Provisioned through the server's own helper; nothing is autouse, so no-database tests stay.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from tests.fakes.drive import FakeDrive

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVISIONING = REPO_ROOT / "server" / "tests" / "knowledge_provisioning.py"

TRUNCATE = "TRUNCATE chunks, documents, sources, sync_runs RESTART IDENTITY CASCADE"
RELEASE_LEASE = "UPDATE sync_lease SET holder = NULL, lease_expires_at = '-infinity' WHERE id = 1"


def _load_provisioning() -> ModuleType | None:
    """Load the server's helper by path; the ``tests`` package name is taken here."""
    if not PROVISIONING.is_file():
        return None
    spec = importlib.util.spec_from_file_location("knowledge_corpus_provisioning", PROVISIONING)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass` resolves its own module out of
    # `sys.modules` and raises on a module that is not there yet.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError:
        del sys.modules[spec.name]
        return None
    return module


_provisioning = _load_provisioning()


@pytest.fixture(scope="module")
def _corpus_database() -> Iterator[Any]:
    if _provisioning is None:
        pytest.skip(f"{PROVISIONING} is not importable from this environment")
    reason = _provisioning.unavailable_reason()
    if reason:
        pytest.skip(reason)
    provisioned = _provisioning.provision("agent_knowledge_sync_test")
    try:
        yield provisioned
    finally:
        _provisioning.teardown(provisioned)


@pytest.fixture()
def corpus(_corpus_database: Any) -> Any:
    """A migrated corpus, emptied and with the lease free, for one test."""
    engine = sa.create_engine(_corpus_database.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(TRUNCATE))
            conn.execute(sa.text(RELEASE_LEASE))
    finally:
        engine.dispose()
    return _corpus_database


@pytest.fixture()
def drive(monkeypatch: pytest.MonkeyPatch) -> FakeDrive:
    """A Drive on a stubbed transport, wired into every client the sync builds."""
    fake = FakeDrive()
    real_client = httpx.AsyncClient

    def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = fake.transport()
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build)
    return fake


def query(corpus: Any, sql: str, **params: Any) -> list[Any]:
    """Read the corpus as the sync role, one statement, connection closed after."""
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return list(conn.execute(sa.text(sql), params).mappings())
    finally:
        engine.dispose()


def execute(corpus: Any, sql: str, **params: Any) -> None:
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(sql), params)
    finally:
        engine.dispose()


def scalar(corpus: Any, sql: str, **params: Any) -> Any:
    rows = query(corpus, sql, **params)
    return None if not rows else next(iter(rows[0].values()))
