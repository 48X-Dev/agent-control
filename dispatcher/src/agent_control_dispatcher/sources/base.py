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

    ``kind`` is ``"linear"`` or ``"file"``. Slice 1 ships only ``"file"``, and
    ``sources/file.py`` stays as the test source forever.
    """

    kind: str

    async def poll(self, *, cursor: str | None) -> list[SourceItem]:
        """Items eligible for claiming, oldest first."""
        ...

    async def write_back(
        self, *, item_ref: str, body: str, idempotency_marker: str
    ) -> WriteBackOutcome:
        """Record the outcome on the source. Must tolerate being called twice."""
        ...
