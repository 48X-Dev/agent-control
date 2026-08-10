"""The claim, the fence and the release, without a database."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

import pytest
from agent_control_knowledge_sync.lease import (
    DEFAULT_LEASE_SECONDS,
    LeaseHeldError,
    SyncLease,
    claim,
    hold_lease,
    mint_token,
)
from sqlalchemy.dialects import postgresql

Responder = Callable[[str, dict[str, Any]], Any]

HELD_UNTIL = dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.UTC)


class _Row:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Session:
    """Answers by SQL text, so a test never depends on statement order."""

    def __init__(self, log: list[tuple[str, dict[str, Any]]], responder: Responder) -> None:
        self.log = log
        self._responder = responder

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        """Compiles with the keys bound: a bare ``str(statement)`` never sees a fence collide."""
        bound = params or {}
        sql = str(statement.compile(dialect=postgresql.dialect(), column_keys=list(bound)))
        self.log.append((sql, bound))
        return _Result(self._responder(sql, bound))

    async def commit(self) -> None:
        self.log.append(("COMMIT", {}))

    async def rollback(self) -> None:
        self.log.append(("ROLLBACK", {}))


def _sessions(responder: Responder) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    log: list[tuple[str, dict[str, Any]]] = []
    return (lambda: _Session(log, responder)), log


def _granted(sql: str, params: dict[str, Any]) -> Any:
    return _Row(id=1)


def _refused(sql: str, params: dict[str, Any]) -> Any:
    if sql.startswith("SELECT"):
        return _Row(holder="run-abc", lease_expires_at=HELD_UNTIL)
    return None


@pytest.mark.asyncio
async def test_the_claim_is_one_statement_carrying_its_own_guard() -> None:
    sessions, log = _sessions(_granted)

    lease = await claim(sessions, holder="run-1", lease_seconds=60)

    updates = [sql for sql, _ in log if sql.startswith("UPDATE")]
    assert len(updates) == 1
    assert "lease_expires_at < now()" in updates[0]
    assert "RETURNING" in updates[0]
    assert lease.holder == "run-1"


@pytest.mark.asyncio
async def test_the_expiry_comes_from_the_database_clock() -> None:
    """Two containers with skewed clocks would otherwise break the fence."""
    sessions, log = _sessions(_granted)

    await claim(sessions, holder="run-1", lease_seconds=90)

    sql, params = log[0]
    assert "now() + make_interval(secs => " in sql
    assert params["lease_seconds"] == 90


@pytest.mark.asyncio
async def test_a_held_lease_is_refused_and_names_the_holder() -> None:
    sessions, _ = _sessions(_refused)

    with pytest.raises(LeaseHeldError) as caught:
        await claim(sessions, holder="run-2")

    assert caught.value.holder == "run-abc"
    assert caught.value.expires_at == HELD_UNTIL
    assert "run-abc" in str(caught.value)


@pytest.mark.asyncio
async def test_a_refused_claim_writes_nothing() -> None:
    sessions, log = _sessions(_refused)

    with pytest.raises(LeaseHeldError):
        await claim(sessions, holder="run-2")

    assert "COMMIT" not in [sql for sql, _ in log]


@pytest.mark.asyncio
async def test_the_renewal_is_fenced_on_the_holder() -> None:
    sessions, log = _sessions(_granted)
    lease = SyncLease(holder="run-1", lease_seconds=60, sessions=sessions)

    assert await lease.renew() is True

    sql, params = log[0]
    assert "sync_lease.holder = " in sql
    assert params["fence_holder"] == "run-1"
    # The renewal extends the expiry and touches nothing else: a SET that also
    # wrote the holder would be a claim wearing a renewal's name.
    assert "SET lease_expires_at=" in sql


@pytest.mark.asyncio
async def test_a_stolen_lease_renews_false_rather_than_raising() -> None:
    """The caller decides what to do about it; the runner stops and says so."""
    sessions, _ = _sessions(lambda sql, params: None)
    lease = SyncLease(holder="run-1", lease_seconds=60, sessions=sessions)

    assert await lease.renew() is False


@pytest.mark.asyncio
async def test_the_release_is_fenced_so_a_late_one_cannot_clear_a_successor() -> None:
    sessions, log = _sessions(_granted)
    lease = SyncLease(holder="run-1", lease_seconds=60, sessions=sessions)

    await lease.release()

    sql, params = log[0]
    assert "sync_lease.holder = " in sql
    assert params["holder"] == "run-1"
    assert "holder=NULL" in sql.replace(" = ", "=")


@pytest.mark.asyncio
async def test_hold_lease_releases_even_when_the_body_raises() -> None:
    sessions, log = _sessions(_granted)

    with pytest.raises(RuntimeError, match="walk exploded"):
        async with hold_lease(sessions, holder="run-1", lease_seconds=60):
            raise RuntimeError("walk exploded")

    assert any("holder=NULL" in sql.replace(" = ", "=") for sql, _ in log)


@pytest.mark.asyncio
async def test_hold_lease_defaults_to_the_planned_ttl() -> None:
    sessions, log = _sessions(_granted)

    async with hold_lease(sessions, holder="run-1") as lease:
        assert lease.lease_seconds == DEFAULT_LEASE_SECONDS

    assert log[0][1]["lease_seconds"] == DEFAULT_LEASE_SECONDS


@pytest.mark.asyncio
async def test_the_lease_says_what_happened_without_naming_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A log line carrying the fence token is a log line somebody can steal a lease with."""
    sessions, _ = _sessions(_granted)
    caplog.set_level(logging.INFO, logger="agent_control_knowledge_sync.lease")

    lease = await claim(sessions, holder="run-secret-token", lease_seconds=60)
    await lease.renew()

    logged = [record.getMessage() for record in caplog.records]
    assert any("lease claimed" in line for line in logged)
    assert any("lease renewed" in line for line in logged)
    assert not any("run-secret-token" in line for line in logged)


@pytest.mark.asyncio
async def test_a_contended_lease_is_logged_without_naming_the_holder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions, _ = _sessions(_refused)
    caplog.set_level(logging.WARNING, logger="agent_control_knowledge_sync.lease")

    with pytest.raises(LeaseHeldError):
        await claim(sessions, holder="run-2")

    logged = [record.getMessage() for record in caplog.records]
    assert any("lease contended" in line for line in logged)
    assert not any("run-abc" in line for line in logged)


def test_the_token_is_not_derived_from_a_pid() -> None:
    first, second = mint_token(), mint_token()
    assert first != second
    assert len(first) == 32
    assert int(first, 16) >= 0
