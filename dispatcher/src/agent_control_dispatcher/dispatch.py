"""One pass over a source: claim, then walk the task's chain of agents.

Each hop is one session, one turn and one step row. The control flow is
deliberately short and the ledger is behind a protocol, so this module does not
know whether the claim it just took was arbitrated by Postgres or by a file on
disk. The default is the former (:mod:`.server_ledger`), which is what makes
the claim survive a second dispatcher and a dead one.

**The agents in a chain never talk to each other, and this module is the whole
reason they appear to.** When A's work feeds B, what happens is: this process
receives A's ``TurnResponse`` over HTTP, extracts text from it, writes that
text to ``agent_task_steps``, formats it into a new prompt under the same
untrusted framing the issue body gets, and starts a separate turn on a separate
session against a separate executor. A never learns B exists. B receives text
from what looks to it like an operator. There is no agent-to-agent channel in
this system and nothing here adds one - which is not a limitation to apologise
for, but the property that makes every hop individually controlled, individually
haltable and individually visible.

Which agent runs which hop is decided on the server and only read here. The
plan comes back with each step's agent already resolved from the workflow row
and the team row; the one gap this process fills is a single unresolved step on
a task with no configured workflow, from ``--agent``. Everything else is
refused, because an agent chosen in the process an operator started is one
argument away from an agent chosen by whoever filed the issue.

The budget and the fleet stop now exist, and neither is in this process. The
namespace's hourly turn ceiling, the pause and the executor kill switch are
refusals inside the server's ``_acquire_turn``, so a dispatcher in a retry loop
or a second one started by somebody else meets them too. Nothing here is the
enforcement point for any of them; what this module does is stop cleanly when
one of them answers.

What is still absent, and each one is still a prerequisite for running this
unattended: a write-back of any kind, and a play button. Linear is a *read*: a
source can produce items and neither source can record anything back onto it.

Four refusals stop the whole run rather than the one task, because in each
case continuing would make things worse rather than merely repeat a failure:

* ``paused_quota`` - the credential is over its ceiling. More turns is the one
  thing that cannot help.
* a fleet ceiling - the namespace is paused, its executors are halted, its hour
  is spent, or the agent is already running something. Every remaining task
  would meet the same refusal, and each one keeps its slot.
* ``running_unknown`` - a turn timed out, so an invocation is still running and
  still spending. Starting another one puts a second concurrent invocation on
  an executor whose plugin has never been shown to be concurrency-safe
  (section 9.1), which is why per-agent concurrency is 1.
* ``blocked`` - no runtime binding, no agent the plan could resolve, or the
  content-access predicate refused. All three are configuration, and all three
  refuse the next task identically.

Two failures end one task without ending the run, because both are about that
task's own content: a control block, and an empty report. Neither is forwarded
to the next agent. Handing B a refusal or a blank as though it were a finding
is the worst-quality failure available here, so the chain stops where it went
wrong and says so.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from agent_control_models.sessions import TurnResponse

from .client import DispatchClient, DispatchHTTPError, Disposition
from .envelope import EnvelopeTooLongError, PriorReport, build_envelope
from .extract import StepOutputCode, extract_step_output, join_agent_text
from .ledger import ClaimLedger, ClaimStatus, LocalTaskLedger, TaskLedger
from .server_ledger import ServerTaskLedger
from .sources.base import ScopedTaskSource, SourceItem
from .sources.resolve import build_source

MAX_TASKS_CEILING = 5
"""Hard cap on ``--max-tasks``. A larger value is refused, never clamped: an
operator who asked for twenty and silently got five would believe the other
fifteen ran."""

DEFAULT_BRIEF = (
    "Work this task and report what you found. There is one step and you are it: "
    "no other agent will continue after you."
)
"""The step brief when the operator does not supply one, on a task with no
configured workflow. One step, and its brief is operator text."""

CHAIN_STEP_FALLBACK_BRIEF = (
    "Do your part of this task and report what you found. Your report is the only "
    "thing that carries forward, so write it for somebody who cannot see your work."
)
"""What a configured step gets when its author left the brief empty.

It does not promise a next agent and it does not name one, because a step must
not be told who follows it. Section 9's rule is that the researcher never
learns the writer exists, and a brief saying "the writer will use this" is the
cheapest possible way to break that with prose."""

NO_AGENT_SELECTED = "NO_AGENT_SELECTED"
"""No step of the plan resolved to an agent, and this process will not pick one.

Terminal and never retried, because it is configuration: the same plan refuses
identically on the next pass, and a loop retrying it produces a wall of
identical failures. A human pins the agent on the workflow step or sets
``default_agent_name`` on the team."""

CHAIN_ALREADY_COMPLETE = "CHAIN_ALREADY_COMPLETE"
"""Reclaimed a task whose every step had already completed.

Not a configuration problem, which is why it does not borrow
``NO_AGENT_SELECTED``: the plan resolved perfectly and every agent it names
already ran. What went wrong is bookkeeping - the last hop landed and the task
row never moved - so the operator needs to be pointed at the step rows, not at
the workflow. It ends this task and **not** the run: the next task's steps are
its own, and stopping here would strand a whole batch behind one row that had
in fact finished."""

PRIOR_REPORT_MISSING = "PRIOR_REPORT_MISSING"
"""A step past the first has no completed report before it.

