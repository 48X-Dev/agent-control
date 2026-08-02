"""Team entity models, slug derivation, and the team HTTP wire models."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, TypeAdapter, field_validator

from .agent import normalize_agent_name
from .base import BaseModel
from .linear import LinearTeamKey
from .server import PaginationInfo

TEAM_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
TEAM_SLUG_MAX_LENGTH = 255
TEAM_DISPLAY_NAME_MAX_LENGTH = 255
TEAM_DESCRIPTION_MAX_LENGTH = 1000

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

TeamSlug = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=TEAM_SLUG_MAX_LENGTH,
        pattern=TEAM_SLUG_PATTERN,
    ),
]


def slugify(value: str) -> str:
    """Derive a team slug from free-form text.

    Lowercases, folds accented characters onto their ASCII base, collapses every
    run of non-alphanumeric characters into a single hyphen, and strips leading
    and trailing hyphens. ``"Sales & Outreach"`` becomes ``"sales-outreach"``.

    Input without any alphanumeric content yields an empty string rather than an
    exception, so callers can validate the result against :data:`TeamSlug` and
    raise an error that fits their own boundary.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    # Accents are dropped so "Café" folds to "cafe", but every other character
    # without an ASCII form becomes a separator rather than disappearing:
    # dropping them would glue words together, turning "Sales—Outreach" into
    # "salesoutreach" while "Sales - Outreach" gives "sales-outreach".
    unaccented = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    folded = unaccented.encode("ascii", "replace").decode("ascii")
    return _NON_ALPHANUMERIC.sub("-", folded.lower()).strip("-")


class Team(BaseModel):
    """A named group of agents within a single namespace.

    Teams are descriptive. Membership records which agents belong together and
    has no effect on how controls or policies resolve at runtime.

    ``slug`` is the stable key and is immutable once the team exists; a rename
    changes ``display_name`` only.
    """

    id: int = Field(..., description="Surrogate identifier for the team.")
    namespace_key: str = Field(..., description="Namespace the team belongs to.")
    slug: TeamSlug = Field(
        ..., description="Stable, immutable key derived from the display name."
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=TEAM_DISPLAY_NAME_MAX_LENGTH,
        description="Human-readable team name, stored verbatim.",
    )
    description: str | None = Field(
        None,
        max_length=TEAM_DESCRIPTION_MAX_LENGTH,
        description="Optional free-text description of the team.",
    )
    linear_team_key: LinearTeamKey | None = Field(
        None,
        description=(
            "Key of the Linear team this team maps to, used to read "
            "milestones. Null when the team is not linked to Linear."
        ),
    )
    created_at: dt.datetime = Field(..., description="When the team was created.")
    updated_at: dt.datetime = Field(..., description="When the team was last modified.")


class TeamMember(BaseModel):
    """Membership of one agent in one team.

    Membership is many-to-many: the same agent may appear in several teams.
    """

    namespace_key: str = Field(
        ..., description="Namespace shared by the team and the agent."
    )
    team_id: int = Field(..., description="Team the agent belongs to.")
    agent_name: str = Field(..., description="Normalized agent identifier.")
    joined_at: dt.datetime = Field(
        ..., description="When the agent was added to the team."
    )

    @field_validator("agent_name", mode="before")
    @classmethod
    def validate_and_normalize_agent_name(cls, value: str) -> str:
        return normalize_agent_name(str(value))


# =============================================================================
# Team requests / responses
# =============================================================================

TEAM_SLUG_ADAPTER: TypeAdapter[str] = TypeAdapter(TeamSlug)

TeamDisplayName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=TEAM_DISPLAY_NAME_MAX_LENGTH),
]
TeamDescription = Annotated[
    str,
    StringConstraints(max_length=TEAM_DESCRIPTION_MAX_LENGTH),
]


