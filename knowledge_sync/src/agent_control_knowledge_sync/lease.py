"""The singleton sync lease: one claim statement, a fenced renewal and a fenced release.

One ``UPDATE ... WHERE lease_expires_at < now() RETURNING``; a read-then-write races.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .schema import sync_lease

SessionFactory = async_sessionmaker[AsyncSession]

DEFAULT_LEASE_SECONDS = 1800
"""Section 5.5's default. Renewed per batch, so it bounds a death, not a run."""

# The holder is a fence token and is never logged: a log line naming it steals the lease.
_LOG = logging.getLogger(__name__)

_EXPIRY = sa.text("now() + make_interval(secs => :lease_seconds)")
"""Database time, never the container's: two skewed clocks would break the fence."""

_CLAIM = (
    sa.update(sync_lease)
    .where(sync_lease.c.id == 1, sync_lease.c.lease_expires_at < sa.func.now())
    .values(holder=sa.bindparam("holder"), lease_expires_at=_EXPIRY)
    .returning(sync_lease.c.id)
)

_CURRENT = sa.select(sync_lease.c.holder, sync_lease.c.lease_expires_at).where(sync_lease.c.id == 1)

# Not "holder": a bindparam named for a column of the table under UPDATE is reserved for
# that column's SET clause, and colliding is a CompileError.
_RENEW = (
    sa.update(sync_lease)
    .where(sync_lease.c.id == 1, sync_lease.c.holder == sa.bindparam("fence_holder"))
    .values(lease_expires_at=_EXPIRY)
    .returning(sync_lease.c.id)
)

_RELEASE = (
    sa.update(sync_lease)
    .where(sync_lease.c.id == 1, sync_lease.c.holder == sa.bindparam("holder"))
    # sa.null() rather than None: a None value would mint a second bind
    # parameter called "holder" and collide with the fence in the WHERE.
    .values(holder=sa.null(), lease_expires_at=sa.text("'-infinity'"))
    .returning(sync_lease.c.id)
)


class LeaseHeldError(RuntimeError):
    """Somebody else is walking. This process must not, and says who."""

    def __init__(self, holder: str | None, expires_at: datetime | None) -> None:
        who = holder or "an unnamed holder"
        until = "an unknown time" if expires_at is None else expires_at.isoformat()
        super().__init__(f"The sync lease is held by {who} until {until}.")
        self.holder = holder
        self.expires_at = expires_at


def mint_token() -> str:
    """A per-run holder. Not ``hostname:pid``: a recycled pid must not fence."""
    return secrets.token_hex(16)


@dataclass(slots=True)
class SyncLease:
    """A claim this process holds, renewed per batch and released on the way out."""

    holder: str
    lease_seconds: int
    sessions: SessionFactory

    async def renew(self) -> bool:
        """Extend the claim. ``False`` means it was stolen and the caller must stop."""
        async with self.sessions() as session:
            row = (
                await session.execute(
                    _RENEW, {"fence_holder": self.holder, "lease_seconds": self.lease_seconds}
                )
            ).first()
            await session.commit()
        if row is None:
            _LOG.warning("lease lost: another holder owns it")
            return False
        _LOG.info("lease renewed for %ds", self.lease_seconds)
        return True

    async def release(self) -> None:
        """Fenced on the holder, so a late release cannot clear a successor's claim."""
        async with self.sessions() as session:
            await session.execute(_RELEASE, {"holder": self.holder})
            await session.commit()


async def claim(
    sessions: SessionFactory,
    *,
    holder: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> SyncLease:
    """Take the singleton lease, or raise naming whoever holds it."""
    async with sessions() as session:
        row = (
            await session.execute(_CLAIM, {"holder": holder, "lease_seconds": lease_seconds})
        ).first()
        if row is None:
            current = (await session.execute(_CURRENT)).first()
            await session.rollback()
            expires_at = None if current is None else current.lease_expires_at
            _LOG.warning("lease contended: held until %s", expires_at)
            raise LeaseHeldError(
                holder=None if current is None else current.holder,
                expires_at=expires_at,
            )
        await session.commit()
    _LOG.info("lease claimed for %ds", lease_seconds)
    return SyncLease(holder=holder, lease_seconds=lease_seconds, sessions=sessions)


@asynccontextmanager
async def hold_lease(
    sessions: SessionFactory,
    *,
    holder: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> AsyncIterator[SyncLease]:
    """Claim, yield, release - including when the body raises."""
    lease = await claim(sessions, holder=holder or mint_token(), lease_seconds=lease_seconds)
    try:
        yield lease
    finally:
        await lease.release()
