"""The loop Phase 4 adds: a jittered interval, a survived failure, a signal mid-pass.

Nothing in here waits: the interval is recorded through the ``_sleep`` seam, and
the clock the loop reads is a fake a test can move.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from agent_control_knowledge_sync import serve as serve_module
from agent_control_knowledge_sync.config import (
    SYNC_INTERVAL_SECONDS_DEFAULT,
    ConfigError,
    SyncConfig,
)
from agent_control_knowledge_sync.drive_auth import DriveAuthError
from agent_control_knowledge_sync.drive_client import DriveRootUnreachableError
from agent_control_knowledge_sync.journal import RunCounters, SyncFailedError
from agent_control_knowledge_sync.lease import LeaseHeldError
from agent_control_knowledge_sync.serve import INTERVAL_JITTER, ShutdownRequest, serve
from sqlalchemy.exc import OperationalError

from tests.drive_support import _config

INTERVAL = float(SYNC_INTERVAL_SECONDS_DEFAULT)
FLOOR = INTERVAL * (1 - INTERVAL_JITTER)
CEILING = INTERVAL * (1 + INTERVAL_JITTER)

COUNTED = RunCounters(
    seen=9, indexed=4, unchanged=3, tombstoned=1, refused=1, refusals_by_code={"oversize": 1}
)


class FakeClock:
    """The loop's monotonic clock, so a test can make a pass take ten minutes."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSync:
    """Stands in for ``run_once``: scripted outcomes, and a count of what completed."""

    def __init__(self, *outcomes: Any, during: Callable[[], Awaitable[None]] | None = None) -> None:
        self._outcomes = list(outcomes)
        self._during = during
        self.calls = 0
        self.finished = 0

    async def __call__(self, config: Any) -> RunCounters:
        self.calls += 1
        if self._during is not None:
            await self._during()
        outcome = self._outcomes.pop(0) if self._outcomes else COUNTED
        if isinstance(outcome, BaseException):
            raise outcome
        self.finished += 1
        return COUNTED


def _run(
    monkeypatch: pytest.MonkeyPatch,
    sync: FakeSync,
    *,
    config: SyncConfig | None = None,
    clock: FakeClock | None = None,
    stop: ShutdownRequest | None = None,
    waits_allowed: int = 1,
    real_signals: bool = False,
) -> tuple[int, list[float]]:
    """Run the loop with the wait recorded rather than taken, and a deadline on the lot."""
    waits: list[float] = []
    monkeypatch.setattr(serve_module, "run_once", sync)
    monkeypatch.setattr(serve_module, "_clock", clock or FakeClock())

    async def fake_sleep(stopping: ShutdownRequest, seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= waits_allowed:
            stopping.request("the test had seen enough")

    monkeypatch.setattr(serve_module, "_sleep", fake_sleep)
    shutdown = None if real_signals else (stop or ShutdownRequest())

    async def go() -> int:
        # A loop built never to end needs a deadline, or a bug in the stop
        # condition hangs the suite instead of failing it.
        return await asyncio.wait_for(serve(config or _config(), shutdown=shutdown), timeout=10)

    return asyncio.run(go()), waits


def _advance(clock: FakeClock, seconds: float) -> Callable[[], Awaitable[None]]:
    async def _act() -> None:
        clock.advance(seconds)

    return _act


# --- the interval -------------------------------------------------------------


def test_the_wait_between_passes_is_jittered_inside_a_stated_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixed interval has every replica of every deployment hitting Drive on one second."""
    sync = FakeSync()

    code, waits = _run(monkeypatch, sync, waits_allowed=8)

    assert code == 0
    assert sync.calls == 8
    assert all(FLOOR <= wait <= CEILING for wait in waits), waits
    assert len(set(waits)) > 1, "a constant interval is not jitter"


def test_the_interval_is_the_config_field_rather_than_a_constant_in_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(sync_interval_seconds=60)

    _, waits = _run(monkeypatch, FakeSync(), config=config, waits_allowed=4)

    assert all(48.0 <= wait <= 72.0 for wait in waits), waits


def test_a_pass_that_took_ten_minutes_does_not_then_wait_a_whole_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interval sits between passes; adding it on top halves the cadence."""
    clock = FakeClock()

    _, waits = _run(monkeypatch, FakeSync(during=_advance(clock, 600.0)), clock=clock)

    assert waits[0] <= CEILING - 600.0
    assert waits[0] >= FLOOR - 600.0


def test_a_pass_longer_than_the_interval_starts_the_next_one_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()

    _, waits = _run(monkeypatch, FakeSync(during=_advance(clock, 2_000.0)), clock=clock)

    assert waits == [0.0]


def test_a_completed_pass_reports_what_it_did(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        code, _ = _run(monkeypatch, FakeSync())

    assert code == 0
    assert "seen=9 indexed=4 unchanged=3 tombstoned=1 refused=1" in caplog.text
    assert "oversize=1" in caplog.text


# --- a failing pass does not end the process ----------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        DriveRootUnreachableError("the corpus root did not resolve"),
        DriveAuthError("the token endpoint answered HTTP 503"),
        LeaseHeldError(holder="run-abc", expires_at=None),
        SyncFailedError("the sync lease was stolen mid-run", code="lease_lost"),
        OperationalError("SELECT 1", {}, Exception()),
        OSError("connection reset by peer"),
    ],
    ids=["root", "token", "lease-held", "lease-lost", "database", "socket"],
)
def test_a_failed_pass_is_survived_and_the_next_one_runs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    """A held lease or an unreachable Drive is a wait. Exiting would need a human to notice."""
    sync = FakeSync(failure)

    with caplog.at_level(logging.WARNING):
        code, waits = _run(monkeypatch, sync, waits_allowed=2)

    assert code == 0
    assert sync.calls == 2
    assert sync.finished == 1, "the pass after the failure ran and completed"
    assert len(waits) == 2, "the failed pass waited out an interval like any other"
    assert "pass failed" in caplog.text


