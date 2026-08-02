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
