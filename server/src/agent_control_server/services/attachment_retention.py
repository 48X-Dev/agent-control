"""Two sweeps that reclaim attachment bytes, and why there are two.

**There is no sweeper daemon in this codebase and this is not the place to
invent one.** Both statements below run from the upload path, which is the only
path that adds bytes, so reclamation happens exactly when pressure is applied.
That is the pattern halt expiry already uses: the event is the next acquire.
The limitation that follows is real and is stated in ``.env.example`` rather
than left for an operator to discover: a namespace that stops uploading stops
reclaiming, so "reclaimed after N days" means "on the next upload attempt into
this namespace after N days", and nothing else will ever run it.

**The sweeps commit in their own transaction**, which is why
:func:`run_attachment_retention_committed` exists at all. Run inside the upload's
transaction, a sweep that made room is rolled back by the 413 of the very upload
it made room for - and the reclaimed-bytes counter, which is not transactional,
would go on reporting work that was undone. Committing separately also means a
refused upload still leaves the namespace smaller than it found it.

**Why two sweeps rather than one.** They reclaim different failures.

The orphan sweep is about an upload that never became part of a conversation.
Somebody attached a file, changed their mind, and closed the tab. Nothing else
in this system will ever look at that row again. It is deleted whole, bytes and
metadata, ``attachment_orphan_ttl_hours`` after the row was last written - the
later of ``created_at`` and ``updated_at``, so that a resurrected tombstone is
an hour old rather than a fortnight old.

The blob sweep is about an attachment that *did* its job. It was bound to a
turn, the model read it, and a fortnight later nobody needs the bytes but
somebody may still need to know the file existed. Bytes go, the row stays as a
tombstone carrying name, hashes, size and origin.

The second one is not tidiness. A first draft of the plan assumed task sessions
are deleted fifteen minutes after a task ends and built the whole retention
story on that cascade firing. It does not: dispatch sessions persist by default,
so without this sweep ``attachment_namespace_total_bytes`` fills with
dispatch-step attachments that nothing reclaims, every upload 413s, and the path
where that happens first (tracker ingress) is the one with no operator watching
it.

Order, stated so nobody reverses it: bytes are reclaimed on a timer, metadata is
reclaimed by the cascade, and the cascade may never run.
"""

from __future__ import annotations

import logging

from agent_control_models.attachments import AttachmentOrigin, AttachmentStatus
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from .executor_metrics import (
    ATTACHMENT_BLOBS_RECLAIMED,
    ATTACHMENT_SWEEP_BLOB_TTL,
    ATTACHMENT_SWEEP_ORPHAN,
)

logger = logging.getLogger(__name__)

# Statuses an orphan sweep may remove. ``tombstoned`` is excluded because its
# bytes are already gone and its whole purpose is to outlive them; ``converting``
# is excluded because a sweep must never delete a row another process is in the
# middle of writing.
_ORPHAN_SWEEPABLE = (
    AttachmentStatus.PENDING.value,
    AttachmentStatus.READY.value,
    AttachmentStatus.REJECTED.value,
    AttachmentStatus.FAILED.value,
)


async def sweep_orphaned_attachments(
    db: AsyncSession, *, namespace_key: str, ttl_hours: int
) -> int:
    """Delete attachments that were never bound to a turn. Returns the count.

    Blobs go with the row through ``ON DELETE CASCADE``, in the same statement
    and the same transaction, which is what makes "no orphaned bytes under
    either ordering" a property of the schema rather than of this function
    remembering to do two deletes in the right order.

    **Age is the later of ``created_at`` and ``updated_at``, not ``created_at``
    alone.** A tombstone that is re-uploaded keeps its key and its original
    ``created_at`` on purpose, so a row whose bytes came back a second ago can
    carry a timestamp from a fortnight ago. Reading ``created_at`` on its own
    put those bytes back inside this sweep's window the moment they landed, and
    the next upload into the namespace deleted the row whole - the caller's 201
    and their audit trail with it. Any future transition that revives a row is
    covered by the same expression, because reviving it is an update.
    """
    result = await db.execute(
        text(
            "DELETE FROM agent_session_attachments a "
            " WHERE a.namespace_key = :ns "
            "   AND a.status = ANY(:statuses) "
            "   AND greatest(a.created_at, a.updated_at) "
            "       < now() - make_interval(hours => :hours) "
            "   AND NOT EXISTS ( "
            "         SELECT 1 FROM agent_turn_attachments t "
            "          WHERE t.namespace_key = a.namespace_key "
            "            AND t.attachment_id = a.id) "
            "RETURNING a.id"
        ),
        {"ns": namespace_key, "statuses": list(_ORPHAN_SWEEPABLE), "hours": ttl_hours},
    )
    return len(result.fetchall())


