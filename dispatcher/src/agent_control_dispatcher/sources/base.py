"""The task source protocol, section 5.1.

A source yields items and records outcomes. It does not carry an agent, a
workflow, a tool list, a priority or labels: nothing a source can express
reaches a decision the dispatcher makes. Section 8 deletes label-driven agent
selection explicitly, and the shape of :class:`SourceItem` is what keeps that
deletion enforced rather than remembered.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SourceItem:
    """One unit of work as the source describes it.

    ``ref`` is a stable id within the source. Everything else is description,
    and every character of ``title`` and ``body`` is untrusted: it was written
    by a person with access to the tracker, which is not the same person as the
    operator, and it reaches the model inside the delimited block that
    :mod:`agent_control_dispatcher.envelope` builds.
    """

    ref: str
    title: str
    body: str
    url: str | None = None
    updated_at: dt.datetime | None = None


class WriteBackStatus(StrEnum):
    """What a write-back attempt did."""

    WRITTEN = "written"
    ALREADY_PRESENT = "already_present"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WriteBackOutcome:
    """The result of recording an outcome on the source."""

    status: WriteBackStatus
    detail: str | None = None


@runtime_checkable
class TaskSource(Protocol):
    """Where work comes from.

    ``kind`` is ``"linear"`` or ``"file"``. Slice 1 shipped only ``"file"``, and
    ``sources/file.py`` stays as the test source forever.
    """

    kind: str

    def describe(self) -> str:
        """One line naming exactly what this source is pointed at."""
        ...

    async def poll(self, *, cursor: str | None) -> list[SourceItem]:
        """Items eligible for claiming, oldest first."""
        ...

    async def write_back(
        self, *, item_ref: str, body: str, idempotency_marker: str
    ) -> WriteBackOutcome:
        """Record the outcome on the source. Must tolerate being called twice."""
        ...


@dataclass(frozen=True, slots=True)
class ScopeReport:
    """What a scoped read saw, and what it left alone.

    Every number here exists so that a person watching the terminal can weigh
    the set before anything spends money on it. The skip counts in particular
    are the reason the eligibility predicates are applied after the read rather
    than inside the query: you cannot count rows a filter removed, and *"2
    issues are assigned to a person and were skipped"* is the sentence that
    tells an operator the override worked.
    """

    fetched: int
    eligible: int
    skipped_started: int
    skipped_assigned: int
    skipped_other_team: int
    beyond_page_cap: bool
    cached: bool
    fetched_at: dt.datetime | None

    def lines(self) -> list[str]:
        """The report as an operator reads it, one label per line."""

        age = self.fetched_at.isoformat() if self.fetched_at is not None else "unknown"
        rendered = [
            f"fetched    {self.fetched} issue(s) in scope, read at {age}"
            f"{' (cached)' if self.cached else ''}",
            f"eligible   {self.eligible}",
            f"skipped    {self.skipped_started} already started by a person, "
            f"{self.skipped_assigned} assigned to a person, "
            f"{self.skipped_other_team} belonging to another team",
        ]
        if self.beyond_page_cap:
            rendered.append(
                "warning    the read came back at its page cap, so this milestone "
                "may hold issues neither of us has seen"
            )
        return rendered


@runtime_checkable
class ScopedTaskSource(Protocol):
    """A source that read a bounded set and can say what it skipped.

    Deliberately separate from :class:`TaskSource`, which is section 5.1's
    protocol and is not widened by this. The file source has no scope to report
    and does not implement this; the dispatcher asks with ``isinstance``.
    """

    scope_report: ScopeReport | None
