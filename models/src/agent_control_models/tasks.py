"""The dispatch ledger: one unit of work, and the steps an agent took on it.

Agent Control is a control plane. The loop that claims a task, opens a session
and starts a turn runs *outside* this server, in the dispatcher process, for
the reasons the plan gives at length: a five-minute turn held inside a
request-scoped FastAPI process would starve policy evaluation for every
unrelated agent in the deployment, and a queue polled by N replicas is the
double-claim bug by construction. What lives *inside* the server is the ledger
and every ceiling that bounds the loop, because a budget enforced by the
process being budgeted is not a control.

Three things in these models are load-bearing rather than decorative.

**The claim is a row, and its holder is named.** ``instance_id`` on every write
is not audit decoration: the service compares it against ``claimed_by`` and
refuses a write from anything that is not the current holder. Two dispatchers
are safe because one of them loses the ``UPDATE ... RETURNING`` and then cannot
write steps for a task it does not hold.

**Resume position is read from the steps, never from a counter on the task.**
:class:`ClaimAgentTaskResponse.resume_step_index` is
``MAX(step_index) WHERE status='completed'`` plus one. A dispatcher that died
between a 200 from ``POST /turns`` and its own bookkeeping leaves a completed
step and a stale counter, and the counter is the half that is allowed to be
wrong.

**Nothing here selects an agent, a workflow, a tool or a ceiling.** A task
carries a title and a body written by whoever has access to the tracker, and
that text is untrusted input in the same class as a fetched web page. The agent
that runs it comes from server-side configuration, which is why there is no
``agent_name`` field on an import item and no ``labels`` field anywhere.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from .agent_runtimes import AgentName
from .attachments import StepFilesSummary
from .base import BaseModel
from .dispatch import DispatchStateSnapshot
from .server import PaginationInfo
from .sessions import SessionKey
from .teams import TeamSlug

TASK_KEY_LENGTH = 32
"""``uuid4().hex``. The only task identifier a client ever sees; the BIGSERIAL
primary key is never serialized, the same rule ``session_key`` follows."""

SOURCE_REF_MAX_LENGTH = 255
"""A Linear issue id, or a line id from a YAML file. Opaque to this server."""

TASK_TITLE_MAX_LENGTH = 500
TASK_BODY_MAX_LENGTH = 20000
"""Stored in full and truncated at the envelope, not here. The envelope caps an
untrusted block at 6000 characters and marks the cut inline; storing the cut
version instead would mean the console showed the operator less than the
tracker holds, with nothing saying so."""

STEP_BRIEF_MAX_LENGTH = 2000
STEP_OUTPUT_MAX_LENGTH = 40000
"""What one agent reported. Longer than a turn's input ceiling on purpose: a
step's output is the durable record once its session is deleted, and this text
is the only thing that survives to be posted back."""

WORKFLOW_KEY_MAX_LENGTH = 64
DISPATCHER_INSTANCE_MAX_LENGTH = 64
FAILURE_CODE_MAX_LENGTH = 64
FAILURE_DETAIL_MAX_LENGTH = 2000

MAX_STEPS_PER_TASK = 4
"""A workflow cannot loop. A ceiling on chain length rather than a guess about
usefulness, and the server refuses a step index at or beyond it."""

MAX_TURNS_PER_STEP = 3
"""One step's turn ceiling. The default is one; this is the most a workflow may
ask for. It is quoted by the lease refusal in the server's dispatch settings,
because the longest a step can legitimately run is a turn timeout times this
number, and a lease shorter than that reclaims work that is still running."""

IMPORT_MAX_ITEMS = 100
"""One import call, one page. The Linear read is capped at the same number and
reports ``beyond_page_cap`` rather than truncating quietly."""

TaskKey = Annotated[
    str,
    StringConstraints(
        min_length=TASK_KEY_LENGTH,
        max_length=TASK_KEY_LENGTH,
        pattern=r"^[0-9a-f]{32}$",
    ),
]

SourceRef = Annotated[
    str, StringConstraints(min_length=1, max_length=SOURCE_REF_MAX_LENGTH)
]

WorkflowKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=WORKFLOW_KEY_MAX_LENGTH,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    ),
]

DispatcherInstanceId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=DISPATCHER_INSTANCE_MAX_LENGTH,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

RefsDigest = Annotated[
    str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
]

DEFAULT_WORKFLOW_KEY = "default"
"""A team with no workflow gets one implicit step. Most of the value is one
agent doing one thing, and a design that demands a workflow before anything
runs does not get used."""


class TaskSourceKind(StrEnum):
    """Where the work came from.

    ``LINEAR`` covers both the milestone press and the team-label poll, and
    that is deliberate rather than untidy. Splitting them would let the same
    issue queued by both paths produce two open tasks and two agents working
    it, because the partial unique index that exists to prevent exactly that is
    keyed on ``(namespace_key, source_kind, source_ref)``.
    """

    LINEAR = "linear"
    FILE = "file"


class TaskScopeKind(StrEnum):
    """What bounded the set this task was imported from.

    Kept on the row rather than joined. A milestone deleted in Linear must
    still leave a legible history, so ``source_scope_name`` is a copy taken at
    import and the row outlives the thing it names.
    """

    MILESTONE = "milestone"
    TEAM_LABEL = "team_label"


class AgentTaskStatus(StrEnum):
    """Where a task is, and who is allowed to move it next.

    ``blocked`` and ``failed`` are not synonyms. ``failed`` means the work was
    attempted and did not work. ``blocked`` means it was never attempted
    because the configuration is wrong, and a dispatcher retrying it on a timer
    produces the same result forever, so a dispatcher never retries it.

    ``paused_quota`` is reclaimable and resumes at the same step. That is
    provably safe rather than merely convenient: the quota check runs before
    anything leaves the process, so a refusal leaves no side effect to
    duplicate.

    ``running_unknown`` is the 504 with no proof the invocation died. It is
    *not* reclaimable by a dispatcher. A machine that automatically resumes
    work that may still be running is the duplicated-email failure with extra
    steps, so only a human clears it.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PAUSED_QUOTA = "paused_quota"
    RUNNING_UNKNOWN = "running_unknown"
    AWAITING_APPROVAL = "awaiting_approval"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES: frozenset[AgentTaskStatus] = frozenset(
    {
        AgentTaskStatus.COMPLETED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED,
    }
)
"""The three that release the source ref.

Every other status, ``paused_quota`` and ``running_unknown`` included, holds
the slot: the partial unique index excludes exactly these three, so an issue
whose task is merely stuck cannot be queued a second time underneath it. The
reclaim predicate covers the same set from the other side, so a held slot is
always recoverable by something."""