Reachable when a task is reclaimed mid-chain and the step it resumes at follows
one that was abandoned rather than completed. The task fails there rather than
running with an empty prior-report block, which section 9.4 forbids for a
reason worth restating: an agent handed "the previous agent reported:
(nothing)" does not stop, it invents the missing work and reports it
confidently."""

MULTI_STEP_NEEDS_THE_SERVER_LEDGER_CODE = "MULTI_STEP_NEEDS_THE_SERVER_LEDGER"
"""Its own code rather than ``NO_AGENT_SELECTED``, which it is not.

Currently unreachable - ``DispatchOptions.__post_init__`` refuses ``--workflow``
with ``--ledger``, and a local run's plan never has a second step - and kept as
defence in depth against a future ledger that answers ``session_task_key``. An
unreachable refusal that records the wrong reason is worse than no refusal at
all, because it is the one that gets believed if it ever does fire."""

MULTI_STEP_NEEDS_THE_SERVER_LEDGER = (
    "a workflow with more than one step needs the server's agent_task_steps, and "
    "--ledger opted out of it. The local file records one session and one output "
    "per item, so the second agent would be handed a report that had already been "
    "overwritten by its own"
)

DRY_RUN_CAVEAT = (
    "dry-run is an assertion about this deployment, not a proof. Section 12.3's "
    "canary is not in this slice, so nothing here has verified that the agent's "
    "tools are read-only. An operator is watching this terminal; that is the control."
)

WET_RUN_CAVEAT = (
    "running with --no-dry-run. Nothing in this slice bounds what the agent's tools "
    "may do, and no canary has proven anything about them. Watch the terminal."
)

ENVELOPE_TOO_LONG = "ENVELOPE_TOO_LONG"
"""The message would not fit in one turn.

Reached from two places, and they leave the item in different states on
purpose. Before the claim, where the brief is ``--brief`` and this process
already knows it, nothing is claimed and nothing is spent - so shortening the
brief and running again picks the item straight back up. Inside a chain, where
the brief comes from the plan and the plan comes from the claimed task, the hop
is recorded as failed: the claim is the only way to have learned the brief at
all, and a task that failed for a legible reason is better than one that
silently stayed queued."""

_CLAIM_REFUSED_BY_A_FLEET_STOP = (
    "the server refused the claim on a fleet ceiling. Every remaining task would "
    "be refused the same way, and nothing was taken out of the queue"
)
"""Why a run ends when a switch is thrown *while* it is running.

The opening read (:func:`_report_fleet_state`) cannot catch this: it happened
before the button was pressed. What the claim refusal must not do is arrive
looking like an ordinary lost race - "already held by another dispatcher" - and
let the run walk through every remaining item reporting a reason that is false
and then finish looking clean."""

DENY_CHECK_UNAVAILABLE = "DENY_CHECK_UNAVAILABLE"
"""The turn ran and the deny query did not answer. A missing answer is not the
same as no deny (``client.deny_events_for_turn`` says so in its own docstring),
so the step is neither completed nor blocked - it is unclassified, and reporting
a possible refusal as a finding is the failure section 9.3 cares most about."""


@dataclass(frozen=True, slots=True)
class DispatchOptions:
    """Everything one ``dispatch once`` run needs."""

    source_spec: str
    agent_name: str | None
    """The agent for a task with no configured workflow.

    Optional now that workflows exist, and it fills exactly one gap: the
    implicit one-step plan, which pins no agent by construction. It is *not* a
    way to override a workflow - a plan that names its own agents ignores this
    entirely - and it cannot fill more than one unresolved step, because one
    agent running two hops of a chain and being reported as a hand-off is a
    lie the ledger would then keep."""
    base_url: str
    api_key: str
    workflow_key: str | None = None
    """Which configured workflow to import these tasks under.

    Recorded on the row at import and read back from the server when the task
    is claimed. The dispatcher never resolves it locally: agent selection is
    server-side configuration, and a plan assembled in this process would be
    one argument away from a plan an issue label could reach."""
    ledger_path: Path | None = None
    """Set to fall back to the local SQLite ledger. ``None`` - the default -
    uses the server's ``agent_tasks``, which is the only claim two dispatchers
    can contend for. The local file is kept for an offline poke at a YAML file
    and coordinates nothing; :mod:`agent_control_dispatcher.ledger` says so at
    length."""
    team_slug: str | None = None
    """Which Agent Control team's scope a Linear read runs under. Required by
    the milestone source, refused by the file source, and never a selector: it
    resolves the ``linear_team_key`` the server filters on and nothing else."""
    max_tasks: int = 1
    dry_run: bool = True
    brief: str = DEFAULT_BRIEF
    delete_sessions: bool = False
    print_envelope: bool = False
    turn_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.agent_name is None and self.workflow_key is None:
            raise ValueError(
                "Pass --agent, --workflow, or both. With neither, a task whose "
                "workflow pins no agent and whose team has no default_agent_name "
                "has nobody to run it, and this process will not choose one: "
                "agents differ in system prompt, in bound controls and in tools, "
                "so choosing the agent is choosing the blast radius."
            )
        if self.workflow_key is not None and self.ledger_path is not None:
            raise ValueError(
                "--workflow and --ledger are mutually exclusive. A workflow is "
                "server-side configuration resolved against the server's task row, "
                "and --ledger opts out of having one."
            )
        if self.max_tasks < 1:
            raise ValueError("--max-tasks must be at least 1.")
        if self.max_tasks > MAX_TASKS_CEILING:
            raise ValueError(
                f"--max-tasks {self.max_tasks} exceeds the hard cap of {MAX_TASKS_CEILING}. "
                "Refused rather than clamped: every turn spends real money, and an "
                "operator who asked for twenty and silently got five would believe "
                "the other fifteen ran. The namespace's hourly ceiling is the bound "
                "that cannot be edited from here; this one is a seatbelt on one run."
            )


@dataclass(frozen=True, slots=True)
class TaskResult:
    """What happened to one item."""

    ref: str
    status: ClaimStatus
    outcome_code: str | None = None
    detail: str | None = None
    session_key: str | None = None
    turn_trace_id: str | None = None
    duration_seconds: float | None = None
    control_name: str | None = None
    output_text: str = ""
    stop_reason: str | None = None
    """Set when this result should end the run rather than the task. A control
    block is *not* one of those: it is about this task's content, and the next
    task's content is different."""


