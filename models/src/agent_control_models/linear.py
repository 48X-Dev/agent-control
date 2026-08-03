"""Wire models for the Linear milestone integration.

Agent Control talks to Linear on the server; the browser only ever sees what
is in this module. Nothing here carries a credential, and none of these models
has a field that could hold one.

A team is linked to Linear by its team key (``ENG``, ``SALES``), stored on the
Agent Control team. Milestones themselves are read-through: they are never
persisted, so nothing in Linear has to be kept in sync with this database.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from .base import BaseModel

LINEAR_TEAM_KEY_PATTERN = r"^\s*[A-Za-z0-9]+\s*$"
LINEAR_TEAM_KEY_MAX_LENGTH = 20

LinearTeamKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_upper=True,
        min_length=1,
        max_length=LINEAR_TEAM_KEY_MAX_LENGTH,
        pattern=LINEAR_TEAM_KEY_PATTERN,
    ),
]
"""Identifier of a team in Linear, as shown on its issue prefixes.

Linear renders keys in upper case, so input is folded to upper case rather
than rejected: ``"eng"`` and ``"ENG"`` name the same Linear team and both
store as ``"ENG"``.

The pattern accepts either case and surrounding whitespace because pydantic
matches it against the raw input, before the strip and upper-case transforms
run. Tightening it to ``^[A-Z0-9]+$`` would reject the very input those
transforms exist to accept.
"""


class MilestonesStatus(StrEnum):
    """Why a milestone response looks the way it does.

    Four of these five are not failures, and the UI is expected to render each
    one differently. Only ``ERROR`` means something went wrong, and even that
    is delivered with a 200 so an unreachable third party cannot make an Agent
    Control page look broken.
    """

    NOT_CONFIGURED = "not_configured"
    """The server has no Linear API key. Nothing was requested from Linear."""

    NOT_LINKED = "not_linked"
    """The team has no ``linear_team_key``, so there is nothing to look up."""

    ERROR = "error"
    """Linear was unreachable, rate-limited the request, or rejected it."""

    EMPTY = "empty"
    """Linear answered, and this team's projects have no milestones."""

    OK = "ok"
    """Linear answered with at least one milestone."""


class Milestone(BaseModel):
    """One Linear project milestone, flattened onto its parent project.

    Linear hangs milestones off projects rather than teams, so a team's
    milestones are the union across its projects. The project fields are kept
    on each row so a flat list stays readable without a second lookup.
    """

    id: str = Field(..., description="Linear identifier for the milestone.")
    name: str = Field(..., description="Milestone name as entered in Linear.")
    description: str | None = Field(
        None, description="Free-text description, when the milestone has one."
    )
    target_date: dt.date | None = Field(
        None, description="Date the milestone is due, when one is set."
    )
    status: str | None = Field(
        None,
        description=(
            "Linear's own status for the milestone ('unstarted', 'next', "
            "'overdue', 'done'). Passed through as text rather than an "
            "enumeration so a value Linear adds later still renders."
        ),
    )
    progress: float | None = Field(
        None,
        ge=0,
        le=1,
        description="Completion of the milestone from 0 to 1, when reported.",
    )
    project_id: str | None = Field(
        None, description="Linear identifier of the project the milestone sits in."
    )
    project_name: str | None = Field(
        None, description="Name of the project the milestone sits in."
    )
    project_url: str | None = Field(
        None, description="Link to the project in Linear."
    )


class ListTeamMilestonesResponse(BaseModel):
    """Milestones for one Agent Control team, read through from Linear.

    ``status`` is the field to branch on. ``milestones`` is empty for every
    status other than ``ok``, so a client that ignores the status still renders
    an empty list rather than stale or wrong data.
    """

    status: MilestonesStatus = Field(
        ..., description="Which of the five outcomes this response represents."
    )
    slug: str = Field(..., description="Slug of the Agent Control team.")
    linear_team_key: str | None = Field(
        None,
        description="Linear team the milestones came from, or null when unlinked.",
    )
    milestones: list[Milestone] = Field(
        default_factory=list,
        description="Milestones ordered by target date, undated ones last.",
    )
    error: str | None = Field(
        None,
        description=(
            "Short, client-safe reason the read failed, set only when status "
            "is 'error'. Never contains credentials or a raw upstream body."
        ),
    )
    retry_after_seconds: int | None = Field(
        None,
        ge=0,
        description=(
            "Seconds to wait before retrying, when Linear asked for a delay. "
            "Only ever set alongside status 'error'."
        ),
    )
    cached: bool = Field(
        False,
        description="True when this response was served from the server's cache.",
    )
    fetched_at: dt.datetime | None = Field(
        None,
        description=(
            "When the underlying data was read from Linear. On a cached "
            "response this is the time of the original read, not of this one."
        ),
    )