def test_a_configuration_error_exits_non_zero_rather_than_looping_forever(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sleeping on it hides a fault behind a process that looks alive and does nothing."""
    sync = FakeSync(ConfigError("AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID is unset or empty."))

    with caplog.at_level(logging.ERROR):
        code, waits = _run(monkeypatch, sync, waits_allowed=3)

    assert code == 2
    assert sync.calls == 1
    assert waits == []
    assert "AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID" in caplog.text


def test_a_corpus_this_build_cannot_write_is_not_retried_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No interval migrates a schema, so waiting fifteen minutes only delays the report."""
    sync = FakeSync(SyncFailedError("schema version 2", code="schema_unsupported"))

    code, waits = _run(monkeypatch, sync, waits_allowed=3)

    assert code == 2
    assert sync.calls == 1
    assert waits == []


# --- stopping -----------------------------------------------------------------


def test_a_stop_mid_pass_lets_the_pass_in_flight_finish_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Abandoning a pass mid-batch is what strands a cursor; the signal only stops the next one."""
    stop = ShutdownRequest()

    async def signalled() -> None:
        stop.request("SIGTERM received")

    sync = FakeSync(during=signalled)

    code, waits = _run(monkeypatch, sync, stop=stop, waits_allowed=3)

    assert code == 0
    assert sync.calls == 1
    assert sync.finished == 1, "the pass in flight was not abandoned"
    assert waits == [], "a stop does not first wait out another interval"


def test_a_stop_during_the_wait_does_not_start_another_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = FakeSync()

    code, waits = _run(monkeypatch, sync, waits_allowed=1)

    assert code == 0
    assert sync.calls == 1
    assert len(waits) == 1


def test_a_stop_asked_for_before_the_first_pass_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = ShutdownRequest()
    stop.request("SIGTERM received")
    sync = FakeSync()

    code, waits = _run(monkeypatch, sync, stop=stop, waits_allowed=3)

    assert code == 0
    assert sync.calls == 0
    assert waits == []


def test_a_real_sigterm_stops_the_loop_and_the_pass_in_flight_still_finishes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every other stop here sets the event directly, which says nothing about the handler."""

    async def signalled() -> None:
        signal.raise_signal(signal.SIGTERM)
        for _ in range(5):
            # Let the loop deliver its own signal before the pass returns.
            await asyncio.sleep(0)

    sync = FakeSync(during=signalled)

    with caplog.at_level(logging.WARNING):
        code, waits = _run(monkeypatch, sync, real_signals=True, waits_allowed=3)

    assert code == 0
    assert sync.calls == 1
    assert sync.finished == 1
    assert waits == []
    assert "SIGTERM received" in caplog.text


def test_a_pass_that_outran_the_stop_signal_says_so_rather_than_pretending(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pass still running when the grace period ends is killed; the log is the only warning."""
    clock = FakeClock()
    stop = ShutdownRequest()

    async def signalled() -> None:
        stop.request("SIGTERM received")
        clock.advance(serve_module.GRACE_WARN_SECONDS + 60.0)

    with caplog.at_level(logging.WARNING):
        code, _ = _run(monkeypatch, FakeSync(during=signalled), clock=clock, stop=stop)

    assert code == 0
    assert "past SIGTERM received" in caplog.text
    assert "stop_grace_period" in caplog.text


def test_a_pass_that_finished_well_inside_the_grace_period_is_not_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    stop = ShutdownRequest()

    async def signalled() -> None:
        stop.request("SIGTERM received")
        clock.advance(10.0)

    with caplog.at_level(logging.WARNING):
        _run(monkeypatch, FakeSync(during=signalled), clock=clock, stop=stop)

    assert "stop_grace_period" not in caplog.text


def test_the_stop_names_its_reason_once_and_keeps_the_first(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stop = ShutdownRequest()

    async def signalled() -> None:
        stop.request("SIGTERM received")
        stop.request("SIGINT received")

    with caplog.at_level(logging.INFO):
        _run(monkeypatch, FakeSync(during=signalled), stop=stop)

    assert stop.reason == "SIGTERM received"
    assert "stopped: SIGTERM received" in caplog.text


@pytest.mark.asyncio
async def test_the_wait_ends_early_when_the_stop_arrives() -> None:
    """The real sleep, briefly: a container slow to die is a container that gets killed."""
    stop = ShutdownRequest()
    loop = asyncio.get_running_loop()
    loop.call_soon(stop.request, "SIGTERM received")

    await asyncio.wait_for(serve_module._sleep(stop, 3_600.0), timeout=5)

    assert stop.requested


@pytest.mark.asyncio
async def test_a_wait_of_nothing_returns_rather_than_hanging() -> None:
    """What a pass longer than its own interval asks the real sleep for."""
    await asyncio.wait_for(serve_module._sleep(ShutdownRequest(), 0.0), timeout=5)