async def sweep_stale_attachment_blobs(
    db: AsyncSession, *, namespace_key: str, ttl_days: int
) -> int:
    """Reclaim the bytes of attachments whose last turn is long past.

    Only attachments that were actually bound to a turn are touched here; the
    never-bound case belongs to the orphan sweep, which removes the row
    entirely. Splitting them that way is what keeps a file uploaded ten minutes
    ago and never sent from being tombstoned as though it had been used.

    The metadata row survives with its name, hashes, size and origin, so a
    transcript can still answer "what documents did this conversation see"
    after the bytes are gone. A download against it returns a written notice
    rather than a 404: the attachment is not missing, its bytes were reclaimed,
    and those are different sentences.

    **An agent's file is exempt until the tracker holds a copy.** Every other
    row here is a copy of something whose original is elsewhere; a file the
    agent wrote is the only one there is until ``linear_asset_url`` is set, and
    reclaiming it on a timer would delete the deliverable rather than a cache
    of it. Once the column is set the row is a copy like any other and the TTL
    resumes, which is one predicate rather than a second retention system.
    """
    result = await db.execute(
        text(
            "WITH stale AS ( "
            "  SELECT a.id "
            "    FROM agent_session_attachments a "
            "   WHERE a.namespace_key = :ns "
            "     AND a.status <> :tombstoned "
            "     AND NOT (a.origin = :agent AND a.linear_asset_url IS NULL) "
            "     AND EXISTS ( "
            "           SELECT 1 FROM agent_turn_attachments t "
            "            WHERE t.namespace_key = a.namespace_key "
            "              AND t.attachment_id = a.id) "
            "     AND ( SELECT max(t.created_at) FROM agent_turn_attachments t "
            "            WHERE t.namespace_key = a.namespace_key "
            "              AND t.attachment_id = a.id) "
            "         < now() - make_interval(days => :days) "
            "), "
            # A data-modifying CTE runs to completion whether or not the outer
            # query reads it, so the bytes go and the tombstone is stamped in
            # one statement against one snapshot. Two statements would leave a
            # window where a row reads ``ready`` with no bytes behind it.
            "gone AS ( "
            "  DELETE FROM agent_session_attachment_blobs b "
            "   WHERE b.namespace_key = :ns "
            "     AND b.attachment_id IN (SELECT id FROM stale) "
            "  RETURNING b.attachment_id "
            ") "
            "UPDATE agent_session_attachments a "
            "   SET status = :tombstoned, "
            "       updated_at = now() "
            " WHERE a.namespace_key = :ns "
            "   AND a.id IN (SELECT id FROM stale) "
            "RETURNING a.id"
        ),
        {
            "ns": namespace_key,
            "days": ttl_days,
            "tombstoned": AttachmentStatus.TOMBSTONED.value,
            "agent": AttachmentOrigin.AGENT.value,
        },
    )
    return len(result.fetchall())


async def run_attachment_retention(
    db: AsyncSession, *, namespace_key: str, orphan_ttl_hours: int, blob_ttl_days: int
) -> tuple[int, int]:
    """Run both sweeps for one namespace. Returns ``(orphans, tombstoned)``.

    Neither this nor the two statements it calls touches a metric, because
    neither has committed anything yet and a Prometheus counter cannot be
    rolled back. :func:`run_attachment_retention_committed` is what records
    them, after the commit that makes them true.
    """
    orphans = await sweep_orphaned_attachments(
        db, namespace_key=namespace_key, ttl_hours=orphan_ttl_hours
    )
    tombstoned = await sweep_stale_attachment_blobs(
        db, namespace_key=namespace_key, ttl_days=blob_ttl_days
    )
    return orphans, tombstoned


async def run_attachment_retention_committed(
    *, namespace_key: str, orphan_ttl_hours: int, blob_ttl_days: int
) -> tuple[int, int]:
    """Run both sweeps in a transaction of their own and commit them.

    A separate session rather than the caller's, so that a refusal further down
    the upload path - a quota, a type gate, anything - cannot undo the
    reclamation. The caller's next statement sees the committed result: this
    server runs Postgres at READ COMMITTED, so the quota counts that follow are
    taken against a snapshot newer than this commit.

    The cost of that second session is a second pooled connection held for the
    length of two statements, while the request still holds its own. Under
    enough concurrent uploads to exhaust the pool, the checkout times out - and
    a missed sweep is a far better outcome there than a failed upload, so the
    timeout is caught and logged rather than raised. Nothing else is caught: a
    sweep that fails for any other reason is a defect and should surface.
    """
    from ..db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            orphans, tombstoned = await run_attachment_retention(
                db,
                namespace_key=namespace_key,
                orphan_ttl_hours=orphan_ttl_hours,
                blob_ttl_days=blob_ttl_days,
            )
            await db.commit()
    except PoolTimeoutError:
        logger.warning(
            "Attachment retention skipped: no database connection was free. "
            "Reclamation runs from the upload path, so the next upload retries it."
        )
        return 0, 0

    if orphans:
        ATTACHMENT_BLOBS_RECLAIMED.labels(sweep=ATTACHMENT_SWEEP_ORPHAN).inc(orphans)
    if tombstoned:
        ATTACHMENT_BLOBS_RECLAIMED.labels(sweep=ATTACHMENT_SWEEP_BLOB_TTL).inc(tombstoned)
    return orphans, tombstoned
