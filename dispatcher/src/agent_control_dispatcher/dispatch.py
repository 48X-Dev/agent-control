"""One pass over a source: claim, one session, one turn, one transcript.

This is the whole of slice 1's control flow and it is deliberately short.
Everything that would make it longer - a claim two processes can trust, a
budget the server enforces, a fleet stop, write-back, Linear - is listed in
section 14 as absent, and each one is a prerequisite for running this without
somebody watching. Nobody should read this module and conclude otherwise.

Three refusals stop the whole run rather than the one task, because in each
case continuing would make things worse rather than merely repeat a failure:

* ``paused_quota`` - the credential is over its ceiling. More turns is the one
  thing that cannot help.
* ``running_unknown`` - a turn timed out, so an invocation is still running and
  still spending. Starting another one puts a second concurrent invocation on
  an executor whose plugin has never been shown to be concurrency-safe
  (section 9.1), which is why per-agent concurrency is 1.
* ``blocked`` - no runtime binding, or the content-access predicate refused.
  Both are configuration, and both will refuse the next task identically.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from agent_control_models.sessions import TurnResponse

from .client import DispatchClient, DispatchHTTPError, Disposition
from .envelope import EnvelopeTooLongError, build_envelope
from .extract import StepOutputCode, extract_step_output, join_agent_text
from .ledger import ClaimLedger, ClaimStatus
from .sources.base import SourceItem
from .sources.file import resolve_source

MAX_TASKS_CEILING = 5
"""Hard cap on ``--max-tasks``. A larger value is refused, never clamped: an
operator who asked for twenty and silently got five would believe the other
fifteen ran."""

DEFAULT_BRIEF = (
    "Work this task and report what you found. There is one step and you are it: "
    "no other agent will continue after you."
)
"""The step brief when the operator does not supply one. Slice 1 has no
workflow, so there is exactly one step and its brief is operator text."""

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
"""The message would not fit in one turn. Recorded against the item but *not*
against the ledger: nothing was claimed and nothing was spent, so shortening the
brief and running again has to be able to pick the item back up."""

DENY_CHECK_UNAVAILABLE = "DENY_CHECK_UNAVAILABLE"
"""The turn ran and the deny query did not answer. A missing answer is not the
same as no deny (``client.deny_events_for_turn`` says so in its own docstring),
so the step is neither completed nor blocked - it is unclassified, and reporting
a possible refusal as a finding is the failure section 9.3 cares most about."""


@dataclass(frozen=True, slots=True)
class DispatchOptions:
    """Everything one ``dispatch once`` run needs."""

    source_spec: str
    agent_name: str
    base_url: str
    api_key: str
    ledger_path: Path
    max_tasks: int = 1
    dry_run: bool = True
    brief: str = DEFAULT_BRIEF
    delete_sessions: bool = False
    print_envelope: bool = False
    turn_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_tasks < 1:
            raise ValueError("--max-tasks must be at least 1.")
        if self.max_tasks > MAX_TASKS_CEILING:
            raise ValueError(
                f"--max-tasks {self.max_tasks} exceeds the hard cap of {MAX_TASKS_CEILING}. "
                "Refused rather than clamped: every turn spends real money, and this slice "
                "has no budget the server enforces."
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
    source = resolve_source(options.source_spec)
    report = RunReport()

    _emit(stream, f"source     {source.kind}://{source.path}")
    _emit(stream, f"agent      {options.agent_name}")
    _emit(stream, f"max-tasks  {options.max_tasks} (ceiling {MAX_TASKS_CEILING})")
    _emit(stream, f"ledger     {options.ledger_path}")
    _emit(stream, f"dry-run    {options.dry_run}")
    _emit(stream, f"note       {DRY_RUN_CAVEAT if options.dry_run else WET_RUN_CAVEAT}")
    _emit(stream, "")

    items = await source.poll(cursor=None)
    if not items:
        _emit(stream, "no items in source; nothing to do")
        return report

    with ClaimLedger(options.ledger_path) as ledger:
        async with DispatchClient(
            base_url=options.base_url,
            api_key=options.api_key,
            turn_timeout_seconds=options.turn_timeout_seconds,
        ) as client:
            for item in items:
                if len(report.results) >= options.max_tasks:
                    break
                try:
                    message = build_envelope(
                        item=item, brief=options.brief, source_kind=source.kind
                    )
                except EnvelopeTooLongError as exc:
                    # Before the claim on purpose: this costs nothing, so a
                    # shortened brief must be able to pick the item back up.
                    _emit(stream, f"\n--- {item.ref}: {item.title}")
                    _emit(stream, f"outcome    {ENVELOPE_TOO_LONG}: {exc}")
                    report.results.append(
                        TaskResult(
                            ref=item.ref,
                            status=ClaimStatus.FAILED,
                            outcome_code=ENVELOPE_TOO_LONG,
                            detail=str(exc),
                        )
                    )
                    continue
                claimed = _claim(
                    ledger, item, source_kind=source.kind, options=options, stream=stream
                )
                if not claimed:
                    continue
                result = await _run_one(
                    client=client,
                    ledger=ledger,
                    source_kind=source.kind,
                    item=item,
                    message=message,
                    options=options,
                    stream=stream,
                )
                report.results.append(result)
                if result.stop_reason is not None:
                    report.stop_reason = result.stop_reason
                    _emit(stream, f"\nstopping the run: {result.stop_reason}")
                    break

    if not report.results:
        _emit(stream, "nothing claimable; every item is already recorded in this ledger")
        return report

    _summarize(stream, report)
    return report


def _claim(
    ledger: ClaimLedger,
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
    """

    if ledger.claim(
        source_kind=source_kind,
        ref=item.ref,
        agent_name=options.agent_name,
        dry_run=options.dry_run,
    ):
        return True
    existing = ledger.get(source_kind=source_kind, ref=item.ref)
    state = existing.status.value if existing else "unknown"
    _emit(stream, f"skip {item.ref}: already {state} in this ledger")
    return False


