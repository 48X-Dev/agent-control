"""The claim ledger: a local SQLite file, and nothing more than that.

Section 14 is explicit about what this is and what it is not. It is **not** the
``agent_tasks`` table, no server row is created by any of this, and the claim it
records **does not survive two processes**. Two dispatchers pointed at the same
source with different ledger files will both claim every item and both spend
money on it. With one dispatcher and one operator watching a terminal, that is
not yet a failure mode; the day it becomes one is the day this file is deleted
and the server's claim statement replaces it.

The seam is kept clean deliberately: nothing outside this module knows the
ledger is SQLite, the CLI signature does not mention it beyond a path, and
:class:`ClaimLedger` exposes the four verbs a server-side claim would expose.
Deleting this file should not change what ``dispatch once`` is called with.

One process-local guard is real and worth having: ``UNIQUE(source_kind, ref)``
means a single run cannot dispatch the same ref twice, and a re-run skips refs
that already reached a terminal state. That is resumability, not safety.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    source_kind   TEXT NOT NULL,
    ref           TEXT NOT NULL,
    agent_name    TEXT NOT NULL,
    status        TEXT NOT NULL,
    dry_run       INTEGER NOT NULL,
    session_key   TEXT,
    turn_trace_id TEXT,
    outcome_code  TEXT,
    detail        TEXT,
    claimed_at    TEXT NOT NULL,
    finished_at   TEXT,
    PRIMARY KEY (source_kind, ref)
);
"""


class ClaimStatus(StrEnum):
    """Where a claimed item got to.

    ``RUNNING_UNKNOWN`` is the 504 case and it is deliberately not terminal-
    looking. The turn outlived the server's patience, the invocation did not
    stop, and the only honest thing to record is that nobody knows.
    """

    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PAUSED_QUOTA = "paused_quota"
    RUNNING_UNKNOWN = "running_unknown"


@dataclass(frozen=True, slots=True)
class Claim:
    """One item this dispatcher took responsibility for."""

    source_kind: str
    ref: str
    agent_name: str
    status: ClaimStatus
    dry_run: bool
    session_key: str | None = None
    turn_trace_id: str | None = None
    outcome_code: str | None = None
    detail: str | None = None


class ClaimLedger:
    """Local record of what has been claimed and how it ended."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        with closing(self._conn.cursor()) as cursor:
            cursor.executescript(_SCHEMA)
        self._conn.commit()

    def __enter__(self) -> ClaimLedger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    @property
    def path(self) -> Path:
        return self._path

    def claim(self, *, source_kind: str, ref: str, agent_name: str, dry_run: bool) -> bool:
        """Take the item, or report that this ledger already has it.

        Returns ``False`` when a row exists and is terminal or in flight. This
        is an insert against a local file: another process holding its own
        ledger sees nothing, which is the whole limitation restated.

        ``paused_quota`` is the one status that re-claims, because the step did
        not run and is meant to resume. A ``claimed`` row does not: the turn may
        have reached the executor before this process stopped, and re-running it
        would spend twice for one item.
        """

        now = _now()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO claims "
                "(source_kind, ref, agent_name, status, dry_run, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source_kind, ref, agent_name, ClaimStatus.CLAIMED.value, int(dry_run), now),
            )
            if cursor.rowcount == 1:
                return True

            row = self._conn.execute(
                "SELECT status FROM claims WHERE source_kind = ? AND ref = ?",
                (source_kind, ref),
            ).fetchone()
            if row is None:
                return False
            if ClaimStatus(row["status"]) is ClaimStatus.PAUSED_QUOTA:
                self._conn.execute(
                    "UPDATE claims SET status = ?, claimed_at = ?, finished_at = NULL, "
                    "outcome_code = NULL, detail = NULL "
                    "WHERE source_kind = ? AND ref = ?",
                    (ClaimStatus.CLAIMED.value, now, source_kind, ref),
                )
                return True
            return False

    def record_session(self, *, source_kind: str, ref: str, session_key: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE claims SET session_key = ? WHERE source_kind = ? AND ref = ?",
                (session_key, source_kind, ref),
            )

    def finish(
        self,
        *,
        source_kind: str,
        ref: str,
        status: ClaimStatus,
        outcome_code: str | None = None,
        detail: str | None = None,
        turn_trace_id: str | None = None,
    ) -> None:
        """Close the claim out. Called for every terminal state, including the
        ones nobody wants."""

        with self._conn:
            self._conn.execute(
                "UPDATE claims SET status = ?, outcome_code = ?, detail = ?, "
                "turn_trace_id = COALESCE(?, turn_trace_id), finished_at = ? "
                "WHERE source_kind = ? AND ref = ?",
                (
                    status.value,
                    outcome_code,
                    detail,
                    turn_trace_id,
                    _now(),
                    source_kind,
                    ref,
                ),
            )

    def get(self, *, source_kind: str, ref: str) -> Claim | None:
        row = self._conn.execute(
            "SELECT * FROM claims WHERE source_kind = ? AND ref = ?", (source_kind, ref)
        ).fetchone()
        return _claim(row) if row is not None else None


def _claim(row: sqlite3.Row) -> Claim:
    return Claim(
        source_kind=row["source_kind"],
        ref=row["ref"],
        agent_name=row["agent_name"],
        status=ClaimStatus(row["status"]),
        dry_run=bool(row["dry_run"]),
        session_key=row["session_key"],
        turn_trace_id=row["turn_trace_id"],
        outcome_code=row["outcome_code"],
        detail=row["detail"],
    )


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