RECLAIMABLE_TASK_STATUSES: frozenset[AgentTaskStatus] = frozenset(
    {AgentTaskStatus.RUNNING, AgentTaskStatus.PAUSED_QUOTA}
)
"""What a second dispatcher may take from a holder whose lease has expired.

``paused_quota`` is in here on purpose, and an earlier draft left it out.
Quota exhaustion is the single most likely moment for a dispatcher to be
restarted, because it is when an operator notices the fleet is stuck and
intervenes. Tasks abandoned at that moment would become permanent orphans:
no queued poll sees them, no reclaim matches them, and the unique index then
blocks the issue ever being imported again."""

DISPATCHER_SETTABLE_TASK_STATUSES: frozenset[AgentTaskStatus] = frozenset(
    {
        AgentTaskStatus.COMPLETED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.PAUSED_QUOTA,
        AgentTaskStatus.RUNNING_UNKNOWN,
    }
)
"""What the claim holder may set when it finishes with a task.

``cancelled`` is an operator's word and ``awaiting_approval`` is the server's,
so neither is reachable from a dispatcher's finish call."""


class AgentTaskStepStatus(StrEnum):
    """What happened on one hop.

    ``abandoned`` is written by the server on reclaim, never by a dispatcher,
    and it exists so the gap is visible in the console rather than papered
    over. A step whose dispatcher died mid-turn did something; nobody knows
    what.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ImportMode(StrEnum):
    """Look, or commit.

    ``preview`` does every read, every bucket count and every configuration
    check and inserts nothing. ``commit`` requires the digest of the set the
    operator was shown.
    """

    PREVIEW = "preview"
    COMMIT = "commit"


# =============================================================================
# Read models
# =============================================================================


class AgentTaskStep(BaseModel):
    """One agent's hop on one task, as the ledger recorded it.

    ``output_text`` is the durable record. Sessions are deleted when a task
    ends, so the transcript dies with them and a link to it would 404 within a
    fortnight; this text is what is still there. Read it as a claim by the
    agent, not as an observation of it.

    ``session_key`` goes null when that session is deleted, which is the
    ordinary end state rather than an error.
    """

    step_index: int = Field(..., ge=0, description="Position in the chain, from zero.")
    agent_name: AgentName = Field(..., description="Agent that ran this step.")
    brief: str = Field(
        "", max_length=STEP_BRIEF_MAX_LENGTH, description="What this step was asked to do."
    )
    status: AgentTaskStepStatus = Field(
        ..., description="How this step ended, or that it is running."
    )
    session_key: SessionKey | None = Field(
        default=None,
        description="Session this step ran on, until that session is deleted.",
    )
    turn_trace_id: str | None = Field(
        default=None,
        max_length=64,
        description="This turn's own trace. Server-minted; never supplied by a caller.",
    )
    output_text: str | None = Field(
        default=None,
        max_length=STEP_OUTPUT_MAX_LENGTH,
        description="What the agent reported. A claim, not a measurement.",
    )
    output_truncated: bool = Field(
        False, description="Whether the report was longer than the column allows."
    )
    attempts: int = Field(
        1,
        ge=1,
        description=(
            "How many times this index has been started. Above one means an "
            "earlier attempt was abandoned when its dispatcher's lease expired."
        ),
    )
    failure_code: str | None = Field(
        default=None, max_length=FAILURE_CODE_MAX_LENGTH, description="Machine-readable failure."
    )
    failure_detail: str | None = Field(
        default=None, max_length=FAILURE_DETAIL_MAX_LENGTH, description="Written explanation."
    )
    started_at: dt.datetime = Field(..., description="When this attempt started.")
    ended_at: dt.datetime | None = Field(
        default=None, description="When it stopped, however it stopped."
    )


class AgentTaskSummary(BaseModel):
    """A task without its steps, for lists and for the claim poll.

    Deliberately carries ``title`` but not ``body``: a queue poll that shipped
    every issue description would move megabytes to decide which key to claim.
    """

    task_key: TaskKey = Field(..., description="The only task identifier a client sees.")
    source_kind: TaskSourceKind = Field(..., description="Where the work came from.")
    source_ref: SourceRef = Field(..., description="The item's id in that source.")
    source_url: str | None = Field(default=None, description="Where a human can go and read it.")
    source_scope_kind: TaskScopeKind | None = Field(
        default=None, description="What bounded the imported set."
    )
    source_scope_ref: str | None = Field(
        default=None, description="Milestone id, when there was one."
    )
    source_scope_name: str | None = Field(
        default=None,
        description=(
            "Milestone name as it read at import. A copy, so a deleted "
            "milestone still reads."
        ),
    )
    source_team_key: str | None = Field(
        default=None,
        description=(
            "Linear team key resolved at import. Fixed here so re-linking a team "
            "cannot retarget a task that is already running."
        ),
    )
    title: str = Field(
        ...,
        max_length=TASK_TITLE_MAX_LENGTH,
        description="Untrusted. Written by whoever filed it.",
    )
    team_slug: TeamSlug | None = Field(
        default=None, description="Agent Control team this runs under."
    )
    workflow_key: str = Field(
        ..., max_length=WORKFLOW_KEY_MAX_LENGTH, description="Resolved at import."
    )
    status: AgentTaskStatus = Field(..., description="Where this task is.")
    dry_run: bool = Field(..., description="Set at import and never changed afterwards.")
    current_step: int = Field(
        ...,
        ge=0,
        description=(
            "Bookkeeping, not the resume rule. A crash can leave this behind the "
            "steps; resume position is read from the step rows."
        ),
    )
    turns_used: int = Field(..., ge=0, description="Turns spent on this task so far.")
    claimed_by: str | None = Field(
        default=None, description="Dispatcher instance holding the claim."
    )
    claimed_at: dt.datetime | None = Field(default=None)
    heartbeat_at: dt.datetime | None = Field(
        default=None,
        description="Last liveness signal from the holder. The lease is read from this.",
    )
    lease_expires_at: dt.datetime | None = Field(
        default=None, description="When another dispatcher may reclaim this task."
    )
    deadline_at: dt.datetime = Field(
        ...,
        description=(
            "Server-set at claim. A step may not start after it, so a hung "
            "dispatcher cannot outlive its own budget."
        ),
    )
    chain_trace_id: str | None = Field(
        default=None,
        max_length=64,
        description="Server-minted at claim. Never accepted from a caller.",
    )
    failure_code: str | None = Field(default=None, max_length=FAILURE_CODE_MAX_LENGTH)
    failure_detail: str | None = Field(default=None, max_length=FAILURE_DETAIL_MAX_LENGTH)
    created_at: dt.datetime = Field(...)
    updated_at: dt.datetime = Field(...)


class AgentTaskDetail(AgentTaskSummary):
    """One task, its body, and every step recorded against it.

    ``body`` is the issue description as filed. It is untrusted input: treat it
    the way you would treat a web page a tool fetched, and never render it as
    instructions to anything.
    """

    body: str = Field(
        "",
        max_length=TASK_BODY_MAX_LENGTH,
        description="Untrusted. Written by whoever filed it.",
    )
    steps: list[AgentTaskStep] = Field(
        default_factory=list,
        description="Ordered by step index. The chain, and the resume rule's source.",
    )


class ListAgentTasksResponse(BaseModel):
    """A page of tasks."""

    tasks: list[AgentTaskSummary] = Field(default_factory=list)
    pagination: PaginationInfo = Field(..., description="Cursor-based pagination metadata.")


class GetAgentTaskResponse(BaseModel):
    """One task with its steps."""

    task: AgentTaskDetail = Field(...)


# =============================================================================
# Import
# =============================================================================


class ImportTaskItem(BaseModel):
    """One candidate row, supplied by the caller for the explicit-list source.

    Note what it cannot carry: an agent, a workflow, a tool list, a priority, a
    label or a ceiling. Nothing the source can express reaches a decision, and
    that is the property section 12.4 of the plan depends on. An earlier draft
    carried labels and used them to pick the agent, which handed agent
    selection to anyone who can file an issue.
    """

    model_config = ConfigDict(extra="forbid")

    source_ref: SourceRef = Field(..., description="Stable id in the source. Deduplication key.")
    title: str = Field(..., min_length=1, max_length=TASK_TITLE_MAX_LENGTH)
    body: str = Field(default="", max_length=TASK_BODY_MAX_LENGTH)
    source_url: str | None = Field(
        default=None,
        max_length=1000,
        description="Where a human can go and read the original. http(s) only.",
    )

    @field_validator("source_url")
    @classmethod
    def _only_a_scheme_a_browser_should_follow(cls, url: str | None) -> str | None:
        """Refuse anything but http and https.

        This field is the one part of an untrusted item that the console turns
        into something clickable, and it arrives from whoever can file into the
        source. A ``javascript:`` or ``data:`` value here would be a script the
        operator runs by clicking the provenance link on the confirm screen -
        which is the screen whose whole job is to let them check provenance.
        """
        if url is None:
            return None
        if not url.startswith(("http://", "https://")):
            raise ValueError("source_url must be an http:// or https:// address.")
        return url


class ImportItemsScope(BaseModel):
    """An explicit list of items, which is what a YAML file on disk becomes.

    The milestone scope is a separate member of this union and lands with the
    Linear read. It is deliberately not reachable from a scheduler: the human
    press over a displayed set is the whole authorization for milestone scope,
    so a cron job must not be able to construct one.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="items", pattern="^items$")
    source_kind: TaskSourceKind = Field(
        TaskSourceKind.FILE,
        description="What these refs are ids in. Decides which open task blocks a duplicate.",
    )
    items: list[ImportTaskItem] = Field(..., min_length=1, max_length=IMPORT_MAX_ITEMS)

    @field_validator("items")
    @classmethod
    def _refs_are_unique(cls, items: list[ImportTaskItem]) -> list[ImportTaskItem]:
        """Refuse a body that names one ref twice.

        The database would dedupe it silently through ``ON CONFLICT DO
        NOTHING`` and report a lower created count than the caller asked for,
        which reads as a partial failure. A duplicate in one request is a
        caller bug and says so."""
        seen = {item.source_ref for item in items}
        if len(seen) != len(items):
            raise ValueError("source_ref appears more than once in this import.")
        return items


