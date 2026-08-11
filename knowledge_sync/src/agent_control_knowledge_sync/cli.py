"""``agent-knowledge-sync``: one pass, a pass per interval, or what the mirror holds."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy.exc import SQLAlchemyError

from .config import (
    SYNC_INTERVAL_ENV,
    SYNC_INTERVAL_SECONDS_DEFAULT,
    ConfigError,
    SyncConfig,
)
from .drive_client import DriveError
from .lease import LeaseHeldError
from .serve import serve
from .sync import CorpusStatus, RunCounters, SyncFailedError, corpus_sessions, read_status, run_once

STALENESS_WARN_SECONDS = 86_400
"""Mirrors the server's default. This process cannot read the server's env."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-knowledge-sync",
        description=(
            "Mirror the allowlisted Drive subtree into the company knowledge corpus. "
            "`once` makes a single pass and stops; `serve` makes one every sync "
            "interval until it is signalled; `status` reads the corpus and reports "
            "what is in it and how fresh it is."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "once",
        help="Make one pass over the corpus root and stop.",
        description=(
            "Claim the singleton lease, walk the root on a first run or replay the "
            "changes feed after that, and print what the pass did. Exit 0 means the "
            "pass completed - refused documents are counted and named, not fatal. "
            "Exit 2 means it did not run: a held lease, an unreachable root, a "
            "corpus schema this build does not write, or missing configuration."
        ),
    )
    subparsers.add_parser(
        "serve",
        help="Make a pass every sync interval until signalled.",
        description=(
            "The same pass `once` makes, repeated on a jittered interval "
            f"({SYNC_INTERVAL_ENV}, default {SYNC_INTERVAL_SECONDS_DEFAULT}s), until "
            "SIGTERM or SIGINT. A failed pass is logged and retried at the next "
            "interval, so a held lease or an unreachable Drive does not end the "
            "process. The signal stops it starting a pass and lets the one in flight "
            "finish. Exit 0 means a clean stop; exit 2 a fault no interval fixes, "
            "such as a corpus schema this build does not write."
        ),
    )
    subparsers.add_parser(
        "status",
        help="Print what the corpus holds and how stale it is.",
        description=(
            "Reads the corpus and prints it. Exit 1 when the mirror is stale or a "
            "source is failing, so a cron wrapper can notice; exit 2 when the "
            "corpus could not be read at all."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("AGENT_KNOWLEDGE_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = SyncConfig.from_env(os.environ)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "status":
        return _status(config)
    if args.command == "serve":
        return _serve(config)
    return _once(config)


def _serve(config: SyncConfig) -> int:
    try:
        return asyncio.run(serve(config))
    except KeyboardInterrupt:
        # A second interrupt: the first is caught and asks the loop to finish.
        print("\ninterrupted. The cursor stays where the last committed batch left it.")
        return 130


def _once(config: SyncConfig) -> int:
    try:
        counters = asyncio.run(run_once(config))
    except LeaseHeldError as exc:
        print(f"{exc} Nothing was walked, so no cursor moved.", file=sys.stderr)
        return 2
    except (SyncFailedError, DriveError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"Could not reach the corpus database: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted. The cursor stays where the last committed batch left it.")
        return 130

    print(format_counters(counters))
    return 0


def _status(config: SyncConfig) -> int:
    try:
        status = asyncio.run(_read(config))
    except (SQLAlchemyError, OSError) as exc:
        print(f"Could not read the corpus: {exc}", file=sys.stderr)
        return 2

    print(format_status(status))
    stale = status.stale_seconds is None or status.stale_seconds > STALENESS_WARN_SECONDS
    return 1 if stale or status.sources_failing else 0


async def _read(config: SyncConfig) -> CorpusStatus:
    async with corpus_sessions(config) as sessions:
        return await read_status(sessions)


def format_counters(counters: RunCounters) -> str:
    """One line of totals, plus one line per refusal code when there were any."""
    head = (
        f"seen {counters.seen}  indexed {counters.indexed}  "
        f"unchanged {counters.unchanged}  tombstoned {counters.tombstoned}  "
        f"refused {counters.refused}"
    )
    if not counters.refusals_by_code:
        return head
    refusals = "  ".join(
        f"{code} {count}" for code, count in sorted(counters.refusals_by_code.items())
    )
    return f"{head}\nrefusals: {refusals}"


def format_status(status: CorpusStatus) -> str:
    """The corpus, its freshness and its last run, in three lines."""
    lines = [
        f"documents {status.documents} in {status.chunks} chunks",
        f"sources {status.sources_enabled} enabled, {status.sources_failing} failing",
        f"verified {_age(status.stale_seconds)}",
    ]
    if status.last_run_status is not None:
        run = f"last run {status.last_run_status}"
        if status.last_run_error_code:
            run += f" ({status.last_run_error_code})"
        lines.append(run)
    if status.stale_seconds is None or status.stale_seconds > STALENESS_WARN_SECONDS:
        lines.append(
            "WARNING: the mirror has not verified within "
            f"{_duration(STALENESS_WARN_SECONDS)}; recent changes may be missing."
        )
    return "\n".join(lines)


def _age(seconds: int | None) -> str:
    if seconds is None:
        return "never - an enabled source has not completed a run"
    return f"{_duration(seconds)} ago"


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


if __name__ == "__main__":
    raise SystemExit(main())