async def _run_one(
    *,
    client: DispatchClient,
    ledger: ClaimLedger,
    source_kind: str,
    item: SourceItem,
    message: str,
    options: DispatchOptions,
    stream: TextIO,
) -> TaskResult:
    _emit(stream, f"\n--- {item.ref}: {item.title}")
    if options.print_envelope:
        _emit(stream, _indent(message))

    try:
        session_key = await client.create_session(
            agent_name=options.agent_name, title=f"dispatch {item.ref}"
        )
    except DispatchHTTPError as exc:
        return _fail(ledger, source_kind, item, exc, stream)

    ledger.record_session(source_kind=source_kind, ref=item.ref, session_key=session_key)
    _emit(stream, f"session    {session_key}")

    try:
        turn = await client.start_turn(session_key=session_key, message=message)
    except DispatchHTTPError as exc:
        return _fail(ledger, source_kind, item, exc, stream, session_key=session_key)

    try:
        deny_events = await client.deny_events_for_turn(
            agent_name=options.agent_name, turn=turn
        )
    except DispatchHTTPError as exc:
        return _unclassified(
            ledger, source_kind, item, turn=turn, exc=exc, session_key=session_key, stream=stream
        )

    output = extract_step_output(turn.messages, deny_events=deny_events)

    _emit(stream, f"trace      {turn.trace_id}  ({turn.duration_seconds:.1f}s)")
    _emit(stream, f"outcome    {output.code.value}")
    if output.control_name:
        _emit(stream, f"control    {output.control_name} ({output.detail})")
    if output.text:
        _emit(stream, _indent(output.text))

    status = _STATUS_FOR_OUTPUT[output.code]
    outcome_code = None if output.code is StepOutputCode.OK else output.code.value
    ledger.finish(
        source_kind=source_kind,
        ref=item.ref,
        status=status,
        outcome_code=outcome_code,
        detail=output.detail,
        turn_trace_id=turn.trace_id,
    )

    deleted = False
    if options.delete_sessions:
        try:
            await client.delete_session(session_key=session_key)
        except DispatchHTTPError as exc:
            # The step is already recorded. A transcript nobody asked to keep is
            # not a reason to lose the result of the turn that produced it, and
            # the key is still reported because the session is still there.
            _emit(stream, f"session    NOT deleted, still on the server: {exc}")
        else:
            deleted = True
            _emit(stream, "session    deleted")

    return TaskResult(
        ref=item.ref,
        status=status,
        outcome_code=outcome_code,
        detail=output.detail,
        session_key=None if deleted else session_key,
        turn_trace_id=turn.trace_id,
        duration_seconds=turn.duration_seconds,
        control_name=output.control_name,
        output_text=output.text,
    )


def _fail(
    ledger: ClaimLedger,
    source_kind: str,
    item: SourceItem,
    exc: DispatchHTTPError,
    stream: TextIO,
    *,
    session_key: str | None = None,
) -> TaskResult:
    status = _STATUS_FOR[exc.disposition]
    ledger.finish(
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


def _unclassified(
    ledger: ClaimLedger,
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
    ledger.finish(
        source_kind=source_kind,
        ref=item.ref,
        status=ClaimStatus.FAILED,
        outcome_code=DENY_CHECK_UNAVAILABLE,
        detail=detail,
        turn_trace_id=turn.trace_id,
    )
    _emit(stream, f"outcome    {DENY_CHECK_UNAVAILABLE}: {detail}")
    text = join_agent_text(turn.messages)
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