class ImportAgentTasksRequest(BaseModel):
    """Preview or commit one import.

    ``mode: preview`` inserts nothing and is safe to call on every render.
    ``mode: commit`` requires ``expected_refs_digest``, a sha256 over the
    sorted source refs of the set the operator was shown, and refuses with 409
    ``SCOPE_CHANGED`` when it no longer matches. A digest over the *set* rather
    than a count is what catches substitution: four items replaced by four
    different items has the same count and a different digest.
    """

    model_config = ConfigDict(extra="forbid")

    scope: ImportItemsScope = Field(..., description="What to import.")
    team_slug: TeamSlug | None = Field(
        default=None, description="Agent Control team these tasks run under."
    )
    workflow_key: WorkflowKey | None = Field(
        default=None,
        description=f"Null resolves to the implicit one-step workflow, {DEFAULT_WORKFLOW_KEY!r}.",
    )
    dry_run: bool = Field(
        True,
        description=(
            "Recorded on the row and never changed afterwards. A dry run is an "
            "assertion about the deployment until the canary proves it."
        ),
    )
    requeue_completed: bool = Field(
        default=False,
        description=(
            "Import refs that already have a finished task. Off by default. The "
            "partial unique index permits it - a completed task must not block "
            "the same tracker item next month, because reopened issues are real "
            "- but permitting it and doing it unasked are different things. An "
            "unattended loop re-reading the same source would otherwise pay for "
            "the same work on every pass, so re-running finished work is an "
            "operator's decision and shows up as one. A preview always reports "
            "these under skipped.already_worked either way."
        ),
    )
    mode: ImportMode = Field(default=ImportMode.PREVIEW, description="Look, or commit.")
    expected_refs_digest: RefsDigest | None = Field(
        default=None, description="Required on commit. sha256 over the sorted refs."
    )