@dataclass
class RunReport:
    """The run, for the operator and for whatever reads this next."""

    results: list[TaskResult] = field(default_factory=list)
    stop_reason: str | None = None

    @property
    def stopped_early(self) -> bool:
        return self.stop_reason is not None


async def dispatch_once(options: DispatchOptions, *, out: TextIO | None = None) -> RunReport:
    """Claim up to ``max_tasks`` items and run one turn against each."""

    stream = out if out is not None else sys.stdout
    report = RunReport()

    async with DispatchClient(
        base_url=options.base_url,
        api_key=options.api_key,
        turn_timeout_seconds=options.turn_timeout_seconds,
    ) as client:
        # The client is opened first because a scoped source reads through it.
        # It costs nothing on a file run: no request is made until one is asked
        # for, so a YAML run against an unreachable server still works.
        source = build_source(options.source_spec, team_slug=options.team_slug, reader=client)

        _emit(stream, f"source     {source.describe()}")
        _emit(stream, f"agent      {options.agent_name or '(from the workflow)'}")
        _emit(stream, f"workflow   {options.workflow_key or '(none; one implicit step)'}")
        _emit(stream, f"max-tasks  {options.max_tasks} (ceiling {MAX_TASKS_CEILING})")
        _emit(stream, f"ledger     {_describe_ledger(options)}")
        _emit(stream, f"dry-run    {options.dry_run}")
        _emit(stream, f"note       {DRY_RUN_CAVEAT if options.dry_run else WET_RUN_CAVEAT}")

        stopped = await _report_fleet_state(client, stream=stream)
        _emit(stream, "")
        if stopped is not None:
            report.stop_reason = stopped
            _emit(stream, f"stopping the run: {stopped}")
            return report

        items = await source.poll(cursor=None)
        if isinstance(source, ScopedTaskSource) and source.scope_report is not None:
            for line in source.scope_report.lines():
                _emit(stream, line)
            _emit(stream, "")
        if not items:
            _emit(stream, "no items in source; nothing to do")
            return report

        # Built here and not at the top of the run: a source that could not be
        # read has produced nothing to claim, and a ledger constructed anyway
        # would leave a file - or a row - for work nobody has seen.
        ledger = _build_ledger(options, client=client)
        try:
            # One row per item before anything is claimed. On the server ledger
            # this is the import, preview then commit against the digest of the
            # preview; locally it does nothing, because there is nobody else to
            # tell. Either way no turn has been started and no money spent.
            await ledger.register(
                source_kind=source.kind,
                items=items,
                dry_run=options.dry_run,
                workflow_key=options.workflow_key,
            )
            for item in items:
                if len(report.results) >= options.max_tasks:
                    break
                unsendable = _refuse_an_unsendable_envelope_before_claiming(
                    ledger, item, source_kind=source.kind, options=options
                )
                if unsendable is not None:
                    _emit(stream, f"\n--- {item.ref}: {item.title}")
                    _emit(stream, f"outcome    {ENVELOPE_TOO_LONG}: {unsendable}")
                    report.results.append(
                        TaskResult(
                            ref=item.ref,
                            status=ClaimStatus.FAILED,
                            outcome_code=ENVELOPE_TOO_LONG,
                            detail=unsendable,
                        )
                    )
                    continue
                try:
                    claimed = await _claim(
                        ledger, item, source_kind=source.kind, options=options, stream=stream
                    )
                except DispatchHTTPError as exc:
                    if exc.disposition is not Disposition.FLEET_STOPPED:
                        raise
                    # A switch was thrown after this run's opening read said the
                    # namespace was clear, which is what an incident looks like
                    # from here. Nothing was claimed and nothing was spent, so
                    # every remaining item keeps its slot.
                    report.stop_reason = f"{_CLAIM_REFUSED_BY_A_FLEET_STOP} ({exc})"
                    _emit(stream, f"\n--- {item.ref}: {item.title}")
                    _emit(stream, f"outcome    not claimed: {exc}")
                    _emit(stream, f"\nstopping the run: {report.stop_reason}")
                    break
                if not claimed:
                    continue
                result = await _run_one(
                    client=client,
                    ledger=ledger,
                    source_kind=source.kind,
                    item=item,
                    options=options,
                    stream=stream,
                )
                report.results.append(result)
                if result.stop_reason is not None:
                    report.stop_reason = result.stop_reason
                    _emit(stream, f"\nstopping the run: {result.stop_reason}")
                    break
        finally:
            await ledger.aclose()

    if not report.results:
        _emit(stream, "nothing claimable; every item is already recorded in this ledger")
        return report

    _summarize(stream, report)
    return report


