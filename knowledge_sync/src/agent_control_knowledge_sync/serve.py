"""``serve``: one sync pass per jittered interval, until a signal asks it to stop."""

from __future__ import annotations

import asyncio
import logging
import random
import signal
import time

from sqlalchemy.exc import SQLAlchemyError

from .config import ConfigError, SyncConfig
from .drive_auth import DriveAuthError
from .drive_client import DriveError
from .github_transport import GitHubError
from .journal import RunCounters, SyncFailedError
from .lease import LeaseHeldError
from .sync import run_once

INTERVAL_JITTER = 0.2
"""How far a wait may wander either side, so replicas do not all poll on one second."""

GRACE_WARN_SECONDS = 300.0
"""How far past a stop signal a pass may run before the overrun is itself news."""

FATAL_CODES = frozenset({"schema_unsupported"})
"""Refusals no interval fixes: another fifteen minutes does not migrate a corpus."""

FATAL_PREFIX = "allowlist_"
"""Every allowlist refusal names a file a person must edit; no interval edits it."""

RECOVERABLE = (DriveError, DriveAuthError, GitHubError, LeaseHeldError, SQLAlchemyError, OSError)
"""A held lease, an unreachable source, a database mid-restart: wait, do not exit."""

_LOG = logging.getLogger(__name__)


class ShutdownRequest:
    """A stop asked for and not forced: no new pass starts, the one in flight finishes."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None
        self.requested_at: float | None = None

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self, reason: str) -> None:
        """The first caller names the reason and starts the grace clock."""
        if self.reason is None:
            self.reason = reason
            self.requested_at = _clock()
        self._event.set()

    async def wait(self) -> None:
        """Returns once a stop has been asked for."""
        await self._event.wait()


async def serve(config: SyncConfig, *, shutdown: ShutdownRequest | None = None) -> int:
    """Sync on an interval until a stop is asked for; 0 on a clean stop, 2 on bad config."""
    stop = shutdown if shutdown is not None else ShutdownRequest()
    if shutdown is None:
        _install_signal_handlers(stop)

    interval = float(config.sync_interval_seconds)
    _LOG.info(
        "serving: a pass every %.0fs +/-%d%%; a stop signal finishes the pass in flight",
        interval,
        round(INTERVAL_JITTER * 100),
    )
    while not stop.requested:
        started = _clock()
        code = await _one_pass(config)
        if code is not None:
            return code
        if stop.requested:
            _report_overrun(stop, _clock())
            break
        await _sleep(stop, _remaining(interval, _clock() - started))

    _LOG.info(
        "stopped: %s. The cursor is where the last committed batch left it.",
        stop.reason or "shutdown requested",
    )
    return 0


async def _one_pass(config: SyncConfig) -> int | None:
    """One pass. ``None`` carries on; an int is an exit code no interval would fix."""
    try:
        counters = await run_once(config)
    except ConfigError as exc:
        _LOG.error("configuration is wrong rather than unlucky: %s", exc)
        return 2
    except SyncFailedError as exc:
        if _fatal(exc.code):
            _LOG.error("no interval fixes this (%s): %s", exc.code, exc)
            return 2
        _survived(exc.code, exc)
    except RECOVERABLE as exc:
        _survived(type(exc).__name__, exc)
    else:
        _LOG.info("pass complete: %s", _counted(counters))
    return None


def _fatal(code: str) -> bool:
    """Whether waiting could ever help: a corpus to migrate or a file to fix says no."""
    return code in FATAL_CODES or code.startswith(FATAL_PREFIX)


def _survived(code: str, exc: BaseException) -> None:
    """A failed pass is a wait, not an exit: the next interval tries again."""
    _LOG.warning("pass failed (%s): %s. The next one runs at the next interval.", code, exc)


def _report_overrun(stop: ShutdownRequest, finished_at: float) -> None:
    """Say when the pass in flight outran the container's patience rather than pretend."""
    if stop.requested_at is None:
        return
    overrun = finished_at - stop.requested_at
    if overrun > GRACE_WARN_SECONDS:
        _LOG.warning(
            "the pass ran %.0fs past %s; a stop_grace_period shorter than that kills "
            "it mid-batch and the lease then waits out its own clock",
            overrun,
            stop.reason,
        )


def _counted(counters: RunCounters) -> str:
    """The five totals, plus the refusal codes when a pass had any."""
    line = (
        f"seen={counters.seen} indexed={counters.indexed} unchanged={counters.unchanged} "
        f"tombstoned={counters.tombstoned} refused={counters.refused}"
    )
    if not counters.refusals_by_code:
        return line
    codes = " ".join(f"{code}={n}" for code, n in sorted(counters.refusals_by_code.items()))
    return f"{line} [{codes}]"


def _jittered(seconds: float) -> float:
    return seconds * random.uniform(1 - INTERVAL_JITTER, 1 + INTERVAL_JITTER)


def _remaining(interval: float, elapsed: float) -> float:
    """The wait sits between passes: a pass that took ten minutes does not add fifteen."""
    return max(0.0, _jittered(interval) - elapsed)


def _clock() -> float:
    """The loop's monotonic clock, one seam so a test can make a pass take ten minutes."""
    return time.monotonic()


async def _sleep(stop: ShutdownRequest, seconds: float) -> None:
    """One interval, cut short by a stop; the seam that keeps tests from waiting."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


def _install_signal_handlers(stop: ShutdownRequest) -> None:
    """Catch the first SIGTERM or SIGINT; let a second one through."""
    loop = asyncio.get_running_loop()

    def _handle(name: str, number: signal.Signals) -> None:
        if stop.requested:
            signal.signal(number, signal.SIG_DFL)
            signal.raise_signal(number)
            return
        _LOG.warning(
            "%s received: no new pass starts and the pass in flight finishes first. "
            "A second signal is not caught.",
            name,
        )
        stop.request(f"{name} received")
        try:
            loop.remove_signal_handler(number)
        except OSError:  # pragma: no cover - CPython refuses the swap mid-signal
            pass

    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(number, _handle, number.name, number)
        except NotImplementedError:  # pragma: no cover - not a POSIX platform
            pass
