"""``agent-control-dispatch``.

The invocation section 14 specifies. It did not change when the server's
``agent_tasks`` claim replaced the SQLite ledger, which was the promise::

    agent-control-dispatch once --source file://tasks.yaml \\
        --agent researcher --max-tasks 3 --dry-run

    agent-control-dispatch once --source linear-milestone:<id> --team operations \\
        --agent ops_runbook_agent --max-tasks 3 --dry-run

``--dry-run`` is the default, so passing it is a statement of intent rather
than a switch. Turning it off takes ``--no-dry-run`` and prints a warning that
says what is and is not being enforced.

A chain of agents is ``--workflow`` instead of ``--agent``::

    agent-control-dispatch once --source linear-milestone:<id> --team marketing \\
        --workflow research-then-write --max-tasks 3 --dry-run

``--agent`` names the agent for a task with **no** configured workflow, which is
the implicit one-step plan every team has by default. It is not an override: a
workflow that pins its own agents ignores it entirely, and it cannot fill more
than one unresolved step. Agent selection is server-side configuration - the
workflow step, then the team's ``default_agent_name`` - and picking one here
would put the decision in the wrong process (plan section 8).

``--workflow`` and ``--ledger`` are mutually exclusive. A chain needs
``agent_task_steps`` to carry one agent's report to the next, and the local
SQLite file records one output per item.

Neither invocation writes anything to Linear. There is no comment, no state
change, no label, and no route on the server that would accept one.

The claim now lives in ``agent_tasks``: atomic, leased, and reclaimable from a
dispatcher that died. ``--ledger`` still exists and now means the opposite of
what it used to - it opts *out* of that, back to a local SQLite file that
coordinates nothing.

``serve`` is the other half of the play button, and it takes no ``--source``::

    agent-control-dispatch serve --team operations --poll-seconds 5

It polls ``GET /agent-tasks?status=queued`` and claims what it finds, so a
press in the console runs within seconds without anybody opening a terminal. It
imports nothing and names no scope: section 4 makes the human press the whole
authorization for milestone scope, so a process a scheduler can start must not
be able to construct one. ``--dry-run`` is absent for the same shape of reason
- each row carries the value it was created with, and this reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .client import DispatchHTTPError
from .dispatch import (
    DEFAULT_BRIEF,
    MAX_TASKS_CEILING,
    DispatchOptions,
    dispatch_once,
)
from .loop import DEFAULT_POLL_SECONDS, ServeOptions, serve
from .sources.file import SourceParseError
from .sources.linear import LinearScopeError

DEFAULT_BASE_URL = "http://localhost:8000"
API_KEY_ENV = "AGENT_CONTROL_API_KEY"
BASE_URL_ENV = "AGENT_CONTROL_BASE_URL"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-control-dispatch",
        description=(
            "Run Agent Control tasks. `once` reads a source, imports what it finds "
            "and makes one pass. `serve` reads no source at all: it polls the queue "
            "for rows somebody pressed play on and runs them until it is stopped."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    once = subparsers.add_parser(
        "once",
        help="Make one pass over the source and stop.",
        description=(
            "One pass, at most --max-tasks items, one session and one turn per item. "
            "The claim is a row in agent_tasks: two dispatchers contend for it and one "
            "wins, and a dispatcher that dies has its tasks reclaimed once its lease "
            "expires. Nothing is written back to the source, in either direction, by "
            "any code path in this tool."
        ),
    )
    once.add_argument(
        "--source",
        required=True,
        metavar="URI",
        help=(
            "Where tasks come from: file://tasks.yaml, or linear-milestone:<id> "
            "with --team. The Linear source only reads; it never writes to Linear."
        ),
    )
    once.add_argument(
        "--agent",
        default=None,
        metavar="NAME",
        help=(
            "Agent for a task with no configured workflow. Fills the implicit "
            "one-step plan and nothing else: a workflow that names its own agents "
            "ignores this. Required unless --workflow is given."
        ),
    )
    once.add_argument(
        "--workflow",
        default=None,
        metavar="KEY",
        help=(
            "Configured workflow these tasks run under, from "
            "PUT /agent-workflows/{key}. The agents come from the workflow and "
            "the team, never from here and never from the issue."
        ),
    )
    once.add_argument(
        "--team",
        default=None,
        metavar="SLUG",
        help=(
            "Agent Control team whose Linear team key scopes the read. Required "
            "by linear-milestone:, refused for a file. An unlinked team is a 409."
        ),
    )
    once.add_argument(
        "--max-tasks",
        type=int,
        default=1,
        metavar="N",
        help=f"How many items to run. Hard cap {MAX_TASKS_CEILING}; a larger value is refused.",
    )
    dry = once.add_mutually_exclusive_group()
    dry.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Default. Asserts the agent's tools are read-only; nothing here proves it.",
    )
    dry.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Drop the assertion. Nothing in this slice bounds what the tools may do.",
    )
    once.add_argument(
        "--brief",
        default=DEFAULT_BRIEF,
        help=(
            "What the agent is asked to do, on the implicit one-step plan only. A "
            "configured workflow carries a brief per step, written by whoever holds "
            "agent_workflows.write, and this flag does not override it."
        ),
    )
    once.add_argument(
        "--server",
        default=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL),
        metavar="URL",
        help=f"Agent Control base URL (env {BASE_URL_ENV}).",
    )
    once.add_argument(
        "--ledger",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Opt out of the server ledger and use a local SQLite file, which "
            "does not coordinate two dispatchers: two of these with two files "
            "both claim every item and both spend money on it."
        ),
    )
    once.add_argument(
        "--delete-sessions",
        action="store_true",
        help=(
            "Delete each session when its task ends (section 6). Off by default in this "
            "slice because the transcript is what the operator reads."
        ),
    )
    once.add_argument(
        "--print-envelope",
        action="store_true",
        help="Print the exact turn message sent for each task.",
    )
    once.add_argument(
        "--turn-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Client-side read timeout. A timeout is never retried: the invocation continues.",
    )

    _add_serve(subparsers)
    return parser


def _add_serve(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The long-running loop: poll the queue, claim, run, repeat.

    It is the other half of the play button. The console writes a queued row;
    this claims it within seconds and runs it, so nobody has to open a terminal
    to make a press mean something.

    **It reads no source and imports nothing.** There is no ``--source`` here
    and there cannot be one: a milestone scope is authorized by a human
    pressing play over the issues themselves, so a process a scheduler starts
    must not be able to construct one. Every row this touches already existed.
    """

    serve_parser = subparsers.add_parser(
        "serve",
        help="Poll the queue for tasks somebody pressed play on, and run them.",
        description=(
            "Claim and run queued tasks until stopped. Nothing is imported and no "
            "source is read: rows arrive from the console's play button, or from a "
            "cron `once`, and this claims what it finds. SIGTERM and SIGINT stop it "
            "claiming and let the task in flight finish, so a restart does not leave "
            "a claimed task with no owner."
        ),
    )
    serve_parser.add_argument(
        "--agent",
        default=None,
        metavar="NAME",
        help=(
            "Agent for a task whose plan resolved nobody, exactly as in `once`: it "
            "fills the implicit one-step plan and nothing else. A workflow that "
            "names its own agents ignores it."
        ),
    )
    serve_parser.add_argument(
        "--workflow",
        default=None,
        metavar="KEY",
        help=(
            "Only claim rows carrying this workflow key. It does not choose or "
            "override the workflow - the row carries the one it was imported under, "
            "and the plan is read from that - it leaves other rows for somebody else."
        ),
    )
    serve_parser.add_argument(
        "--team",
        default=None,
        metavar="SLUG",
        help="Only claim rows belonging to this Agent Control team. Default: any.",
    )
    serve_parser.add_argument(
        "--max-tasks",
        type=int,
        default=1,
        metavar="N",
        help=(
            f"Tasks per pass, before the queue is polled again. Hard cap "
            f"{MAX_TASKS_CEILING}; a larger value is refused."
        ),
    )
    serve_parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        metavar="SECONDS",
        help=(
            f"How long an idle pass waits, jittered so two dispatchers do not "
            f"poll in lockstep. Default {DEFAULT_POLL_SECONDS:.0f}."
        ),
    )
    serve_parser.add_argument(
        "--server",
        default=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL),
        metavar="URL",
        help=f"Agent Control base URL (env {BASE_URL_ENV}).",
    )
    serve_parser.add_argument(
        "--delete-sessions",
        action="store_true",
        help="Delete each session when its task ends (section 6).",
    )
    serve_parser.add_argument(
        "--print-envelope",
        action="store_true",
        help="Print the exact turn message sent for each task.",
    )
    serve_parser.add_argument(
        "--turn-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Client-side read timeout. A timeout is never retried: the loop continues.",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(
            f"{API_KEY_ENV} is not set. The dispatcher authenticates as an ordinary "
            "caller and has no credential of its own.",
            file=sys.stderr,
        )
        return 2

    if args.command == "serve":
        return _serve(args, api_key=api_key)

    try:
        options = DispatchOptions(
            source_spec=args.source,
            agent_name=args.agent,
            base_url=args.server,
            api_key=api_key,
            workflow_key=args.workflow,
            ledger_path=args.ledger,
            team_slug=args.team,
            max_tasks=args.max_tasks,
            dry_run=args.dry_run,
            brief=args.brief,
            delete_sessions=args.delete_sessions,
            print_envelope=args.print_envelope,
            turn_timeout_seconds=args.turn_timeout,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        report = asyncio.run(dispatch_once(options))
    except SourceParseError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LinearScopeError as exc:
        # The scope was not read, so nothing was dispatched from it. Exiting
        # non-zero matters here: an unreadable milestone and an empty one must
        # not look the same to whoever ran this.
        print(str(exc), file=sys.stderr)
        return 2
    except DispatchHTTPError as exc:
        # Only a failure of the scope read reaches here; a per-task refusal is
        # recorded against its own item and the run continues.
        print(f"Could not read the source scope: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "\ninterrupted. Any turn already in flight is still running on the executor; "
            "this process stopping does not stop it.",
            file=sys.stderr,
        )
        return 130

    if report.stopped_early:
        return 1
    return 0 if all(result.status.value == "completed" for result in report.results) else 1


def _serve(args: argparse.Namespace, *, api_key: str) -> int:
    """Run the loop until a signal stops it.

    A task that failed is not a failure of this process, so it exits 0. The
    exit code has to mean "the dispatcher broke", because it is what a restart
    policy reads, and a loop that exited non-zero every time an agent tripped a
    control would restart on the system working correctly.
    """

    try:
        options = ServeOptions(
            base_url=args.server,
            api_key=api_key,
            agent_name=args.agent,
            workflow_key=args.workflow,
            team_slug=args.team,
            max_tasks=args.max_tasks,
            poll_seconds=args.poll_seconds,
            delete_sessions=args.delete_sessions,
            print_envelope=args.print_envelope,
            turn_timeout_seconds=args.turn_timeout,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        return asyncio.run(serve(options))
    except KeyboardInterrupt:
        # Only reachable from a second interrupt: the first is caught inside the
        # loop, which is what lets the task in flight close its own row.
        print(
            "\ninterrupted. Any turn already in flight is still running on the executor; "
            "this process stopping does not stop it.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
