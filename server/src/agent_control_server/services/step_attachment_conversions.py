"""Waiting, once and briefly, for the text of the files a step just fetched.

Plan sections 3.9 and 3.4 together, and the tension between them is the reason
this is its own file.

3.4 says conversion never runs on the path that needs its output, and it is
right about the case it was written for: five image attachments is roughly a
hundred seconds of OCR, and an upload request that waited for it would hold a
pooled database connection for the whole run. That argument is about a request
somebody is watching, holding a connection.

This is neither. Nobody is watching a dispatch chain, nothing here holds a
session, and the gap being closed is a single HTTP round trip: the dispatcher
opens the step, this server fetches and queues, this server answers, and the
dispatcher posts the turn - milliseconds, against a conversion measured in
seconds. Scheduling and returning therefore puts a file's *name* in front of
the agent on the step that fetched it and its *contents* in front of nobody,
which is precisely the half-answer section 3.9 exists to remove.

So the wait is bounded by ``linear_attachment_conversion_wait_seconds``,
answers honestly when it runs out, and stops early for work the queue refused.
The chat path is unchanged and still never waits, because an operator is
standing in front of it and a refusal they can see beats a delay they cannot.
"""

from __future__ import annotations

import asyncio

from agent_control_models.attachments import (
    AttachmentRefusalCode,
    StepAttachmentSummary,
    StepFilesSummary,
)

from ..config import linear_settings
from ..db import AsyncSessionLocal
from .attachment_conversions import read_cached, schedule_conversion
from .step_attachments import StepFilesPlan, StoredStepFiles


async def settle_step_conversions(
    *, plan: StepFilesPlan, stored: StoredStepFiles
) -> StepFilesSummary:
    """Schedule the text, then wait for it, briefly and with no session held.

    The gap this closes is one HTTP round trip. The dispatcher opens the step,
    the server fetches, the server answers, and the dispatcher posts the turn -
    all inside a few milliseconds, against a conversion that takes seconds.
    Scheduling and returning therefore puts a file's *name* in front of the
    agent on the step that fetched it and its *contents* in front of nobody,
    which is the failure the whole section exists to remove.

    **Scheduling is here and not in the store, and that is not tidiness.** A
    worker opens its own session. Submitted inside the store's transaction it
    can reach the blob store before the commit that makes the bytes visible,
    find nothing, release its claim and never convert the file - and which
    files that happens to depends on where the event loop yielded, so it is a
    race that silently converts some of a step's files and not others.

    Nothing is held while it waits. The caller has committed, this opens its
    own short session per poll, and the ceiling is
    ``linear_attachment_conversion_wait_seconds``. Running out is an answer and
    not an error: the file keeps its row, ``text_ready`` stays false, and the
    envelope says the contents are not in the message.
    """
    summary = stored.summary
    if not stored.pending:
        return summary

    unscheduled = {
        item.source_sha256
        for item in stored.pending
        if not schedule_conversion(
            namespace_key=plan.namespace_key,
            attachment_id=item.attachment_id,
            source_sha256=item.source_sha256,
            declared_mime=item.declared_mime,
        )
    }
    pending = {
        entry.sha256
        for entry in summary.files
        if entry.attachment_key is not None and entry.sha256 and not entry.text_ready
    }
    if not pending:
        return summary

    ready: dict[str, bool] = {}
    failed: set[str] = set()
    deadline = (
        asyncio.get_running_loop().time() + linear_settings.attachment_conversion_wait_seconds
    )
    first_pass = True
    while pending:
        async with AsyncSessionLocal() as db:
            for sha in list(pending):
                cached = await read_cached(
                    db, namespace_key=plan.namespace_key, source_sha256=sha
                )
                if cached is None:
                    # A worker inserts its claim before it reads anything, so
                    # no row at all on the first look and a submission the
                    # queue refused together mean nothing is coming. Waiting
                    # out the full ceiling for that would stall every step
                    # behind a full queue for an answer already known.
                    if first_pass and sha in unscheduled:
                        pending.discard(sha)
                    continue
                if not cached.is_finished:
                    continue
                pending.discard(sha)
                ready[sha] = cached.has_text
                if not cached.has_text:
                    failed.add(sha)
        first_pass = False
        if not pending or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(linear_settings.attachment_conversion_poll_seconds)

    settled = [_settle_one(entry, ready=ready, failed=failed) for entry in summary.files]
    return replace_summary(summary, settled)


def _settle_one(
    entry: StepAttachmentSummary, *, ready: dict[str, bool], failed: set[str]
) -> StepAttachmentSummary:
    """One row, told apart three ways rather than two.

    A file nobody has read yet and a file somebody read and found nothing in
    are different facts, and an agent given the wrong one draws the wrong
    conclusion: the first is worth asking about again, the second never will
    be.
    """
    if entry.attachment_key is None or not entry.sha256:
        return entry
    has_text = ready.get(entry.sha256)
    if has_text:
        return entry.model_copy(update={"text_ready": True, "failure_code": None})
    code = (
        AttachmentRefusalCode.NO_TEXT
        if entry.sha256 in failed
        else AttachmentRefusalCode.NOT_CONVERTED
    )
    return entry.model_copy(update={"text_ready": False, "failure_code": code.value})


def replace_summary(
    summary: StepFilesSummary, entries: list[StepAttachmentSummary]
) -> StepFilesSummary:
    """The same summary with new rows, and ``delivered`` recounted from them."""
    return summary.model_copy(
        update={
            "files": entries,
            "delivered": sum(1 for entry in entries if entry.text_ready),
        }
    )

