"""The claim ledger, and the local SQLite one that is no longer the default.

``agent_tasks`` has landed, so the shipped ledger is the server's: an atomic
claim, a lease, and reclaim of a holder that died. :mod:`.server_ledger` is
that one, and it is what ``dispatch once`` uses unless a path is passed.

**This file is what remains, and it is deliberately still here.** A run pointed
at a local file needs no server rows, which keeps the offline path in section
14's spirit available for a developer poking at a YAML file. What it is not is
a claim: two dispatchers with two ledger files both claim every item and both
spend money on it. The docstrings below say so, and nothing about that changed
when the server ledger arrived.

The seam that made the swap cheap is :class:`TaskLedger`. Both implementations
expose the same five verbs, ``dispatch.py`` knows only the protocol, and
section 14's promise held: the invocation did not change when the ledger moved
onto the server.

One process-local guard is real and worth having: ``UNIQUE(source_kind, ref)``
means a single run cannot dispatch the same ref twice, and a re-run skips refs
that already reached a terminal state. That is resumability, not safety.
"""

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


class TaskLedger(Protocol):
    """What a ledger has to be able to do, whichever side of the wire it is on.

    Async because the shipped implementation is HTTP. The local one wraps
    synchronous SQLite in :class:`LocalTaskLedger` rather than making the
    protocol synchronous, because a protocol shaped around the weaker
    implementation is a protocol the stronger one cannot satisfy.

    ``register`` is the verb the SQLite ledger did not need. On the server it
    is the import - preview, then commit against the digest of what was
    previewed - and it is the point at which one row per item comes into
    existence. Locally it is a no-op, because the local ledger creates a row
    when it claims one.
    """

    async def register(
        self,
        *,
        source_kind: str,
        items: Sequence[SourceItem],
        dry_run: bool,
        workflow_key: str | None = None,
    ) -> None:
        """Make sure a row exists for each item, without claiming any of them.

        ``workflow_key`` is fixed on the row here and never afterwards, for the
        same reason ``dry_run`` is: a dispatcher that could change which agents
        a task runs *after* an operator agreed to the set would have turned the
        confirm into a different question than the one that was answered.
        """

    async def claim(
        self, *, source_kind: str, ref: str, agent_name: str, dry_run: bool
    ) -> bool:
        """Take one item, or report that this ledger cannot."""

    def resume_step_index(self, *, source_kind: str, ref: str) -> int:
        """Which step of the chain to start at, from what the claim returned.

        ``MAX(step_index) WHERE status='completed'`` plus one, decided by the
        server and read here rather than recomputed: a dispatcher that arrived
        at a different number would re-run a step that already spent money and
        may already have acted through a tool. Synchronous, for the same reason
        :meth:`session_task_key` is - it sits between the claim and the first
        turn, and a round trip there is a second thing that can fail in the
        window the claim exists to make small.
        """

    def session_task_key(self, *, source_kind: str, ref: str) -> str | None:
        """The task row a session for this item must be bound to, if there is one.

        Synchronous and answered from what the claim already returned, because
        it is read on the path to opening a session and a round trip there
        would be a second thing that can fail between the claim and the turn.
        ``None`` from the local ledger, which has no server row to bind to.
        """

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
        """Record the session this item's step is running on, before its turn.

        Returns what the server found attached to the item, when it looked.
        ``None`` means it did not look - no tracker behind this task, or the
        source switched off - and an envelope says nothing at all in that case
        rather than claiming an issue carries no files.

        ``step_index`` defaults to the position the claim reported, which is
        where a one-step task and a reclaimed task both resume. A chain passes
        the position it is about to run instead, and each position
        gets its own session: reuse is not available, because
        ``release_turn_lock`` fences on the in-flight trace and
        ``uq_agent_session_halts_turn`` is a full unique constraint on the
        session's turn, so two turns of one session sharing a trace would let a
        late release clear a live lock and let step 1's halt row block step 3's.
        """

    async def complete_step(
        self,
        *,
        source_kind: str,
        ref: str,
        step_index: int,
        output_text: str,
        turn_trace_id: str | None = None,
    ) -> None:
        """Close one hop of a chain that carries on afterwards.

        Only for a step that succeeded and is followed by another. The last
        step of any chain, and every step that failed, goes through
        :meth:`finish`, which closes the step and the task together in the one
        server transaction that gets the write order right.
        """

    async def prior_report(
        self, *, source_kind: str, ref: str, step_index: int
    ) -> PriorReport | None:
        """What the step before ``step_index`` reported, for the envelope.

        Read from the ledger rather than remembered in this process, because a
        reclaimed task resumes mid-chain in a *different* dispatcher: the one
        that ran step 0 is gone, and its memory with it. ``None`` means no
        completed step precedes this one, which the caller must treat as a
        refusal rather than as an empty report - handing the next agent "the
        previous agent reported: (nothing)" is how it invents the missing work
        and reports it confidently.
        """

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
        """Close the claim out, however it ended.

        ``step_index`` names the hop this outcome belongs to; ``None`` means the
        one this ledger last opened, which is the only hop a single-step task
        has.
        """

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


class LocalTaskLedger:
    """:class:`ClaimLedger` behind the :class:`TaskLedger` protocol.

    An adapter and nothing else: every method here is one synchronous call.
    It exists so a local run and a server run go through one code path in
    ``dispatch.py``, which is what stops the offline path drifting into a
    second, differently-behaved dispatcher.

    ``register`` does nothing, and that is the honest difference between the
    two ledgers: there is nothing to register with, because there is nobody
    else to tell.

    **It records one step, not a chain**, and that is the second honest
    difference. The ``claims`` table has one row per item with one
    ``session_key`` and one ``turn_trace_id``, so a second hop would overwrite
    the first and the prior report would be gone. Rather than half-supporting a
    chain, :meth:`prior_report` answers ``None`` for every index and
    :meth:`resume_step_index` answers zero, and ``dispatch.py`` refuses a
    multi-step plan on this ledger with a reason rather than running the first
    step and silently losing the rest.
    """

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
        """Unreachable: a multi-step plan is refused before it gets here.

        Kept so the adapter satisfies the protocol whole. Raising rather than
        passing, because a silent no-op would mean a chain that appeared to run
        and recorded none of it.
        """
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
