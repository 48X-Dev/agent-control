"""Joining the Linear fetch to the shipped attachment store, around one commit.

Plan section 3.9. :mod:`services.linear_attachments` finds and fetches; this
module decides whether a step fetches at all, and turns what came back into
stored rows and the summary an envelope is built from. It writes nothing that
another module could write and converts nothing.

**The split into three functions is the point of the file.** ``linear_issues.py``
caps its outbound call at ten seconds with a comment saying it "runs on a
request path holding a database session, so a hanging Linear must not be able
to hold that session for a full request timeout". Three attachments under a
per-step budget is twenty-five seconds of network wait, and holding a pooled
connection across that against ``pool_size=5, max_overflow=10`` is the defect
``orchestration-plan.md`` section 8.3 forbids outright. So the caller reads what
it needs and commits, :func:`fetch_step_files` runs with no session in hand at
all, and :func:`store_step_files` opens the second, short write.

**Nothing here fails a step.** A file is a refusal code and a sentence the agent
reads; a tracker that is down, a quota that is full and a type nobody accepts
are all outcomes rather than errors. The step opened before any of this ran and
it stays open, because a step that failed on an attachment would be the same
half-answer as a step that never mentioned one.

**And the step waits for the text, which is the one thing the shipped upload
path does not do.** Delivery to a model is text: a stored file whose conversion
has not finished is named to the agent and its contents are not there. On the
operator's upload path that gap is the minutes between dropping a file in and
pressing send, so a background conversion has finished by then. On this path it
is a single HTTP round trip between opening the step and starting its turn,
which no conversion can win, so scheduling and returning would deliver the
file's *name* on every step that fetched it and its *contents* on none.
:mod:`services.step_attachment_conversions` closes that, bounded, holding no
session, and says so plainly when the ceiling runs out.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_control_models.attachments import (
    AttachmentOrigin,
    AttachmentRefusalCode,
    StepAttachmentSummary,
    StepFilesSummary,
    TurnAttachmentVerdict,
)
from agent_control_models.tasks import AgentTaskSummary
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import executor_settings, linear_settings
from ..errors import APIError
from ..models import AgentSession, AgentTask, AgentTaskStep
from .agent_attachments import AgentAttachmentsService
from .attachment_blobs import get_attachment_blob_store
from .linear_attachments import (
    HttpLinearAttachmentClient,
    IssueFiles,
    collect_issue_files,
)

LINEAR_SOURCE_KIND = "linear"
"""The one source whose items carry files this server can fetch.

