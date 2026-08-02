"""Per-principal ceiling on how many turns may be started per minute.

A turn is the first request in this product that costs money every time it is
made. Nothing else here needed a rate limit, and ``grep -rn`` over ``server/src``
before this module existed found none: only handling of being rate-limited *by*
Linear. So this is the first, and it has to exist before the endpoint is
reachable rather than after the first bill.

The per-session concurrency guard does not cover this. It permits exactly one
turn per session, and opening sessions is cheap, so a caller with one valid key
can hold a hundred sessions and run a hundred concurrent turns inside a limit
that is working exactly as designed.

**The bucket is per process, and that is a real limitation stated rather than
hidden.** With N replicas a principal gets N times the configured allowance.
Making it exact means a shared counter, which means Redis or a hot row in
Postgres on the path of every turn; this repo has neither and adding one for a
cost ceiling that only needs to be roughly right is the wrong trade. What this
does deliver is the thing that matters: a runaway loop or a leaked key cannot
spend without bound, and the ceiling is a small multiple of the configured
number rather than infinity.

The key is ``(namespace_key, caller_hash)``, the same bucket the plan gives the
halt endpoint, so a later phase sharing it shares a bucket rather than
discovering a second one.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_MAX_TRACKED_BUCKETS = 4096
"""Ceiling on distinct principals held at once.

Unbounded, this dictionary is a memory leak with an attacker-supplied key count
under a provider that resolves callers. When the ceiling is hit the coldest
buckets are dropped, which forgives whoever was quietest rather than whoever was
loudest - the opposite of the direction that would hurt."""

_WINDOW_SECONDS = 60.0


class TurnQuota:
    """A sliding one-minute window of turn starts, per principal.

    Sliding rather than fixed: a fixed window lets a caller spend the whole
    allowance in its last second and the whole next allowance in the first,
    which is double the intended rate at exactly the moment a runaway loop
    produces it.

    Guarded by a plain lock. Every caller today is on one event loop, so the
    lock is almost never contended, but "almost never" is not a property worth
    relying on when a sync dependency running in a worker thread would make it
    false.
    """

    def __init__(self, *, max_per_minute: int) -> None:
        if max_per_minute < 1:
            raise ValueError("max_per_minute must be at least 1")
        self.max_per_minute = max_per_minute
        self._lock = threading.Lock()
        self._starts: dict[tuple[str, str], deque[float]] = {}

    def try_acquire(
        self, *, namespace_key: str, caller_hash: str | None
    ) -> float | None:
        """Record one turn start, or return how long to wait instead.

        ``None`` means go ahead. A number means refuse, and it is the seconds
        until the oldest start in the window ages out - which is a real answer
        to "when can I retry", not a fixed guess.

        An unattributable caller shares one bucket. That is deliberately the
        strict direction: it cannot be split by anything a caller controls, so
        it cannot be evaded by arriving anonymously.
        """
        key = (namespace_key, caller_hash or "-")
        now = time.monotonic()
        cutoff = now - _WINDOW_SECONDS
        with self._lock:
            window = self._starts.get(key)
            if window is None:
                self._evict_if_full(cutoff)
                window = deque()
                self._starts[key] = window
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.max_per_minute:
                return max(0.0, window[0] + _WINDOW_SECONDS - now)
            window.append(now)
            return None

    def _evict_if_full(self, cutoff: float) -> None:
        """Drop spent buckets, then the coldest, until there is room."""
        if len(self._starts) < _MAX_TRACKED_BUCKETS:
            return
        for key in [
            key
            for key, window in self._starts.items()
            if not window or window[-1] <= cutoff
        ]:
            del self._starts[key]
        while len(self._starts) >= _MAX_TRACKED_BUCKETS:
            coldest = min(self._starts, key=lambda k: self._starts[k][-1])
            del self._starts[coldest]


_quota: TurnQuota | None = None
_quota_lock = threading.Lock()


def get_turn_quota(*, max_per_minute: int) -> TurnQuota:
    """Return the process-wide quota, building it on first use.

    Rebuilt when the configured ceiling changes, which in practice happens only
    in tests: settings are read once at import in a running server.
    """
    global _quota
    with _quota_lock:
        if _quota is None or _quota.max_per_minute != max_per_minute:
            _quota = TurnQuota(max_per_minute=max_per_minute)
        return _quota


def reset_turn_quota() -> None:
    """Forget every bucket. For tests, so one does not leak into the next."""
    global _quota
    with _quota_lock:
        _quota = None
