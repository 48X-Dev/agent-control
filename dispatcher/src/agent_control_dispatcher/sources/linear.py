"""The milestone source, section 14 slice 2. A read, and only a read.

    agent-control-dispatch once --source linear-milestone:<id> --team operations \\
        --agent ops_runbook_agent --max-tasks 3 --dry-run

Real issues, written by a person in a real tracker, become the task block of a
real envelope. Nothing here writes to Linear: not a comment, not a state
change, not a label. :meth:`LinearMilestoneSource.write_back` refuses rather
than returning a success-shaped outcome, for the same reason the file source
does - a ledger that believes a report exists on the source when none does is
worse than one that admits it cannot write.

**The dispatcher never talks to Linear.** It asks the Agent Control server,
which holds the credential and enforces the scope. Two consequences worth
being explicit about, because both are safety properties rather than layering
preferences: the dispatcher cannot widen the scope it was given, and an
operator who can run this still cannot read anything the server would not
show them.

**What crosses into :class:`SourceItem`, and what stops.** The server returns
``creator_id``, ``creator_display_name`` and ``created_at`` so a person can
weigh the set's provenance before starting. Section 5.1 keeps all three off
``SourceItem``, so they never reach the envelope and never reach a model, and
:func:`_to_item` is where that stops. An agent that can read who filed an issue
is an agent an injection can address by name.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from agent_control_models.linear import (
    ListMilestoneIssuesResponse,
    MilestoneIssue,
    MilestonesStatus,
)

from .base import ScopeReport, SourceItem, WriteBackOutcome

SOURCE_PREFIX = "linear-milestone:"

_STATUS_REFUSALS = {
    MilestonesStatus.NOT_CONFIGURED: (
        "The server has no Linear API key, so it never called Linear. There is "
        "nothing to dispatch and nothing was read."
    ),
    MilestonesStatus.ERROR: (
        "The server could not read this milestone from Linear. No set was "
        "produced, so nothing is dispatched: an empty run and a failed read "
        "must not look the same."
    ),
}

_DISPATCHABLE_STATUSES = frozenset({MilestonesStatus.OK, MilestonesStatus.EMPTY})
"""The only two statuses that mean a read happened. Anything else refuses.

Listed as an allowlist rather than as more entries in ``_STATUS_REFUSALS``
because the failure mode of a denylist here is the expensive one: a status this
version has never seen would fall through it, produce no items, and be reported
as "nothing to do" for a milestone nobody actually read."""


class LinearScopeError(RuntimeError):
    """The scope could not be read, so nothing may be dispatched from it.

    Raised rather than returning an empty list. An empty milestone and an
    unreadable one are different facts, and a dispatcher that treats an
    unreadable one as "nothing to do" reports success for work it never saw.
    """


class MilestoneIssueReader(Protocol):
    """What :class:`LinearMilestoneSource` needs from the server client.

    One method. Narrow on purpose, so the source can be exercised without a
    transport and so nothing here can reach a route that writes.
    """

    async def fetch_milestone_issues(
        self, *, team_slug: str, milestone_id: str
    ) -> ListMilestoneIssuesResponse:
        """Read one milestone's eligible issues and its skip counts."""
        ...