class UpsertTeamRequest(BaseModel):
    """Request to create or replace a team, keyed by slug.

    Replace semantics: an omitted ``description`` clears the stored value on an
    existing team. Use PATCH to change one field and leave the rest alone.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: TeamDisplayName = Field(
        ..., description="Human-readable team name, stored verbatim."
    )
    slug: TeamSlug | None = Field(
        None,
        description=(
            "Stable key for the team. Derived from display_name when omitted "
            "('Sales & Outreach' becomes 'sales-outreach'). Ignored when the "
            "team already exists: slugs are immutable."
        ),
    )
    description: TeamDescription | None = Field(
        None, description="Optional free-text description of the team."
    )
    linear_team_key: LinearTeamKey | None = Field(
        None,
        description=(
            "Key of the Linear team to read milestones from, e.g. 'ENG'. "
            "Lower case input is folded to upper case. Omitting it unlinks an "
            "existing team from Linear, in line with replace semantics."
        ),
    )


class UpsertTeamResponse(BaseModel):
    """Response from a slug-keyed team upsert."""

    team_id: int = Field(..., description="Identifier of the team.")
    slug: str = Field(..., description="Slug the team is keyed by.")
    created: bool = Field(
        ...,
        description=(
            "True when a new team was created; False when an existing team "
            "was updated in place."
        ),
    )


class PatchTeamRequest(BaseModel):
    """Request to update a team's mutable fields.

    ``slug`` is immutable, so supplying it is rejected rather than silently
    ignored. Omitted fields are left unchanged; an explicit ``null``
    description clears it.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: TeamDisplayName | None = Field(
        None, description="New human-readable team name."
    )
    description: TeamDescription | None = Field(
        None, description="New description, or null to clear it."
    )
    linear_team_key: LinearTeamKey | None = Field(
        None,
        description=(
            "Key of the Linear team to read milestones from, or null to "
            "unlink the team from Linear."
        ),
    )


class PatchTeamResponse(BaseModel):
    """Response from updating a team."""

    success: bool = Field(..., description="Whether the update succeeded.")
    slug: str = Field(..., description="Unchanged slug of the team.")
    display_name: str = Field(..., description="Current display name.")
    description: str | None = Field(None, description="Current description.")
    linear_team_key: str | None = Field(
        None, description="Current Linear team key, or null when unlinked."
    )


class DeleteTeamResponse(BaseModel):
    """Response from deleting a team."""

    success: bool = Field(..., description="Whether the deletion succeeded.")
    removed_member_count: int = Field(
        ..., description="Number of memberships removed along with the team."
    )


class TeamSummary(BaseModel):
    """List view of a single team."""

    id: int
    namespace_key: str
    slug: str
    display_name: str
    description: str | None = None
    linear_team_key: str | None = Field(
        None, description="Linear team this team maps to, or null when unlinked."
    )
    member_count: int = Field(..., description="Number of agents in the team.")
    created_at: dt.datetime
    updated_at: dt.datetime


class TeamMemberRef(BaseModel):
    """One agent's membership in a team."""

    agent_name: str = Field(..., description="Normalized agent identifier.")
    joined_at: dt.datetime = Field(
        ..., description="When the agent was added to the team."
    )


class GetTeamResponse(TeamSummary):
    """Detail view of a single team, including its members."""

    members: list[TeamMemberRef] = Field(
        default_factory=list,
        description="Members ordered by agent name.",
    )


class ListTeamsResponse(BaseModel):
    """Paginated list of teams."""

    teams: list[TeamSummary] = Field(default_factory=list)
    pagination: PaginationInfo = Field(
        ..., description="Cursor-based pagination metadata."
    )


class AddTeamMemberResponse(BaseModel):
    """Response from adding an agent to a team (idempotent)."""

    added: bool = Field(
        ...,
        description=(
            "True when the membership was created; False when the agent was "
            "already a member."
        ),
    )
    team_id: int = Field(..., description="Identifier of the team.")
    agent_name: str = Field(..., description="Normalized agent identifier.")
    joined_at: dt.datetime = Field(
        ..., description="When the agent joined; unchanged on a repeat add."
    )


class RemoveTeamMemberResponse(BaseModel):
    """Response from removing an agent from a team (idempotent)."""

    removed: bool = Field(
        ...,
        description=(
            "True when a membership was deleted; False when the agent was "
            "not a member."
        ),
    )