async def _report_fleet_state(client: DispatchClient, *, stream: TextIO) -> str | None:
    """Print the namespace's ceilings, and say so if a switch is already thrown.

    An optimisation and nothing more. Every refusal it anticipates is enforced
    on the server's turn path, where this process cannot reach it, and a run
    that skipped this check would meet exactly the same answer one HTTP call
    later. What it buys is that a paused namespace does not get four rows
    imported, four sessions opened and four refusals logged before anybody sees
    the word "paused".

    A server that cannot answer is not treated as a stop. Refusing to run
    because a *read* failed would put an advisory call on the critical path,
    which is the shape of the mistake this whole phase exists to avoid.
    """

    try:
        state = await client.read_dispatch_state()
    except DispatchHTTPError as exc:
        _emit(stream, f"budget     unknown ({exc}); the server enforces it either way")
        return None

    budget = state.budget
    _emit(
        stream,
        f"budget     {budget.turns_remaining_this_hour} of "
        f"{budget.max_turns_per_hour} turns left this hour, "
        f"{budget.tasks_remaining_this_hour} of {budget.max_tasks_per_hour} tasks",
    )
    if state.executors_halted:
        return (
            "executors are halted in this namespace"
            f"{_because(state.executors_halted_reason)}. An admin releases it"
        )
    if state.paused:
        return (
            "dispatch is paused in this namespace"
            f"{_because(state.paused_reason)}. An admin resumes it"
        )
    if budget.turns_remaining_this_hour < 1:
        return (
            "this namespace has spent its hourly turn allowance; it rolls at "
            f"{budget.window_resets_at.isoformat()}"
        )
    return None


def _because(reason: str | None) -> str:
    return f" ({reason})" if reason else ""


def _build_ledger(options: DispatchOptions, *, client: DispatchClient) -> TaskLedger:
    """The server's ``agent_tasks``, unless a path asks for the local file.

    The default moved with the ledger and the flag did not: ``--ledger`` is now
    how an operator asks for the old, uncoordinated behaviour, and the
    invocation section 14 published still means what it said.
    """

    if options.ledger_path is not None:
        return LocalTaskLedger(ClaimLedger(options.ledger_path))
    return ServerTaskLedger(client, team_slug=options.team_slug)


def _describe_ledger(options: DispatchOptions) -> str:
    """Name the ledger for the header without constructing it."""

    if options.ledger_path is not None:
        return f"local sqlite {options.ledger_path} (coordinates nothing)"
    return "server agent_tasks (atomic claim, leased, reclaimable)"


async def _claim(
    ledger: TaskLedger,
    item: SourceItem,
    *,
    source_kind: str,
    options: DispatchOptions,
    stream: TextIO,
) -> bool:
    """Take one item immediately before running it, never earlier.

    Claiming the whole batch up front strands whatever the run does not reach:
    a stop reason on task 1 would leave tasks 2..N sitting at ``claimed``, which
    is neither terminal nor re-claimable, so a re-run skips them permanently.
    One claim per dispatch keeps the un-run items untouched and the window
    between claiming and the first HTTP call microseconds wide.

    On the server ledger a refusal is a lost race rather than an error, and it
    is not retried: the next poll hands out a different task, and hammering a
    row another dispatcher holds produces conflict logs and no throughput.
    """

    if await ledger.claim(
        source_kind=source_kind,
        ref=item.ref,
        # A label on the local row and nothing more. Which agent actually runs
        # is decided per step by the plan, so this is empty when the operator
        # named no fallback and the workflow names its own.
        agent_name=options.agent_name or "",
        dry_run=options.dry_run,
    ):
        return True
    existing = await ledger.get(source_kind=source_kind, ref=item.ref)
    state = existing.status.value if existing else "held by another dispatcher"
    _emit(stream, f"skip {item.ref}: already {state} in this ledger")
    return False


@dataclass(frozen=True, slots=True)
class ChainStep:
    """One hop of a plan, with its agent already decided by the server.

    ``agent_name`` is never ``None`` here: an unresolved step blocks the task
    before any of this is built, because filling it in would be this process
    choosing an agent.
    """

    index: int
    agent_name: str
    brief: str
    required_output: str = "text"


@dataclass(frozen=True, slots=True)
class ChainRefusal:
    """Why a claimed task will not run, in the words the ledger keeps.

    ``code`` is carried rather than assumed. Every refusal here used to be
    written down as ``NO_AGENT_SELECTED``, which is the audit record telling an
    operator to go and pin an agent on a workflow that had already resolved one
    for every step - and the ledger keeps that answer after the terminal
    scrollback is gone.

    ``stops_the_run`` is separate from the code for the same reason. A refusal
    about *configuration* refuses the next task identically, so the run ends. A
    refusal about *this row* says nothing about the next one, and ending the
    batch on it strands work that would have run.
    """

    code: str
    detail: str
    stops_the_run: bool = True