class LinearMilestoneSource:
    """One Linear milestone, scoped to one team, read through the server.

    Implements :class:`~agent_control_dispatcher.sources.base.TaskSource` and
    :class:`~agent_control_dispatcher.sources.base.ScopedTaskSource`.
    """

    kind = "linear"

    def __init__(
        self, *, reader: MilestoneIssueReader, milestone_id: str, team_slug: str
    ) -> None:
        self._reader = reader
        self._milestone_id = milestone_id
        self._team_slug = team_slug
        self.scope_report: ScopeReport | None = None

    @property
    def milestone_id(self) -> str:
        return self._milestone_id

    @property
    def team_slug(self) -> str:
        return self._team_slug

    def describe(self) -> str:
        return f"{SOURCE_PREFIX}{self._milestone_id} (team {self._team_slug})"

    async def poll(self, *, cursor: str | None) -> list[SourceItem]:
        """The milestone's eligible issues, oldest change first.

        Section 5.1 says oldest first and Linear's ``orderBy: updatedAt`` comes
        back newest first, so the order is reversed here rather than left as the
        server found it. It matters under ``--max-tasks``: the first three of a
        newest-first page are the three somebody just touched, and the first
        three of an oldest-first one are the three that have been sitting
        longest, which is the set an operator meant.

        Ordering is total - ``updated_at`` then ``ref`` - so two reads of an
        unchanged milestone hand out the same items in the same sequence.
        ``cursor`` is the ref of the last item handed out, and a cursor naming a
        ref no longer in the set restarts from the top rather than guessing
        where it would have been; the local ledger is what actually stops an
        item running twice.
        """

        response = await self._reader.fetch_milestone_issues(
            team_slug=self._team_slug, milestone_id=self._milestone_id
        )
        status = MilestonesStatus(response.status)
        if status not in _DISPATCHABLE_STATUSES:
            refusal = _STATUS_REFUSALS.get(
                status,
                f"The server answered this scope read with status '{status}', which "
                "this dispatcher does not know how to treat as a set of work. "
                "Nothing is dispatched.",
            )
            detail = f" {response.error}" if response.error else ""
            raise LinearScopeError(f"{refusal}{detail}")

        counts = response.counts
        self.scope_report = ScopeReport(
            fetched=counts.fetched,
            eligible=counts.eligible,
            skipped_started=counts.skipped.started,
            skipped_assigned=counts.skipped.assigned,
            skipped_other_team=counts.skipped.other_team,
            beyond_page_cap=counts.beyond_page_cap,
            cached=response.cached,
            fetched_at=response.fetched_at,
        )

        items = sorted((_to_item(issue) for issue in response.issues), key=_oldest_first)
        if cursor is None:
            return items
        refs = [item.ref for item in items]
        if cursor not in refs:
            return items
        return items[refs.index(cursor) + 1 :]

    async def write_back(
        self, *, item_ref: str, body: str, idempotency_marker: str
    ) -> WriteBackOutcome:
        """Not in this slice, and not faked.

        Slice 2 is a read. There is no comment, no state change and no server
        route that would accept one, so this refuses instead of returning
        something a caller could record as a delivered report. Nothing in the
        dispatcher calls it.
        """

        raise NotImplementedError(
            "The Linear source has no write-back. Slice 2 does not write to Linear at "
            "all; the operator reads the transcript."
        )


def _oldest_first(item: SourceItem) -> tuple[dt.datetime, str]:
    """A total order over the page, oldest change first.

    An issue with no ``updated_at`` sorts first rather than being dropped or
    left wherever it landed. ``ref`` breaks ties, so two issues touched in the
    same millisecond do not swap places between reads and change which three
    ``--max-tasks 3`` picks. Naive timestamps are read as UTC because Python
    refuses to compare them with aware ones and a mixed page must not crash.
    """

    moment = item.updated_at or dt.datetime.min
    return (
        moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC),
        item.ref,
    )


def _to_item(issue: MilestoneIssue) -> SourceItem:
    """Narrow a server issue row to the five fields a source may carry.

    The identifier is folded into the title rather than used as the ref.
    ``ref`` has to survive an issue moving between teams and being renamed, and
    Linear's own id does while ``OPS-12`` does not; putting ``OPS-12`` at the
    front of the title keeps the terminal readable and puts the issue key where
    the agent can quote it back.

    Creator and creation time are dropped here, deliberately, and this is the
    only place they could have leaked through.
    """

    return SourceItem(
        ref=issue.ref,
        title=f"{issue.identifier}: {issue.title}" if issue.identifier else issue.title,
        body=issue.description or "",
        url=issue.url,
        updated_at=issue.updated_at,
    )