class ImportCandidate(BaseModel):
    """One row the operator is being asked to agree to.

    The confirm renders the list, not the number, and that is the point. An
    attacker with tracker access files an issue into the targeted scope and is
    inside the enumerated set; an operator who sees "5 issues" where they
    expected 4 presses anyway, because 5 and 4 look the same at a glance.
    """

    source_ref: SourceRef = Field(...)
    title: str = Field(..., max_length=TASK_TITLE_MAX_LENGTH)
    source_url: str | None = Field(default=None)
    flags: list[str] = Field(
        default_factory=list,
        description=(
            "Provenance heuristics such as new_within_hour. Heuristics rather "
            "than proof, and the console has to say so."
        ),
    )


class ImportSkipCounts(BaseModel):
    """Why candidates did not make the eligible set.

    Counted in Python rather than removed by a filter, because a confirm that
    cannot say *"3 are already queued"* is a confirm the operator cannot read.
    """

    already_queued: int = Field(default=0, ge=0, description="An open task already holds that ref.")
    already_worked: int = Field(
        0, ge=0, description="A terminal task exists; the ref is free again."
    )
    other_team: int = Field(default=0, ge=0)
    assigned: int = Field(default=0, ge=0)
    in_progress: int = Field(default=0, ge=0)
    label_filtered: int = Field(default=0, ge=0)
    beyond_page_cap: int = Field(default=0, ge=0)


