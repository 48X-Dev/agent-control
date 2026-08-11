"""The poll-claim-execute-release cycle behind ``serve``.

It imports nothing and names no scope: only a row somebody already pressed play on reaches it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import random
import signal
import sys
from dataclasses import dataclass
from typing import TextIO

from agent_control_models.tasks import RECLAIMABLE_TASK_STATUSES, AgentTaskSummary

from .client import DEFAULT_TURN_TIMEOUT_SECONDS, DispatchClient, DispatchHTTPError, Disposition
from .dispatch import (
    MAX_TASKS_CEILING,
    DispatchOptions,
    TaskResult,
    _emit,
    _report_fleet_state,
    _run_one,
)
from .server_ledger import ServerTaskLedger
from .sources.base import SourceItem

DEFAULT_POLL_SECONDS = 5.0
"""Section 4: "a jittered 5s interval"."""

POLL_JITTER = 0.4
"""How far a sleep is allowed to wander, either side.

Two dispatchers started by one ``docker compose up`` otherwise poll in lockstep
forever, and every claim is a race one of them loses. Jitter does not make the
second one useful - section 18 is clear that it is not - it stops the two of
them beating on the same row at the same instant."""

HELD_POLL_SECONDS = 30.0
"""How long to wait when a fleet switch is thrown or the hour is spent.

Longer than the ordinary poll on purpose: nothing this process can do shortens
a pause, and five-second polling against a namespace somebody has just stopped
adds noise to the incident they are already handling."""

MAX_BACKOFF_SECONDS = 60.0
"""The ceiling on the retry sleep after consecutive server failures."""

RECLAIM_SWEEP_SECONDS = 60.0
"""How often an idle loop looks for rows whose holder died.