async def _plan_chain(
    *,
    client: DispatchClient,
    ledger: TaskLedger,
    source_kind: str,
    ref: str,
    options: DispatchOptions,
) -> tuple[list[ChainStep], ChainRefusal | None]:
    """Read the plan for one claimed task, or say why it cannot run.

    Returns ``(steps, refusal)``. Every refusal this function returns is a
    ``blocked`` outcome about configuration, so the next pass produces it
    identically, nothing retries it, and the run ends.

    **Agent selection happens on the server and is only read here.** The plan
    comes back with each step's agent resolved from the workflow row and the
    team row, and with the ones it could not resolve *named* rather than filled
    in. The single gap this process is allowed to fill is a one-step plan whose
    only step resolved to nobody, filled from ``--agent``: that is the implicit
    workflow every team has by default, and the operator naming the agent at
    the terminal is the path slice 1 shipped. Two or more unresolved steps and
    one ``--agent`` is refused rather than reused, because one agent running
    two hops of a chain and being recorded as a hand-off is a lie the ledger
    would then keep.
    """
    task_key = ledger.session_task_key(source_kind=source_kind, ref=ref)
    if task_key is None:
        # The local ledger. There is no server row to hold a plan, so there is
        # one step and the operator named its agent.
        if options.agent_name is None:
            return [], ChainRefusal(
                code=NO_AGENT_SELECTED,
                detail=(
                    "no task row to read a plan from and no --agent to run. The local "
                    "ledger holds no workflow."
                ),
            )
        return (
            [ChainStep(index=0, agent_name=options.agent_name, brief=options.brief)],
            None,
        )

    plan = await client.get_task_plan(task_key=task_key)
    unresolved = list(plan.unresolved_step_indexes)
    if unresolved and not (
        len(plan.steps) == 1 and len(unresolved) == 1 and options.agent_name
    ):
        return [], ChainRefusal(
            code=NO_AGENT_SELECTED,
            detail=(
                f"workflow '{plan.workflow_key}' resolved no agent for "
                f"step{'s' if len(unresolved) > 1 else ''} "
                f"{', '.join(str(index) for index in unresolved)}. Pin an agent on the "
                "workflow step, or set default_agent_name on the team. This process "
                "does not choose one."
            ),
        )

    if len(plan.steps) > 1 and options.ledger_path is not None:
        return [], ChainRefusal(
            code=MULTI_STEP_NEEDS_THE_SERVER_LEDGER_CODE,
            detail=MULTI_STEP_NEEDS_THE_SERVER_LEDGER,
        )

    steps: list[ChainStep] = []
    for step in plan.steps:
        agent_name = step.agent_name or options.agent_name
        if agent_name is None:
            # Unreachable: the refusal above covers every unresolved step. Kept
            # as a refusal rather than an assertion because the alternative
            # reading of a None here is "run something", and there is no
            # something that would be right.
            return [], ChainRefusal(
                code=NO_AGENT_SELECTED,
                detail=f"step {step.step_index} has no agent and none can be supplied.",
            )
        steps.append(
            ChainStep(
                index=step.step_index,
                agent_name=agent_name,
                brief=step.brief or _fallback_brief(plan.implicit, options),
                required_output=str(step.required_output),
            )
        )
    return steps, None


def _fallback_brief(implicit: bool, options: DispatchOptions) -> str:
    """What a step with an empty brief is asked to do.

    The operator's ``--brief`` only applies to the implicit one-step plan,
    where it is the whole configuration there is. A configured workflow whose
    author left a brief empty gets the neutral chain wording instead: letting
    a command-line flag fill in for an ADMIN-authored step would put operator
    text into the one part of the turn message that is *not* framed as
    untrusted data, on a step somebody else configured.
    """
    return options.brief if implicit else CHAIN_STEP_FALLBACK_BRIEF


def _refuse_an_unsendable_envelope_before_claiming(
    ledger: TaskLedger,
    item: SourceItem,
    *,
    source_kind: str,
    options: DispatchOptions,
) -> str | None:
    """Reject an over-long envelope before the claim, where that is possible.

    Only on the path where this process already knows the brief: the local
    ledger's single step, whose brief is ``--brief``. It costs nothing to
    refuse there, so a shortened brief has to be able to pick the item back up,
    which means not claiming it.

    On the server path the brief comes from the plan and the plan comes from
    the claimed task, so the same check happens per step inside the chain and
    the task records a failure instead. That is the honest ordering: guessing
    here with ``--brief`` would refuse items whose configured brief is fine.
    """
    if ledger.session_task_key(source_kind=source_kind, ref=item.ref) is not None:
        return None
    try:
        build_envelope(item=item, brief=options.brief, source_kind=source_kind)
    except EnvelopeTooLongError as exc:
        return str(exc)
    return None