class ImportAgentTasksResponse(BaseModel):
    """One shape for both modes, and it carries the rows.

    ``created`` is zero and ``task_keys`` is empty on a preview. On a commit,
    ``created`` is what was actually inserted, which can be lower than the
    eligible count when another caller committed the same ref in between:
    the insert is ``ON CONFLICT DO NOTHING`` and reports what it did rather
    than what it attempted.
    """

    mode: ImportMode = Field(...)
    eligible: list[ImportCandidate] = Field(default_factory=list)
    refs_digest: RefsDigest = Field(..., description="sha256 over the sorted eligible refs.")
    skipped: ImportSkipCounts = Field(
        ..., description="Why candidates did not make the eligible set."
    )
    workflow_key: str = Field(...)
    dry_run: bool = Field(...)
    created: int = Field(default=0, ge=0)
    task_keys: list[TaskKey] = Field(default_factory=list)
    dispatch_state: DispatchStateSnapshot | None = Field(
        default=None,
        description=(
            "The namespace's budget and both stop switches, so a confirm can "
            "say 'this namespace is paused' rather than queueing rows that will "
            "never run. **Advisory.** Enforcement is the hourly task ceiling in "
            "the transaction that inserts, and the turn-path refusal inside "
            "_acquire_turn. A preview that reports a budget is exactly the shape "
            "of thing a later reader simplifies into the enforcement point."
        ),
    )


