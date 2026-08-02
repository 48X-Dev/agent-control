"""Cached, degradation-aware reads of Linear milestones.

Sits between the HTTP endpoint and :mod:`.linear_client` and answers one
question: what should this team's milestone panel show right now? Every
outcome is a value, not an exception, so an unconfigured integration or an
unreachable third party never turns into a 500.

Three things protect Linear from a busy dashboard:

* a short TTL, so repeated page views inside a minute cost nothing;
* a single-flight lock per team, so ten simultaneous viewers of a cold team
  produce one request rather than ten;
* a per-team cooldown after any failed read, honouring ``Retry-After`` when a
  429 supplies one and using a fixed wait otherwise, so a rate-limited or
  unreachable Linear is not called again on the very next page view.

When a read fails and a recent-enough cached copy exists, the cached copy is
served: an hour-old board beats an error panel, and ``fetched_at`` tells the
client exactly how old it is.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from dataclasses import dataclass, field

from agent_control_models.linear import MilestonesStatus

from ..config import linear_settings
from .linear_client import HttpLinearClient, LinearClient, LinearError, LinearMilestone

_MAX_CACHE_ENTRIES = 512
"""Ceiling on tracked teams. Entries are keyed by namespace and team key, both
of which come from authenticated callers, so this is a backstop rather than a
defence against a flood."""


@dataclass(frozen=True)
class MilestonesResult:
    """What a milestone read produced, including why it produced nothing."""

    status: MilestonesStatus
    milestones: list[LinearMilestone] = field(default_factory=list)
    error: str | None = None
    retry_after_seconds: int | None = None
    cached: bool = False
    fetched_at: dt.datetime | None = None


@dataclass
class _CacheEntry:
    milestones: list[LinearMilestone]
    stored_at_monotonic: float
    fetched_at: dt.datetime


@dataclass
class _Cooldown:
    until_monotonic: float
    message: str
    upstream_retry_after_seconds: int | None
    """What Linear asked for, or ``None`` when the wait is this server's own
    choice. Only an upstream figure is reported to clients."""


class LinearMilestoneService:
    """Reads milestones for a Linear team, with caching and back-off.

    ``client`` is ``None`` when no API key is configured, which is how the
    service reports ``not_configured`` without needing to know what a
    credential looks like.
    """

    def __init__(
        self,
        *,
        client: LinearClient | None,
        ttl_seconds: float = 60.0,
        stale_ttl_seconds: float = 900.0,
        error_cooldown_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._stale_ttl_seconds = max(stale_ttl_seconds, ttl_seconds)
        self._error_cooldown_seconds = error_cooldown_seconds
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._cooldowns: dict[tuple[str, str], _Cooldown] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def aclose(self) -> None:
        """Release the underlying client and drop every cached read."""
        self._cache.clear()
        self._cooldowns.clear()
        self._locks.clear()
        if self._client is not None:
            await self._client.aclose()

    async def get_milestones(
        self, *, namespace_key: str, linear_team_key: str | None
    ) -> MilestonesResult:
        """Return the milestones to show for one Agent Control team.

        A server with no API key reports ``not_configured`` even for a team
        that is also unlinked: linking a team is pointless while the server
        cannot call Linear at all, so the setting that has to change first is
        the one to report.

        The cache is keyed by namespace as well as team key. Two namespaces
        pointing at the same Linear team is legitimate, and keeping their
        entries separate means one namespace's rate-limit cooldown never
        silences another's panel.
        """
        if self._client is None:
            return MilestonesResult(status=MilestonesStatus.NOT_CONFIGURED)
        if not linear_team_key:
            return MilestonesResult(status=MilestonesStatus.NOT_LINKED)

        key = (namespace_key, linear_team_key)
        fresh = self._fresh_entry(key)
        if fresh is not None:
            return _from_entry(fresh, cached=True)

        async with self._lock_for(key):
            # A caller that queued behind the lock may find the read it was
            # waiting for already done.
            fresh = self._fresh_entry(key)
            if fresh is not None:
                return _from_entry(fresh, cached=True)

            cooldown = self._active_cooldown(key)
            if cooldown is not None:
                remaining = int(cooldown.until_monotonic - time.monotonic()) + 1
                reported = remaining if cooldown.upstream_retry_after_seconds is not None else None
                return self._degraded(key, cooldown.message, reported)

            try:
                milestones = await self._client.fetch_milestones(linear_team_key)
            except LinearError as exc:
                self._start_cooldown(key, exc)
                return self._degraded(key, exc.message, exc.retry_after_seconds)

            entry = self._store(key, milestones)
            return _from_entry(entry, cached=False)

    def _fresh_entry(self, key: tuple[str, str]) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stored_at_monotonic > self._ttl_seconds:
            return None
        return entry

    def _stale_entry(self, key: tuple[str, str]) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stored_at_monotonic > self._stale_ttl_seconds:
            del self._cache[key]
            return None
        return entry

    def _degraded(
        self, key: tuple[str, str], message: str, retry_after_seconds: int | None
    ) -> MilestonesResult:
        """Serve the last good read if there is one, otherwise report the error."""
        stale = self._stale_entry(key)
        if stale is not None:
            return _from_entry(stale, cached=True)
        return MilestonesResult(
            status=MilestonesStatus.ERROR,
            error=message,
            retry_after_seconds=retry_after_seconds,
        )

    def _start_cooldown(self, key: tuple[str, str], exc: LinearError) -> None:
        """Stop calling Linear for this team for a while after a failed read.

        A 429 sets the wait; every other failure gets a fixed one. Without that
        second case an unreachable or hanging Linear costs one full request
        timeout on every page view, and each of those holds a database session
        open for the duration.
        """
        upstream = exc.retry_after_seconds
        seconds = float(upstream) if upstream is not None else self._error_cooldown_seconds
        self._cooldowns[key] = _Cooldown(
            until_monotonic=time.monotonic() + seconds,
            message=exc.message,
            upstream_retry_after_seconds=upstream,
        )

    def _active_cooldown(self, key: tuple[str, str]) -> _Cooldown | None:
        cooldown = self._cooldowns.get(key)
        if cooldown is None:
            return None
        if time.monotonic() >= cooldown.until_monotonic:
            del self._cooldowns[key]
            return None
        return cooldown

    def _store(
        self, key: tuple[str, str], milestones: list[LinearMilestone]
    ) -> _CacheEntry:
        self._cooldowns.pop(key, None)
        entry = _CacheEntry(
            milestones=milestones,
            stored_at_monotonic=time.monotonic(),
            fetched_at=dt.datetime.now(tz=dt.UTC),
        )
        self._cache[key] = entry
        self._evict_if_oversized()
        return entry

    def _evict_if_oversized(self) -> None:
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            oldest = min(
                self._cache, key=lambda k: self._cache[k].stored_at_monotonic
            )
            del self._cache[oldest]
            self._cooldowns.pop(oldest, None)
            # A held lock is someone's in-flight read; dropping it would let the
            # next caller start a second request for the same team.
            lock = self._locks.get(oldest)
            if lock is not None and not lock.locked():
                del self._locks[oldest]

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


def _from_entry(entry: _CacheEntry, *, cached: bool) -> MilestonesResult:
    status = MilestonesStatus.OK if entry.milestones else MilestonesStatus.EMPTY
    return MilestonesResult(
        status=status,
        # Copied so a caller cannot mutate what the next request will be served.
        milestones=list(entry.milestones),
        cached=cached,
        fetched_at=entry.fetched_at,
    )


_service: LinearMilestoneService | None = None
_service_lock = threading.Lock()


def build_milestone_service() -> LinearMilestoneService:
    """Construct a service from the process settings.

    An absent API key yields a service with no client rather than a failure,
    which is what makes ``not_configured`` an ordinary answer.
    """
    api_key = linear_settings.get_api_key()
    client = (
        HttpLinearClient(
            api_key=api_key,
            api_url=linear_settings.api_url,
            timeout_seconds=linear_settings.timeout_seconds,
            max_projects=linear_settings.max_projects,
            max_milestones_per_project=linear_settings.max_milestones_per_project,
        )
        if api_key is not None
        else None
    )
    return LinearMilestoneService(
        client=client,
        ttl_seconds=linear_settings.cache_ttl_seconds,
        stale_ttl_seconds=linear_settings.stale_ttl_seconds,
        error_cooldown_seconds=linear_settings.error_cooldown_seconds,
    )


def get_milestone_service() -> LinearMilestoneService:
    """FastAPI dependency returning the process-wide milestone service.

    Built on first use so a server that never reads milestones never opens an
    HTTP client. Tests override the dependency rather than reaching in here.

    FastAPI runs this in a worker thread, so two first requests can land here
    at once; the lock keeps the second from building a second HTTP client that
    nothing would ever close.
    """
    global _service
    with _service_lock:
        if _service is None:
            _service = build_milestone_service()
        return _service


async def shutdown_milestone_service() -> None:
    """Close the process-wide service, if one was ever built."""
    global _service
    with _service_lock:
        service, _service = _service, None
    if service is not None:
        await service.aclose()