class MilestoneIssue(BaseModel):
    """One issue inside a milestone that an agent could be pointed at.

    Only *eligible* issues are rendered as rows. Everything the scope read
    skipped is reported as a count on :class:`MilestoneIssueCounts` and nothing
    else, so a shared project's other-team work never has its text copied into
    an Agent Control response.

    ``creator_id``, ``creator_display_name`` and ``created_at`` exist so a human
    can weigh provenance before starting anything. **They stop at the confirm.**
    Plan section 5.1 keeps them off ``SourceItem``, out of the envelope and away
    from a model: a creator name in the prompt is one more attacker-controlled
    string, and an agent that can read who filed an issue is an agent an
    injection can address by name.
    """

    ref: str = Field(
        ...,
        description=(
            "Linear's own identifier for the issue, stable across renames and "
            "across a move between teams. This is what a claim is keyed on."
        ),
    )
    identifier: str = Field(
        ...,
        description="Human-facing key such as 'OPS-12'. Changes if the issue moves team.",
    )
    title: str = Field(..., description="Issue title as written in Linear.")
    description: str | None = Field(
        None,
        description=(
            "Issue body as written in Linear. Untrusted text: whoever can file "
            "an issue wrote it."
        ),
    )
    url: str | None = Field(None, description="Link to the issue in Linear.")
    created_at: dt.datetime | None = Field(
        None, description="When the issue was filed. Provenance for the confirm."
    )
    updated_at: dt.datetime | None = Field(
        None, description="When the issue last changed. The read is ordered by this."
    )
    creator_id: str | None = Field(
        None, description="Linear id of whoever filed the issue. Provenance for the confirm."
    )
    creator_display_name: str | None = Field(
        None, description="Display name of whoever filed the issue. Never reaches a model."
    )


class MilestoneIssueSkipCounts(BaseModel):
    """Why issues in this milestone were not offered, by reason.

    The three are disjoint and add up with ``eligible`` to the milestone's whole
    issue set. An issue that is both started and assigned counts once, under
    ``started``, because that is the stronger statement about it.
    """

    started: int = Field(
        default=0,
        ge=0,
        description=(
            "State type is neither 'backlog' nor 'unstarted', so a human has "
            "already begun. Never offered, and no request field can change that."
        ),
    )
    assigned: int = Field(
        default=0,
        ge=0,
        description=(
            "The issue has an assignee, so it is that person's. Assigning an "
            "issue to yourself is the cheapest possible override."
        ),
    )
    other_team: int = Field(
        default=0,
        ge=0,
        description=(
            "In this milestone but owned by another Linear team. A project "
            "shared across teams is ordinary, and widening one press to cover "
            "it would make the blast radius a property of somebody else's "
            "project layout. Cross-team work needs that team's own run."
        ),
    )


class MilestoneIssueCounts(BaseModel):
    """What the scope read saw, so an operator can weigh it before starting."""

    fetched: int = Field(
        default=0, ge=0, description="Rows the team-scoped read returned, before bucketing."
    )
    eligible: int = Field(
        default=0, ge=0, description="Issues offered as rows. Equals the length of ``issues``."
    )
    skipped: MilestoneIssueSkipCounts = Field(
        default_factory=MilestoneIssueSkipCounts,
        description="Everything not offered, by reason.",
    )
    beyond_page_cap: bool = Field(
        default=False,
        description=(
            "The read came back at its hard page cap, so this milestone may "
            "hold issues nobody here has seen. Reported rather than truncated "
            "silently."
        ),
    )


class ListMilestoneIssuesResponse(BaseModel):
    """The issues of one milestone that are eligible to be worked, plus counts.

    ``status`` carries the same meanings as
    :class:`ListTeamMilestonesResponse`, minus one: ``not_linked`` never appears
    here because a team with no ``linear_team_key`` is refused with 409
    ``TEAM_NOT_LINKED`` instead. There is no scope to read against, and
    answering 200 with an empty list would read as "nothing to do".

    Nothing on this response is a write, an authorization, or a claim. It is a
    read of somebody else's tracker.
    """

    status: MilestonesStatus = Field(
        ..., description="not_configured, error, empty or ok. Never not_linked."
    )
    slug: str = Field(..., description="Slug of the Agent Control team.")
    linear_team_key: str = Field(
        ..., description="Linear team the read was scoped to. Always set on a 200."
    )
    milestone_id: str = Field(..., description="Milestone the read was scoped to.")
    issues: list[MilestoneIssue] = Field(
        default_factory=list,
        description=(
            "Eligible issues in the order Linear returned them under "
            "'orderBy: updatedAt', which is most recently changed first. The "
            "server does not reorder; a caller that wants oldest first sorts on "
            "``updated_at`` itself. Empty for any status but 'ok'."
        ),
    )
    counts: MilestoneIssueCounts = Field(
        default_factory=MilestoneIssueCounts,
        description="What the read saw and what it skipped.",
    )
    error: str | None = Field(
        None,
        description=(
            "Short, client-safe reason the read failed, set only when status is "
            "'error'. Never contains credentials or a raw upstream body."
        ),
    )
    retry_after_seconds: int | None = Field(
        None,
        ge=0,
        description="Seconds Linear asked the server to wait, when it asked for one.",
    )
    cached: bool = Field(
        False, description="True when this response was served from the server's cache."
    )
    fetched_at: dt.datetime | None = Field(
        None,
        description=(
            "When the underlying read happened. On a cached response this is "
            "the time of the original read, and how stale the set is."
        ),
    )