# =============================================================================
# Claim and lease
# =============================================================================


class ClaimAgentTaskRequest(BaseModel):
    """Take a task, or find out that somebody else holds it.

    ``instance_id`` names the holder. Every later write quotes it and the
    server refuses any that does not match, which is what makes the claim a
    lease rather than a label.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: DispatcherInstanceId = Field(
        ..., description="Stable identifier for this dispatcher process."
    )


class ClaimAgentTaskResponse(BaseModel):
    """The claimed task, and where to start.

    ``prior_status`` is here because the safety argument for resuming differs
    by it even where the arithmetic does not. A ``queued`` task ran nothing. A
    reclaimed ``running`` task has an abandoned step at
    ``resume_step_index`` whose side effects, if any, already happened and will
    happen again. A reclaimed ``paused_quota`` task never reached the executor,
    which is the one genuinely safe retry in this design.
    """

    task: AgentTaskDetail = Field(...)
    prior_status: AgentTaskStatus = Field(
        ..., description="What the task was immediately before this claim."
    )
    resume_step_index: int = Field(
        ...,
        ge=0,
        description=(
            "MAX(step_index) WHERE status='completed', plus one. Read from the "
            "steps, never from current_step."
        ),
    )
    reclaimed: bool = Field(
        ...,
        description="True when this claim took the task from a holder whose lease had expired.",
    )
    abandoned_step_indexes: list[int] = Field(
        default_factory=list,
        description="Steps this claim marked abandoned. Empty on a fresh claim.",
    )
    lease_expires_at: dt.datetime = Field(
        ..., description="Heartbeat before this, or lose the claim."
    )
    lease_seconds: int = Field(
        ..., gt=0, description="Server-set. The dispatcher does not choose it."
    )


class HeartbeatAgentTaskRequest(BaseModel):
    """Refresh the lease. Sent between steps, and during a quota backoff.

    Between steps rather than during one: a step can legitimately take five
    minutes, so the lease has to outlast a turn by construction rather than by
    a timer racing it.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: DispatcherInstanceId = Field(...)