async def _run_one(
    *,
    client: DispatchClient,
    ledger: TaskLedger,
    source_kind: str,
    item: SourceItem,
    options: DispatchOptions,
    stream: TextIO,
) -> TaskResult:
    """Walk one task's chain: one session, one turn and one step row per hop.

    **The agents in this loop never meet.** Each hop is an ordinary ``POST
    /turns`` against a session opened for one agent, and what the next agent
    receives is text this process read out of the previous response, wrote to
    ``agent_task_steps``, and formatted into a fresh prompt under the same
    untrusted framing the issue body gets. A never learns B exists; B receives
    text from what looks to it like an operator. Everything that resembles
    collaboration is this function holding both ends, and that is the property
    that makes every hop individually controlled, haltable and visible.

    A hop that is not ``ok`` ends the task there. The rest of the chain is
    never started, and the reason is quality rather than caution: forwarding a
    control's refusal, or an empty report, as if it were a finding is the worst
    failure available here.
    """
    _emit(stream, f"\n--- {item.ref}: {item.title}")

    steps, refusal = await _plan_chain(
        client=client, ledger=ledger, source_kind=source_kind, ref=item.ref, options=options
    )
    if refusal is not None:
        return await _blocked(ledger, source_kind, item, refusal=refusal, stream=stream)

    resume = ledger.resume_step_index(source_kind=source_kind, ref=item.ref)
    remaining = [step for step in steps if step.index >= resume]
    _emit(
        stream,
        f"chain      {len(steps)} step(s): "
        + " -> ".join(step.agent_name for step in steps)
        + (f", resuming at {resume}" if resume else ""),
    )
    if not remaining:
        # Every step is already completed, which a reclaim of a task whose last
        # hop landed but whose task row never moved looks like. Nothing to run,
        # and nothing to invent. It is emphatically *not* a configuration
        # refusal: the plan resolved an agent for every step and every one of
        # them ran, so the run carries on to the next task.
        return await _blocked(
            ledger,
            source_kind,
            item,
            refusal=ChainRefusal(
                code=CHAIN_ALREADY_COMPLETE,
                detail=(
                    f"every step of this task is already completed and it resumed at "
                    f"{resume}. Nothing left to run; read the step rows for what they "
                    f"produced."
                ),
                stops_the_run=False,
            ),
            stream=stream,
        )

    opened_sessions: list[str] = []
    deleted: set[str] = set()
    result: TaskResult | None = None
    try:
        for step in remaining:
            result = await _run_step(
                client=client,
                ledger=ledger,
                source_kind=source_kind,
                item=item,
                step=step,
                is_last=step is remaining[-1],
                options=options,
                stream=stream,
                opened_sessions=opened_sessions,
            )
            if result.status is not ClaimStatus.COMPLETED:
                break
    finally:
        # After the chain, never between hops. A session is what an operator
        # watches and what a halt is bound to, and deleting one mid-chain would
        # take a running task's own history away while it was still running.
        if options.delete_sessions:
            deleted = await _delete_sessions(client, opened_sessions, stream=stream)

    assert result is not None  # noqa: S101 - `remaining` is non-empty above.
    if result.session_key is not None and result.session_key in deleted:
        # Reported as gone only when it actually went. A delete that failed
        # leaves the transcript on the server, and an operator who was told the
        # key was cleaned up would not go looking for it.
        result = replace(result, session_key=None)
    return result