Only when the queue itself is empty: a dispatcher with work to do is not the
one that needs to go looking for more. A lapsed lease is half an hour old
before it is claimable, so noticing within a minute costs nothing."""

QUEUE_PAGE_SIZE = 50
"""How many queued rows one poll asks for. Bigger than ``--max-tasks`` so a
pass can skip rows belonging to another team and still find its own."""


@dataclass(frozen=True, slots=True)
class ServeOptions:
    """Everything the loop needs. Notably not a source, and not ``dry_run``."""

    base_url: str
    api_key: str
    agent_name: str | None = None
    """Fills the one gap ``once`` fills: a task whose implicit one-step plan
    resolved no agent. It is not an override and it cannot fill two steps."""
    workflow_key: str | None = None
    """Narrows which rows this dispatcher takes. It does **not** choose the
    workflow - the row carries the one it was imported under, and the plan is
    read from that - so it only leaves other rows for somebody else."""
    team_slug: str | None = None
    max_tasks: int = 1
    """How many tasks one pass may run before polling again."""
    poll_seconds: float = DEFAULT_POLL_SECONDS
    delete_sessions: bool = False
    print_envelope: bool = False
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.max_tasks < 1:
            raise ValueError("--max-tasks must be at least 1.")
        if self.max_tasks > MAX_TASKS_CEILING:
            raise ValueError(
                f"--max-tasks {self.max_tasks} exceeds the hard cap of "
                f"{MAX_TASKS_CEILING}. It bounds one pass, not the day: serve polls "
                "again straight after a pass that filled it, so a larger value only "
                "buys a longer stretch with no chance to notice a switch thrown."
            )
        if self.poll_seconds <= 0:
            raise ValueError("--poll-seconds must be greater than zero.")


class ShutdownRequest:
    """A stop that has been asked for but not forced."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self._event.set()

    async def sleep(self, seconds: float) -> None:
        """Wait, or return early because a stop was asked for."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=seconds)
        except TimeoutError:
            return


async def serve(
    options: ServeOptions,
    *,
    out: TextIO | None = None,
    shutdown: ShutdownRequest | None = None,
) -> int:
    """Poll, claim, run, repeat, until a stop is asked for."""

    stream = out if out is not None else sys.stdout
    stop = shutdown if shutdown is not None else ShutdownRequest()
    if shutdown is None:
        _install_signal_handlers(stop, stream=stream)

    _emit(stream, f"server     {options.base_url}")
    _emit(stream, f"agent      {options.agent_name or '(from the workflow)'}")
    _emit(stream, f"workflow   {options.workflow_key or '(any)'}")
    _emit(stream, f"team       {options.team_slug or '(any)'}")
    _emit(stream, f"poll       {options.poll_seconds:.0f}s, jittered")
    _emit(stream, f"max-tasks  {options.max_tasks} per pass (ceiling {MAX_TASKS_CEILING})")
    _emit(stream, "dry-run    per row, as created. This process cannot change it.")
    _emit(stream, "waiting for queued tasks; press play in the console\n")

    failures = 0
    announced: str | None = None
    announced_once = False
    next_sweep = 0.0

    async with DispatchClient(
        base_url=options.base_url,
        api_key=options.api_key,
        turn_timeout_seconds=options.turn_timeout_seconds,
    ) as client:
        ledger = ServerTaskLedger(client, team_slug=options.team_slug)
        clock = asyncio.get_running_loop().time
        while not stop.requested:
            started = clock()
            queue: list[AgentTaskSummary] = []
            try:
                # Into a buffer, not onto the terminal. `_report_fleet_state`
                # prints the budget line every time it runs, which is right for
                # a run an operator is watching start and wrong every five
                # seconds forever. The transition is the news, not the state.
                # `strict` is what stops a failed read reading as a clear one.
                held = await _report_fleet_state(client, stream=io.StringIO(), strict=True)
                if held is None:
                    page = await client.list_tasks(status="queued", limit=QUEUE_PAGE_SIZE)
                    queue = [task for task in page.tasks if _wanted(task, options)]
                    if not queue and clock() >= next_sweep:
                        queue = await _expired_leases(client, options)
                        next_sweep = clock() + RECLAIM_SWEEP_SECONDS
            except DispatchHTTPError as exc:
                if exc.disposition is Disposition.BLOCKED:
                    # A credential the server will not accept, or one without
                    # the privilege to read its own queue. Sleeping on it hides
                    # a configuration fault behind a process that looks alive
                    # and is doing nothing, which is the worst of both.
                    _emit(stream, f"fatal      the server refused this dispatcher: {exc}")
                    return 1
                failures += 1
                if failures == 1:
                    _emit(stream, f"retrying   the server did not answer: {exc}")
                await stop.sleep(_backoff_seconds(failures, options))
                continue
            if failures:
                _emit(stream, "polling    the server is answering again")
                failures = 0

            if held != announced or not announced_once:
                _emit(
                    stream,
                    f"holding    {held}" if held else "polling    claiming queued tasks",
                )
                announced, announced_once = held, True
            if held is not None:
                await stop.sleep(_jittered(HELD_POLL_SECONDS))
                continue
            if not queue:
                await stop.sleep(_jittered(options.poll_seconds))
                continue

            outcome = await _run_a_pass(
                client=client,
                ledger=ledger,
                queue=queue,
                options=options,
                stream=stream,
                stop=stop,
            )
            if outcome.server_failed:
                # Same curve as a failed queue read. A fixed backoff here would
                # claim a fresh row every twenty seconds for as long as the
                # server stayed broken, stranding each one in turn.
                failures += 1
                await stop.sleep(_backoff_seconds(failures, options))
            elif outcome.hold is not None:
                await stop.sleep(_jittered(outcome.hold))
            else:
                await stop.sleep(_pass_remainder(clock() - started, options))

    _emit(stream, f"\nstopped    {stop.reason or 'shutdown requested'}; nothing is claimed")
    return 0


@dataclass(frozen=True, slots=True)
class PassOutcome:
    """What one pass over a page of the queue established."""

    hold: float | None = None
    server_failed: bool = False


async def _run_a_pass(
    *,
    client: DispatchClient,
    ledger: ServerTaskLedger,
    queue: list[AgentTaskSummary],
    options: ServeOptions,
    stream: TextIO,
    stop: ShutdownRequest,
) -> PassOutcome:
    """Claim and run up to ``max_tasks`` of one page."""

    ran = 0
    for summary in queue:
        if stop.requested or ran >= options.max_tasks:
            break
        try:
            result = await _claim_and_run(
                client=client, ledger=ledger, summary=summary, options=options, stream=stream
            )
        except DispatchHTTPError as exc:
            if exc.disposition is Disposition.FLEET_STOPPED:
                # Somebody threw a switch after this pass's gate read said the
                # namespace was clear. Nothing was claimed, so the row keeps its
                # slot, and the next gate read is what reports it.
                return PassOutcome(hold=HELD_POLL_SECONDS)
            # The ledger write-back itself failed, which is the only refusal
            # left: everything inside the task's own path is closed into an
            # outcome. The row is claimed and stays at `running` until its lease
            # lapses - unavoidable, since closing it needs the server that is
            # not answering - so this counts as a server failure and the backoff
            # is what stops the loop stranding a second row behind the first.
            _emit(stream, f"retrying   the task could not be closed: {exc}")
            return PassOutcome(server_failed=True)
        if result is None:
            continue
        ran += 1
        if result.stop_reason is not None:
            _emit(stream, f"holding    {result.stop_reason}")
            return PassOutcome(hold=HELD_POLL_SECONDS)
    # A pass that lost every race waits: another dispatcher is working through
    # the same page. A pass that ran something falls to the caller's remainder,
    # which is immediate for work that took real time.
    return PassOutcome(hold=None if ran else options.poll_seconds)


async def _claim_and_run(
    *,
    client: DispatchClient,
    ledger: ServerTaskLedger,
    summary: AgentTaskSummary,
    options: ServeOptions,
    stream: TextIO,
) -> TaskResult | None:
    """Take one queued row and walk its chain. ``None`` means somebody else won."""

    source_kind = str(summary.source_kind)
    ref = str(summary.source_ref)
    ledger.adopt(source_kind=source_kind, ref=ref, task_key=str(summary.task_key))
    try:
        if not await ledger.claim(
            source_kind=source_kind,
            ref=ref,
            agent_name=options.agent_name or "",
            dry_run=summary.dry_run,
        ):
            # A lost race, and deliberately silent. Two dispatchers reading one
            # page in one order lose most of them, and a line per loss is a log
            # that reports normal operation as a problem.
            return None

        task = ledger.claimed_task(source_kind=source_kind, ref=ref)
        if task is None:  # pragma: no cover - claim() populates it or returns False
            return None

        # ``updated_at`` is deliberately left unset. The row carries the task's
        # own, not the issue's, and a source timestamp only orders a page -
        # which the queue has already done.
        item = SourceItem(ref=ref, title=task.title, body=task.body, url=task.source_url)
        run_options = DispatchOptions(
            source_spec=None,
            agent_name=options.agent_name,
            base_url=options.base_url,
            api_key=options.api_key,
            # Read off the claimed row, both of them. The workflow decides which
            # agents run and dry-run decides what they are asserted not to do,
            # and neither is this process's to pick: a serve started with the
            # wrong flags must not change what a human already agreed to.
            workflow_key=task.workflow_key,
            team_slug=task.team_slug,
            dry_run=task.dry_run,
            delete_sessions=options.delete_sessions,
            print_envelope=options.print_envelope,
            turn_timeout_seconds=options.turn_timeout_seconds,
        )
        return await _run_one(
            client=client,
            ledger=ledger,
            source_kind=source_kind,
            item=item,
            options=run_options,
            stream=stream,
        )
    finally:
        # The task is over on every path out of here: the run closes every
        # outcome it returns, and the one thing it raises on is a write-back the
        # server refused, where the lease is what recovers the row anyway. A
        # lost race leaves an adopted key that nothing else would drop. Without
        # this the process holds an issue body per task ever seen.
        ledger.forget(source_kind=source_kind, ref=ref)


async def _expired_leases(
    client: DispatchClient, options: ServeOptions
) -> list[AgentTaskSummary]:
    """Rows whose holder stopped existing, once their lease has lapsed."""
    now = dt.datetime.now(dt.UTC)
    stale: list[AgentTaskSummary] = []
    for status in sorted(status.value for status in RECLAIMABLE_TASK_STATUSES):
        page = await client.list_tasks(status=status, limit=QUEUE_PAGE_SIZE)
        stale.extend(
            task
            for task in page.tasks
            if _wanted(task, options)
            and task.lease_expires_at is not None
            and task.lease_expires_at < now
        )
    return stale


def _wanted(task: AgentTaskSummary, options: ServeOptions) -> bool:
    """Whether this dispatcher takes this row."""
    if options.team_slug is not None and task.team_slug != options.team_slug:
        return False
    if options.workflow_key is not None and task.workflow_key != options.workflow_key:
        return False
    return True


def _jittered(seconds: float) -> float:
    return seconds * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER)


def _pass_remainder(elapsed: float, options: ServeOptions) -> float:
    """What is left of one poll interval after a pass that ran something."""
    return max(0.0, _jittered(options.poll_seconds) - elapsed)


def _backoff_seconds(failures: int, options: ServeOptions) -> float:
    """Exponential, capped, jittered."""
    return _jittered(min(options.poll_seconds * 2.0**failures, MAX_BACKOFF_SECONDS))


def _install_signal_handlers(stop: ShutdownRequest, *, stream: TextIO) -> None:
    """Catch the first SIGTERM or SIGINT; let a second one through."""

    loop = asyncio.get_running_loop()

    def _handle(name: str, number: signal.Signals) -> None:
        if stop.requested:
            # Somebody who has decided not to wait. Reached when the removal
            # below lost its race, which happens when the next signal is
            # already pending as the handler is being swapped out.
            signal.signal(number, signal.SIG_DFL)
            signal.raise_signal(number)
            return
        _emit(
            stream,
            f"\nstopping   {name} received. No new work will be claimed; the task in "
            "flight finishes first. A second signal is not caught.",
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