class HeartbeatAgentTaskResponse(BaseModel):
    """The refreshed lease, and the deadline it still sits under."""

    task_key: TaskKey = Field(...)
    status: AgentTaskStatus = Field(...)
    heartbeat_at: dt.datetime = Field(...)
    lease_expires_at: dt.datetime = Field(...)
    deadline_at: dt.datetime = Field(...)


# =============================================================================
# Steps
# =============================================================================


class StartAgentTaskStepRequest(BaseModel):
    """Open a step row before the turn, so a dispatcher that dies leaves a mark.

    Called before ``POST /turns``, never after. A step that exists only once it
    has succeeded cannot record the case this table was added for: a hop that
    reached the executor, spent money, possibly acted through a tool, and never
    came back.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: DispatcherInstanceId = Field(...)
    step_index: int = Field(..., ge=0, lt=MAX_STEPS_PER_TASK)
    agent_name: AgentName = Field(
        ...,
        description=(
            "Resolved from server-side configuration by the caller. Nothing the "
            "task's source can express reaches this field."
        ),
    )
    brief: str = Field(default="", max_length=STEP_BRIEF_MAX_LENGTH)
    session_key: SessionKey | None = Field(
        default=None, description="Session this step will run on, when it is already open."
    )


class FinishAgentTaskStepRequest(BaseModel):
    """Close one step out, in the one transaction that also moves the task.

    The write order is the point of putting this on the server rather than
    leaving it to whoever writes the dispatcher: the step row is updated first
    and the task's counters second, in one transaction. A crash between them
    then leaves a completed step and a stale ``current_step``, which the resume
    rule tolerates exactly. A crash in the other order loses the output
    permanently.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: DispatcherInstanceId = Field(...)
    status: AgentTaskStepStatus = Field(
        ..., description="completed or failed. abandoned is written by the server on reclaim."
    )
    output_text: str | None = Field(default=None, max_length=STEP_OUTPUT_MAX_LENGTH)
    output_truncated: bool = Field(default=False)
    session_key: SessionKey | None = Field(default=None)
    turn_trace_id: str | None = Field(default=None, max_length=64)
    failure_code: str | None = Field(default=None, max_length=FAILURE_CODE_MAX_LENGTH)
    failure_detail: str | None = Field(default=None, max_length=FAILURE_DETAIL_MAX_LENGTH)

    @field_validator("status")
    @classmethod
    def _dispatcher_cannot_abandon(cls, status: AgentTaskStepStatus) -> AgentTaskStepStatus:
        if status in (AgentTaskStepStatus.RUNNING, AgentTaskStepStatus.ABANDONED):
            raise ValueError(
                "A step finishes as 'completed' or 'failed'. 'abandoned' is what the "
                "server writes when it reclaims a step from an expired lease, and "
                "'running' is not an ending."
            )
        return status


class AgentTaskStepResponse(BaseModel):
    """The step as recorded, the task it belongs to, and the files it found.

    ``files`` is what makes the envelope able to say "2 of 3 files were
    delivered". It is answered here rather than by a later read because the
    step is the point where the fetch happens and the envelope is built from
    what it returns. ``None`` means no fetch ran at all - the deployment has
    the Linear source off, or this step opened on something with no issue
    behind it - which is different from a fetch that found nothing.
    """

    step: AgentTaskStep = Field(...)
    task: AgentTaskSummary = Field(...)
    files: StepFilesSummary | None = Field(default=None)


# =============================================================================
# Task-level transitions
# =============================================================================