async def _run_step(
    *,
    client: DispatchClient,
    ledger: TaskLedger,
    source_kind: str,
    item: SourceItem,
    step: ChainStep,
    is_last: bool,
    options: DispatchOptions,
    stream: TextIO,
    opened_sessions: list[str],
) -> TaskResult:
    """One hop: build the envelope, open a session, run a turn, record it."""

    prior: PriorReport | None = None
    if step.index > 0:
        prior = await ledger.prior_report(
            source_kind=source_kind, ref=item.ref, step_index=step.index
        )
        if prior is None:
            detail = (
                f"step {step.index} follows no completed step, so there is no report to "
                "hand it. The previous hop was abandoned or produced nothing. Not "
                "starting this agent with an empty prior-report block."
            )
            await ledger.finish(
                source_kind=source_kind,
                ref=item.ref,
                status=ClaimStatus.FAILED,
                outcome_code=PRIOR_REPORT_MISSING,
                detail=detail,
                step_index=step.index,
            )
            _emit(stream, f"outcome    {PRIOR_REPORT_MISSING}: {detail}")
            return TaskResult(
                ref=item.ref,
                status=ClaimStatus.FAILED,
                outcome_code=PRIOR_REPORT_MISSING,
                detail=detail,
            )

    _emit(stream, f"step {step.index}     {step.agent_name}")
    try:
        message = build_envelope(
            item=item, brief=step.brief, source_kind=source_kind, prior=prior
        )
    except EnvelopeTooLongError as exc:
        await ledger.finish(
            source_kind=source_kind,
            ref=item.ref,
            status=ClaimStatus.FAILED,
            outcome_code=ENVELOPE_TOO_LONG,
            detail=str(exc),
            step_index=step.index,
        )
        _emit(stream, f"outcome    {ENVELOPE_TOO_LONG}: {exc}")
        return TaskResult(
            ref=item.ref,
            status=ClaimStatus.FAILED,
            outcome_code=ENVELOPE_TOO_LONG,
            detail=str(exc),
        )
    if options.print_envelope:
        _emit(stream, _indent(message))

    try:
        session_key = await client.create_session(
            agent_name=step.agent_name,
            title=f"dispatch {item.ref} step {step.index}",
            # Bound at creation, not recorded afterwards. The server reads this
            # column on the turn path to tell a fleet turn from a human one, so
            # a session opened without it is one that no dispatch ceiling
            # applies to and that no operator without an admin key can halt.
            task_key=ledger.session_task_key(source_kind=source_kind, ref=item.ref),
        )
    except DispatchHTTPError as exc:
        return await _fail(ledger, source_kind, item, exc, stream)
    opened_sessions.append(session_key)

    # The step row is opened here, before the turn and after the session
    # exists, so a dispatcher that dies mid-turn leaves a record that a hop
    # reached the executor rather than leaving nothing at all. The heartbeat
    # rides along inside it, which is what keeps a four-hop chain from being
    # reclaimed underneath itself while its third turn is running.
    await ledger.record_session(
        source_kind=source_kind,
        ref=item.ref,
        session_key=session_key,
        agent_name=step.agent_name,
        brief=step.brief,
        step_index=step.index,
    )
    _emit(stream, f"session    {session_key}")

    try:
        turn = await client.start_turn(session_key=session_key, message=message)
    except DispatchHTTPError as exc:
        return await _fail(ledger, source_kind, item, exc, stream, session_key=session_key)

    try:
        deny_events = await client.deny_events_for_turn(
            agent_name=step.agent_name, turn=turn
        )
    except DispatchHTTPError as exc:
        return await _unclassified(
            ledger, source_kind, item, turn=turn, exc=exc, session_key=session_key, stream=stream
        )

    output = extract_step_output(
        turn.messages, deny_events=deny_events, required_output=step.required_output
    )

    _emit(stream, f"trace      {turn.trace_id}  ({turn.duration_seconds:.1f}s)")
    _emit(stream, f"outcome    {output.code.value}")
    if output.control_name:
        _emit(stream, f"control    {output.control_name} ({output.detail})")
    elif output.detail:
        # Every other refusal in this module prints why. A bare
        # ``EMPTY_STEP_OUTPUT`` on the terminal leaves the operator - who is
        # the only control this slice has - to guess whether the agent said
        # nothing or the extraction lost it.
        _emit(stream, f"reason     {output.detail}")
    if output.text:
        _emit(stream, _indent(output.text))

    status = _STATUS_FOR_OUTPUT[output.code]
    outcome_code = None if output.code is StepOutputCode.OK else output.code.value

    if status is ClaimStatus.COMPLETED and not is_last:
        # The hop closes; the task does not. A two-agent task reaching
        # ``completed`` when its researcher finished would tell an operator the
        # writer had run.
        await ledger.complete_step(
            source_kind=source_kind,
            ref=item.ref,
            step_index=step.index,
            output_text=output.text,
            turn_trace_id=turn.trace_id,
        )
    else:
        await ledger.finish(
            source_kind=source_kind,
            ref=item.ref,
            status=status,
            outcome_code=outcome_code,
            detail=output.detail,
            turn_trace_id=turn.trace_id,
            output_text=output.text,
            step_index=step.index,
        )

    return TaskResult(
        ref=item.ref,
        status=status,
        outcome_code=outcome_code,
        detail=output.detail,
        session_key=session_key,
        turn_trace_id=turn.trace_id,
        duration_seconds=turn.duration_seconds,
        control_name=output.control_name,
        output_text=output.text,
    )


async def _delete_sessions(
    client: DispatchClient, session_keys: Sequence[str], *, stream: TextIO
) -> set[str]:
    """Delete every session this task opened, once the task has ended.

    Section 6's ``session_retention_seconds`` grace is deliberately absent, and
    it is absent rather than forgotten: ``dispatch once`` is one pass that then
    exits, so a fifteen-minute grace here would be fifteen minutes of a process
    sitting idle holding nothing anybody can see. The grace belongs to the
    long-running loop that does not exist yet, and this flag is off by default
    precisely because the transcript is what an operator reads.

    Returns the keys that actually went. A failed delete is reported and not
    retried: the steps are already recorded, a transcript nobody asked to keep
    is not a reason to lose the result of the turns that produced it, and the
    session is still on the server, so the run must keep naming it.
    """
    deleted: set[str] = set()
    for session_key in session_keys:
        try:
            await client.delete_session(session_key=session_key)
        except DispatchHTTPError as exc:
            _emit(stream, f"session    {session_key} NOT deleted, still on the server: {exc}")
        else:
            deleted.add(session_key)
            _emit(stream, f"session    {session_key} deleted")
    return deleted


async def _blocked(
    ledger: TaskLedger,
    source_kind: str,
    item: SourceItem,
    *,
    refusal: ChainRefusal,
    stream: TextIO,
) -> TaskResult:
    """Nothing ran on this task, and the refusal says which kind of nothing.

    ``blocked`` rather than ``failed`` on purpose: the work was never
    attempted, and a loop retrying it produces the same answer forever.

    Whether the *run* stops is the refusal's own answer rather than this
    function's. A plan that resolved no agent refuses the next task
    identically, so the batch ends; a task that turned out to have already
    finished says nothing at all about the next one, and ending the batch there
    would strand every task behind it for the least alarming reason available.
    """
    await ledger.finish(
        source_kind=source_kind,
        ref=item.ref,
        status=ClaimStatus.BLOCKED,
        outcome_code=refusal.code,
        detail=refusal.detail,
    )
    _emit(
        stream,
        f"outcome    {ClaimStatus.BLOCKED.value} ({refusal.code}): {refusal.detail}",
    )
    return TaskResult(
        ref=item.ref,
        status=ClaimStatus.BLOCKED,
        outcome_code=refusal.code,
        detail=refusal.detail,
        stop_reason=(
            _STOP_REASON_FOR[Disposition.BLOCKED] if refusal.stops_the_run else None
        ),
    )