``source_kind`` stays ``linear`` for both the milestone path and the team-label
path (``models.AgentTask``), so this covers both and widens to neither."""


@dataclass(frozen=True, slots=True)
class StepFilesPlan:
    """Everything the fetch needs, read before the commit that releases the session.

    It carries an issue ref and never a URL: which URLs exist is a question
    only :mod:`services.linear_attachments` asks, and the answer never leaves
    it.
    """

    namespace_key: str
    session_id: int
    session_key: str
    issue_ref: str
    caller_hash: str | None
    task_key: str
    step_index: int
    remaining_task_bytes: int


async def plan_step_files(
    db: AsyncSession,
    *,
    namespace_key: str,
    task: AgentTaskSummary,
    step_index: int,
    session_key: str | None,
    caller_hash: str | None,
) -> StepFilesPlan | None:
    """Whether this step fetches, and under what budget. ``None`` means it does not.

    Four switches and a source check, all of them cheap and none of them
    reaching Linear. ``None`` here is what makes ``files`` absent on the step
    response rather than an empty summary: a deployment with the source off has
    not found nothing, it has not looked, and an envelope that said "0 of 0
    files" would be asserting something nobody checked.
    """
    if not (executor_settings.attachments_enabled and linear_settings.attachments_enabled):
        return None
    if task.source_kind != LINEAR_SOURCE_KIND or not task.source_ref:
        return None
    if session_key is None or linear_settings.get_api_key() is None:
        return None

    session_id = await db.scalar(
        select(AgentSession.id).where(
            AgentSession.namespace_key == namespace_key,
            AgentSession.session_key == session_key,
        )
    )
    if session_id is None:
        # The step is allowed to run on a session this server does not hold;
        # what it may not do is store files against one that is not there.
        return None

    spent = await _bytes_spent_on_task(db, namespace_key=namespace_key, task_key=task.task_key)
    return StepFilesPlan(
        namespace_key=namespace_key,
        session_id=int(session_id),
        session_key=session_key,
        issue_ref=task.source_ref,
        caller_hash=caller_hash,
        task_key=task.task_key,
        step_index=step_index,
        remaining_task_bytes=max(0, executor_settings.attachment_task_total_bytes - spent),
    )


async def _bytes_spent_on_task(
    db: AsyncSession, *, namespace_key: str, task_key: str
) -> int:
    """Bytes this task's earlier steps already pulled from the upload host.

    Summed from the durable step summaries rather than kept in a counter,
    because a twelve-step chain can be resumed by a different dispatcher after
    a reclaim and an in-memory total would restart at zero on the step that was
    most likely to cross the ceiling.
    """
    rows = await db.execute(
        select(AgentTaskStep.attachments_summary)
        .join(AgentTask, AgentTask.id == AgentTaskStep.task_id)
        .where(AgentTask.namespace_key == namespace_key, AgentTask.task_key == task_key)
    )
    total = 0
    for (summary,) in rows:
        for entry in summary or []:
            if not isinstance(entry, dict):
                continue
            # ``bytes_fetched`` and not ``size_bytes``: the ceiling bounds what
            # crossed the wire, and an aborted download, a login page and a
            # type refusal all cost bytes and store none. Falling back to
            # ``size_bytes`` keeps rows written before this field existed from
            # counting as free.
            spent = entry.get("bytes_fetched")
            if not isinstance(spent, int):
                spent = entry.get("size_bytes")
            if isinstance(spent, int):
                total += spent
    return total


async def fetch_step_files(plan: StepFilesPlan) -> IssueFiles:
    """Find and fetch this issue's files. Runs with no database session in hand.

    The client is built and closed here rather than held for the process: a
    step is minutes long, so a socket per step costs nothing measurable, and a
    long-lived client holding the API key would need a shutdown hook to close
    what a request opened.
    """
    api_key = linear_settings.get_api_key()
    if api_key is None:
        return IssueFiles(found=0, outcomes=())
    client = HttpLinearAttachmentClient(api_key=api_key, settings=linear_settings)
    try:
        return await collect_issue_files(
            client,
            issue_ref=plan.issue_ref,
            settings=linear_settings,
            # The smaller of the two ceilings, because bytes that cleared the
            # fetch and then failed the store would be spent for nothing.
            max_bytes_per_file=min(
                linear_settings.attachment_max_bytes,
                executor_settings.attachment_max_bytes,
            ),
            remaining_task_bytes=plan.remaining_task_bytes,
            accepted_mimes=set(executor_settings.attachment_accepted_mimes),
        )
    finally:
        await client.aclose()


@dataclass(frozen=True, slots=True)
class PendingConversion:
    """One stored file the step still needs the text of."""

    attachment_id: int
    source_sha256: str
    declared_mime: str


@dataclass(frozen=True, slots=True)
class StoredStepFiles:
    """What the store wrote, and what still has to be read.

    ``pending`` is deliberately not scheduled here. A background worker opens
    its own session, so a conversion started inside this transaction races the
    commit that makes the bytes visible: the worker finds no blob, releases its
    claim, and the file is never converted at all - a race that costs the whole
    point of the step on whichever file the event loop happened to reach
    first. The caller commits and then schedules.
    """

    summary: StepFilesSummary
    pending: tuple[PendingConversion, ...]


async def store_step_files(
    db: AsyncSession, *, plan: StepFilesPlan, files: IssueFiles
) -> StoredStepFiles:
    """Store what arrived, record what did not, and write the step's summary.

    Every outcome produces a row, delivered or refused. Recording only the
    deliveries would leave the audit trail saying what an under-delivering step
    says to an agent - that there was nothing to deliver - which is the failure
    this path exists to prevent.
    """
    session = await db.get(AgentSession, plan.session_id)
    service = AgentAttachmentsService(
        db, settings=executor_settings, blobs=get_attachment_blob_store()
    )
    entries: list[StepAttachmentSummary] = []
    pending: list[PendingConversion] = []

    for outcome in files.outcomes:
        if outcome.fetched is None or session is None:
            # A session that vanished between the two writes is not a fetch
            # that failed, and saying so would send whoever reads that line
            # looking at the tracker for a file the server actually holds.
            code = outcome.refusal or (
                AttachmentRefusalCode.BLOCKED
                if session is None
                else AttachmentRefusalCode.FETCH_FAILED
            )
            entries.append(
                StepAttachmentSummary(
                    display_name=outcome.display_name,
                    origin=AttachmentOrigin.LINEAR,
                    origin_ref=outcome.origin_ref,
                    verdict=TurnAttachmentVerdict.BLOCKED,
                    failure_code=code.value,
                    bytes_fetched=outcome.bytes_read,
                )
            )
            continue
        entry, conversion = await _store_one(
            service,
            plan=plan,
            session=session,
            display_name=outcome.display_name,
            origin_ref=outcome.origin_ref,
            data=outcome.fetched.data,
            sniffed_mime=outcome.fetched.sniffed_mime,
            bytes_read=outcome.bytes_read,
        )
        entries.append(entry)
        if conversion is not None:
            pending.append(conversion)

    summary = _summarize(files, entries)
    await record_step_summary(db, plan=plan, summary=summary)
    return StoredStepFiles(summary=summary, pending=tuple(pending))


def _summarize(files: IssueFiles, entries: list[StepAttachmentSummary]) -> StepFilesSummary:
    """The counts, with ``delivered`` meaning readable rather than stored.

    A file whose text is not in the turn message is a file the agent cannot
    read, so counting it as delivered would be the count line - the one
    sentence this path asks an agent to rely on - stating something untrue.
    """
    return StepFilesSummary(
        found=files.found,
        delivered=sum(1 for entry in entries if entry.text_ready),
        files=entries,
        read_failed=files.read_failed,
    )


async def record_step_summary(
    db: AsyncSession, *, plan: StepFilesPlan, summary: StepFilesSummary
) -> None:
    """Write the durable per-file record onto the step row.

    Called twice for one step: once with what was stored, and again once the
    conversions have settled. Writing it early is what keeps a fault in the
    second half from erasing the audit trail of the first.
    """
    step = await db.scalar(
        select(AgentTaskStep)
        .join(AgentTask, AgentTask.id == AgentTaskStep.task_id)
        .where(
            AgentTask.namespace_key == plan.namespace_key,
            AgentTask.task_key == plan.task_key,
            AgentTaskStep.step_index == plan.step_index,
        )
    )
    if step is not None:
        step.attachments_summary = [entry.model_dump(mode="json") for entry in summary.files]



async def _store_one(
    service: AgentAttachmentsService,
    *,
    plan: StepFilesPlan,
    session: AgentSession,
    display_name: str,
    origin_ref: str,
    data: bytes,
    sniffed_mime: str,
    bytes_read: int,
) -> tuple[StepAttachmentSummary, PendingConversion | None]:
    """One fetched file into the shipped store, with every refusal caught.

    ``create`` answers a browser, so its refusals are HTTP statuses. Nobody is
    holding a browser here: the same conditions have to become codes the
    envelope can turn into a sentence, or a full namespace quota would fail a
    step instead of telling an agent which file it is missing.

    The second half of the answer is the conversion the caller owes this file,
    or ``None`` when there is no file to convert.
    """
    try:
        row, _ = await service.create(
            namespace_key=plan.namespace_key,
            session=session,
            caller_hash=plan.caller_hash,
            declared_name=display_name,
            declared_mime=sniffed_mime,
            data=data,
            origin=AttachmentOrigin.LINEAR,
            origin_ref=origin_ref,
            enforce_rate=False,
        )
    except APIError as exc:
        return (
            StepAttachmentSummary(
                display_name=display_name,
                origin=AttachmentOrigin.LINEAR,
                origin_ref=origin_ref,
                verdict=TurnAttachmentVerdict.BLOCKED,
                failure_code=_REFUSAL_FOR_STATUS.get(
                    exc.status_code, AttachmentRefusalCode.BLOCKED
                ).value,
                bytes_fetched=bytes_read,
            ),
            None,
        )

    # Not scheduled here, and that is the whole reason this returns two
    # things. The worker opens its own session; started inside this
    # transaction it can reach the blob store before the commit that makes the
    # bytes visible, find nothing, release its claim and never convert the file
    # at all. The caller commits first.
    return (
        StepAttachmentSummary(
            display_name=row.display_name,
            sha256=row.source_sha256,
            size_bytes=row.size_bytes,
            sniffed_mime=row.sniffed_mime,
            origin=AttachmentOrigin.LINEAR,
            origin_ref=origin_ref,
            # The turn has not run yet, so nothing has been sent and nothing
            # has been refused. ``blocked`` above is not a control's verdict
            # either: it is the honest statement that this file will not be on
            # the turn.
            verdict=TurnAttachmentVerdict.PENDING,
            attachment_key=row.attachment_key,
            bytes_fetched=bytes_read,
        ),
        PendingConversion(
            attachment_id=row.id,
            source_sha256=row.source_sha256,
            declared_mime=row.sniffed_mime,
        ),
    )


_REFUSAL_FOR_STATUS: dict[int, AttachmentRefusalCode] = {
    413: AttachmentRefusalCode.TOO_LARGE,
    415: AttachmentRefusalCode.UNSUPPORTED_TYPE,
}
"""A store refusal in the vocabulary the agent is told.

Anything not here is ``blocked``: a code nobody can act on is worse than one
that says plainly that a guardrail refused the file."""