class FinishAgentTaskRequest(BaseModel):
    """Record how a task ended, from the process that was holding it."""

    model_config = ConfigDict(extra="forbid")

    instance_id: DispatcherInstanceId = Field(...)
    status: AgentTaskStatus = Field(..., description="One of the dispatcher-settable statuses.")
    failure_code: str | None = Field(default=None, max_length=FAILURE_CODE_MAX_LENGTH)
    failure_detail: str | None = Field(default=None, max_length=FAILURE_DETAIL_MAX_LENGTH)

    @field_validator("status")
    @classmethod
    def _within_the_dispatcher_s_vocabulary(cls, status: AgentTaskStatus) -> AgentTaskStatus:
        if status not in DISPATCHER_SETTABLE_TASK_STATUSES:
            allowed = ", ".join(sorted(s.value for s in DISPATCHER_SETTABLE_TASK_STATUSES))
            raise ValueError(f"A dispatcher may finish a task as one of: {allowed}.")
        return status


class CancelAgentTaskRequest(BaseModel):
    """An operator taking a queued task off the list before anything runs."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=FAILURE_DETAIL_MAX_LENGTH)


class ResolveAgentTaskRequest(BaseModel):
    """A human clearing a ``running_unknown`` task, which nothing else may do.

    The turn timed out and the plan cannot prove the invocation stopped, so the
    task holds its slot until somebody looks. ``requeue`` puts it back on the
    queue; otherwise it is recorded as failed. Both are decisions a person made
    after reading the transcript, which is the only thing that can tell them
    apart.
    """

    model_config = ConfigDict(extra="forbid")

    requeue: bool = Field(
        False, description="Put it back on the queue. Otherwise it is recorded as failed."
    )
    reason: str | None = Field(default=None, max_length=FAILURE_DETAIL_MAX_LENGTH)


class AgentTaskResponse(BaseModel):
    """One task after a transition."""

    task: AgentTaskDetail = Field(...)


__all__ = [
    "DEFAULT_WORKFLOW_KEY",
    "DISPATCHER_INSTANCE_MAX_LENGTH",
    "DISPATCHER_SETTABLE_TASK_STATUSES",
    "FAILURE_CODE_MAX_LENGTH",
    "FAILURE_DETAIL_MAX_LENGTH",
    "IMPORT_MAX_ITEMS",
    "MAX_STEPS_PER_TASK",
    "MAX_TURNS_PER_STEP",
    "RECLAIMABLE_TASK_STATUSES",
    "SOURCE_REF_MAX_LENGTH",
    "STEP_BRIEF_MAX_LENGTH",
    "STEP_OUTPUT_MAX_LENGTH",
    "TASK_BODY_MAX_LENGTH",
    "TASK_KEY_LENGTH",
    "TASK_TITLE_MAX_LENGTH",
    "TERMINAL_TASK_STATUSES",
    "WORKFLOW_KEY_MAX_LENGTH",
    "AgentTaskDetail",
    "AgentTaskResponse",
    "AgentTaskStatus",
    "AgentTaskStep",
    "AgentTaskStepResponse",
    "AgentTaskStepStatus",
    "AgentTaskSummary",
    "CancelAgentTaskRequest",
    "ClaimAgentTaskRequest",
    "ClaimAgentTaskResponse",
    "DispatcherInstanceId",
    "FinishAgentTaskRequest",
    "FinishAgentTaskStepRequest",
    "GetAgentTaskResponse",
    "HeartbeatAgentTaskRequest",
    "HeartbeatAgentTaskResponse",
    "ImportAgentTasksRequest",
    "ImportAgentTasksResponse",
    "ImportCandidate",
    "ImportItemsScope",
    "ImportMode",
    "ImportSkipCounts",
    "ImportTaskItem",
    "ListAgentTasksResponse",
    "RefsDigest",
    "ResolveAgentTaskRequest",
    "SourceRef",
    "StartAgentTaskStepRequest",
    "TaskKey",
    "TaskScopeKind",
    "TaskSourceKind",
    "WorkflowKey",
]