async def _fail(
    ledger: TaskLedger,
    source_kind: str,
    item: SourceItem,
    exc: DispatchHTTPError,
    stream: TextIO,
    *,
    session_key: str | None = None,
) -> TaskResult:
    status = _STATUS_FOR[exc.disposition]
    await ledger.finish(
        source_kind=source_kind,
        ref=item.ref,
        status=status,
        outcome_code=exc.error_code or str(exc.status_code),
        detail=exc.detail,
    )
    _emit(stream, f"outcome    {status.value}: {exc}")
    if exc.retry_after_seconds is not None:
        _emit(stream, f"retry-after {exc.retry_after_seconds:.0f}s (server-supplied)")
    return TaskResult(
        ref=item.ref,
        status=status,
        outcome_code=exc.error_code or str(exc.status_code),
        detail=exc.detail,
        session_key=session_key,
        stop_reason=_STOP_REASON_FOR.get(exc.disposition),
    )


async def _unclassified(
    ledger: TaskLedger,
    source_kind: str,
    item: SourceItem,
    *,
    turn: TurnResponse,
    exc: DispatchHTTPError,
    session_key: str,
    stream: TextIO,
) -> TaskResult:
    """The turn ran; the deny query did not answer.

    The step is failed rather than completed, and the run stops. Treating the
    silence as "no deny" would forward a possible refusal downstream as a
    finding; carrying on would spend another turn nobody can classify either.
    """

    detail = (
        f"The turn completed but the control-execution query failed ({exc}). Whether a "
        f"control blocked this turn is unknown. The transcript is on session {session_key}."
    )
    text = join_agent_text(turn.messages)
    await ledger.finish(
        source_kind=source_kind,
        ref=item.ref,
        status=ClaimStatus.FAILED,
        outcome_code=DENY_CHECK_UNAVAILABLE,
        detail=detail,
        turn_trace_id=turn.trace_id,
        output_text=text,
    )
    _emit(stream, f"outcome    {DENY_CHECK_UNAVAILABLE}: {detail}")
    if text:
        _emit(stream, _indent(text))
    return TaskResult(
        ref=item.ref,
        status=ClaimStatus.FAILED,
        outcome_code=DENY_CHECK_UNAVAILABLE,
        detail=detail,
        session_key=session_key,
        turn_trace_id=turn.trace_id,
        duration_seconds=turn.duration_seconds,
        output_text=text,
        stop_reason="the deny query stopped answering; further turns cannot be classified",
    )


_STATUS_FOR: dict[Disposition, ClaimStatus] = {
    Disposition.FAILED: ClaimStatus.FAILED,
    Disposition.RETRY: ClaimStatus.FAILED,
    Disposition.PAUSED_QUOTA: ClaimStatus.PAUSED_QUOTA,
    # A fleet stop leaves the task exactly where a quota refusal does:
    # ``paused_quota`` is the one non-terminal status the claim statement will
    # hand back out, and nothing reached the executor, so resuming at the same
    # step is provably safe. The two dispositions differ in what the operator is
    # told, not in what the ledger records.
    Disposition.FLEET_STOPPED: ClaimStatus.PAUSED_QUOTA,
    Disposition.BLOCKED: ClaimStatus.BLOCKED,
    Disposition.RUNNING_UNKNOWN: ClaimStatus.RUNNING_UNKNOWN,
}

_STATUS_FOR_OUTPUT: dict[StepOutputCode, ClaimStatus] = {
    StepOutputCode.OK: ClaimStatus.COMPLETED,
    StepOutputCode.EMPTY_STEP_OUTPUT: ClaimStatus.FAILED,
    StepOutputCode.BLOCKED_BY_CONTROL: ClaimStatus.BLOCKED,
}

_STOP_REASON_FOR: dict[Disposition, str] = {
    Disposition.PAUSED_QUOTA: "quota exceeded; more turns cannot help",
    Disposition.FLEET_STOPPED: (
        "the server refused this turn on a fleet ceiling. Every remaining task "
        "would be refused the same way, and the tasks keep their slots"
    ),
    Disposition.RUNNING_UNKNOWN: (
        "a turn timed out and its invocation did not stop. Not starting another "
        "against the same agent"
    ),
    Disposition.BLOCKED: "configuration refusal; the next task would be refused identically",
}


def _summarize(stream: TextIO, report: RunReport) -> None:
    _emit(stream, "\n--- summary")
    for result in report.results:
        line = f"{result.ref:<12} {result.status.value}"
        if result.outcome_code:
            line += f" ({result.outcome_code})"
        _emit(stream, line)
    if not report.results:
        _emit(stream, "(nothing ran)")


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def _emit(stream: TextIO, line: str) -> None:
    print(line, file=stream, flush=True)
