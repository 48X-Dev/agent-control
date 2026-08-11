"""The claim ledger, and the local SQLite one that is no longer the default."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Protocol

from agent_control_models.attachments import StepFilesSummary

from .envelope import PriorReport
from .sources.base import SourceItem

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
    """Where a claimed item got to."""

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


class TaskLedger(Protocol):
    """What a ledger has to be able to do, whichever side of the wire it is on."""

    async def register(
        self,
        *,
        source_kind: str,
        items: Sequence[SourceItem],
        dry_run: bool,
        workflow_key: str | None = None,
    ) -> None:
        """Make sure a row exists for each item, without claiming any of them."""

    async def claim(
        self, *, source_kind: str, ref: str, agent_name: str, dry_run: bool
    ) -> bool:
        """Take one item, or report that this ledger cannot."""

    def resume_step_index(self, *, source_kind: str, ref: str) -> int:
        """Which step of the chain to start at, from what the claim returned."""

    def session_task_key(self, *, source_kind: str, ref: str) -> str | None:
        """The task row a session for this item must be bound to, if there is one."""

    async def record_session(
        self,
        *,
        source_kind: str,
        ref: str,
        session_key: str,
        agent_name: str,
        brief: str,
        step_index: int | None = None,
    ) -> StepFilesSummary | None:
        """Record the session this item's step is running on, before its turn."""

    async def complete_step(
        self,
        *,
        source_kind: str,
        ref: str,
        step_index: int,
        output_text: str,
        turn_trace_id: str | None = None,
    ) -> None:
        """Close one hop of a chain that carries on afterwards."""

    async def prior_report(
        self, *, source_kind: str, ref: str, step_index: int
    ) -> PriorReport | None:
        """What the step before ``step_index`` reported, for the envelope."""

    async def finish(
        self,
        *,
        source_kind: str,
        ref: str,
        status: ClaimStatus,
        outcome_code: str | None = None,
        detail: str | None = None,
        turn_trace_id: str | None = None,
        output_text: str | None = None,
        step_index: int | None = None,
    ) -> None:
        """Close the claim out, however it ended."""

    async def get(self, *, source_kind: str, ref: str) -> Claim | None:
        """What this ledger currently believes about one item."""

    async def aclose(self) -> None:
        """Release whatever this ledger holds."""


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
        """Take the item, or report that this ledger already has it."""

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


class LocalTaskLedger:
    """:class:`ClaimLedger` behind the :class:`TaskLedger` protocol."""

    def __init__(self, ledger: ClaimLedger) -> None:
        self._ledger = ledger

    def session_task_key(self, *, source_kind: str, ref: str) -> str | None:
        """Nothing to bind to. A local run creates no server row."""
        del source_kind, ref
        return None

    def resume_step_index(self, *, source_kind: str, ref: str) -> int:
        """Always zero. A local run has one step and does not resume into one."""
        del source_kind, ref
        return 0

    async def prior_report(
        self, *, source_kind: str, ref: str, step_index: int
    ) -> PriorReport | None:
        """Always ``None``. This table holds no step output to report."""
        del source_kind, ref, step_index
        return None

    async def complete_step(
        self,
        *,
        source_kind: str,
        ref: str,
        step_index: int,
        output_text: str,
        turn_trace_id: str | None = None,
    ) -> None:
        """Unreachable: a multi-step plan is refused before it gets here."""
        del output_text, turn_trace_id
        raise NotImplementedError(
            f"The local ledger records one step per item, so step {step_index} of "
            f"{source_kind}:{ref} has nowhere to go. Drop --ledger to run a chain "
            "against the server's agent_task_steps."
        )

    async def register(
        self,
        *,
        source_kind: str,
        items: Sequence[SourceItem],
        dry_run: bool,
        workflow_key: str | None = None,
    ) -> None:
        del source_kind, items, dry_run, workflow_key

    async def claim(
        self, *, source_kind: str, ref: str, agent_name: str, dry_run: bool
    ) -> bool:
        return self._ledger.claim(
            source_kind=source_kind, ref=ref, agent_name=agent_name, dry_run=dry_run
        )

    async def record_session(
        self,
        *,
        source_kind: str,
        ref: str,
        session_key: str,
        agent_name: str,
        brief: str,
        step_index: int | None = None,
    ) -> StepFilesSummary | None:
        del agent_name, brief, step_index
        self._ledger.record_session(
            source_kind=source_kind, ref=ref, session_key=session_key
        )
        # The local ledger has no server behind it and therefore no fetch.
        return None

    async def finish(
        self,
        *,
        source_kind: str,
        ref: str,
        status: ClaimStatus,
        outcome_code: str | None = None,
        detail: str | None = None,
        turn_trace_id: str | None = None,
        output_text: str | None = None,
        step_index: int | None = None,
    ) -> None:
        del output_text, step_index
        self._ledger.finish(
            source_kind=source_kind,
            ref=ref,
            status=status,
            outcome_code=outcome_code,
            detail=detail,
            turn_trace_id=turn_trace_id,
        )

    async def get(self, *, source_kind: str, ref: str) -> Claim | None:
        return self._ledger.get(source_kind=source_kind, ref=ref)

    async def aclose(self) -> None:
        self._ledger.close()


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
