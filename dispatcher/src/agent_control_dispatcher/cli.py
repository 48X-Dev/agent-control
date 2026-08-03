"""``agent-control-dispatch``.

The invocation section 14 specifies, and it does not change when the SQLite
ledger is deleted and the server's ``agent_tasks`` claim replaces it::

    agent-control-dispatch once --source file://tasks.yaml \\
        --agent researcher --max-tasks 3 --dry-run

``--dry-run`` is the default, so passing it is a statement of intent rather
than a switch. Turning it off takes ``--no-dry-run`` and prints a warning that
says what is and is not being enforced.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .dispatch import (
    DEFAULT_BRIEF,
    MAX_TASKS_CEILING,
    DispatchOptions,
    dispatch_once,
)
from .sources.file import SourceParseError

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_LEDGER_PATH = Path(".agent-control-dispatch/claims.sqlite3")
API_KEY_ENV = "AGENT_CONTROL_API_KEY"
BASE_URL_ENV = "AGENT_CONTROL_BASE_URL"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-control-dispatch",
        description=(
            "Dispatch tasks from a source to one Agent Control agent, one turn each. "
            "An operator starts this and watches it finish; it does not run unattended."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    once = subparsers.add_parser(
        "once",
        help="Make one pass over the source and stop.",
        description=(
            "One pass, at most --max-tasks items, one session and one turn per item. "
            "The claim ledger is a local SQLite file: it does not coordinate two "
            "dispatchers, and running two of these against one source runs everything "
            "twice."
        ),
    )
    once.add_argument(
        "--source",
        required=True,
        metavar="URI",
        help="Where tasks come from. file://tasks.yaml is the only scheme in this slice.",
    )
    once.add_argument(
        "--agent",
        required=True,
        metavar="NAME",
        help="Agent to run every task against. One agent, one step.",
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
        help="What this step's agent is asked to do. Operator text; not treated as data.",
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
        default=DEFAULT_LEDGER_PATH,
        metavar="PATH",
        help="Local claim ledger. Deleted the day agent_tasks lands.",
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
    return parser


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

    try:
        options = DispatchOptions(
            source_spec=args.source,
            agent_name=args.agent,
            base_url=args.server,
            api_key=api_key,
            ledger_path=args.ledger,
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


if __name__ == "__main__":
    raise SystemExit(main())
