"""HTTP endpoints for managing teams and their membership.

Teams are descriptive in this pass: these routes create teams, group agents
into them, and read the groupings back. Binding a control to a team does not
apply it to the team's members.

Every route resolves its namespace from the authenticated ``Principal`` and
passes it through to the service, which filters every statement on it.
"""

from __future__ import annotations

from agent_control_models.errors import ErrorCode, ValidationErrorItem
from agent_control_models.linear import ListTeamMilestonesResponse, Milestone
from agent_control_models.server import PaginationInfo
from agent_control_models.teams import (
    TEAM_SLUG_ADAPTER,
    AddTeamMemberResponse,
    DeleteTeamResponse,
    GetTeamResponse,
    ListTeamsResponse,
    PatchTeamRequest,
    PatchTeamResponse,
    RemoveTeamMemberResponse,
    TeamMemberRef,
    TeamSummary,
    UpsertTeamRequest,
    UpsertTeamResponse,
    slugify,
)
from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..db import get_async_db
from ..errors import APIValidationError, BadRequestError
from ..models import Team as TeamRow
from ..services.agent_names import normalize_agent_name_or_422
from ..services.linear_milestones import (
    LinearMilestoneService,
    get_milestone_service,
)
from ..services.teams import TeamsService

router = APIRouter(prefix="/teams", tags=["teams"])

_DEFAULT_LIST_LIMIT = 20
_MAX_LIST_LIMIT = 100


def _to_summary(team: TeamRow, member_count: int) -> TeamSummary:
    return TeamSummary(
        id=team.id,
        namespace_key=team.namespace_key,
        slug=team.slug,
        display_name=team.display_name,
        description=team.description,
        linear_team_key=team.linear_team_key,
        member_count=member_count,
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


def _resolve_slug(request: UpsertTeamRequest) -> str:
    """Return the slug to key the upsert on.

    An explicit slug wins; otherwise it is derived from the display name.
    Derivation can legitimately produce something unusable - a display name of
    ``"***"`` has no alphanumeric content - so the result is validated here and
    reported as a request error rather than surfacing as a database failure.
    """
    candidate = request.slug if request.slug is not None else slugify(request.display_name)
    try:
        return TEAM_SLUG_ADAPTER.validate_python(candidate)
    except ValidationError as exc:
        raise APIValidationError(
            error_code=ErrorCode.VALIDATION_ERROR,
            detail="Could not derive a valid slug from display_name.",
            resource="Team",
            hint=(
                "Supply an explicit slug of lowercase alphanumeric words "
                "separated by single hyphens, or use a display name that "
                "contains letters or digits."
            ),
            errors=[
                ValidationErrorItem(
                    resource="Team",
                    field="slug",
                    code="invalid_format",
                    message="Derived slug is empty or malformed.",
                    value=candidate,
                )
            ],
        ) from exc


@router.put(
    "",
    response_model=UpsertTeamResponse,
    summary="Create or replace a team (idempotent)",
    response_description="Created or updated team",
)
async def upsert_team(
    request: UpsertTeamRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_WRITE)),
) -> UpsertTeamResponse:
    """Idempotent create-or-replace keyed by slug.

    The slug is derived from ``display_name`` when the request omits it:
    ``"Sales & Outreach"`` becomes ``"sales-outreach"``. Slugs are immutable,
    so a request naming an existing team updates the mutable fields only.
    Replace semantics apply: an omitted description or ``linear_team_key``
    clears the stored one.
    """
    slug = _resolve_slug(request)
    service = TeamsService(db)
    team, created = await service.upsert_team(
        namespace_key=principal.namespace_key,
        slug=slug,
        display_name=request.display_name,
        description=request.description,
        linear_team_key=request.linear_team_key,
    )
    await db.commit()
    await db.refresh(team)
    return UpsertTeamResponse(team_id=team.id, slug=team.slug, created=created)


@router.get(
    "",
    response_model=ListTeamsResponse,
    summary="List teams",
    response_description="Teams in the request namespace",
)
async def list_teams(
    cursor: str | None = Query(
        None,
        description=(
            "Opaque cursor returned as ``next_cursor`` on the previous page. "
            "Pass it back unchanged to fetch the next page."
        ),
    ),
    limit: int = Query(
        _DEFAULT_LIST_LIMIT,
        ge=1,
        le=_MAX_LIST_LIMIT,
        description="Maximum teams to return (default 20, max 100).",
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_READ)),
) -> ListTeamsResponse:
    """Return teams in the request namespace with cursor-based pagination.

    Teams are ordered by ID descending (newest first). The cursor is opaque to
    clients: pass back the ``next_cursor`` value verbatim to fetch the
    following page. Each row carries its member count; the members themselves
    come from the single-team endpoint.
    """
    parsed_cursor: int | None
    if cursor is None:
        parsed_cursor = None
    else:
        try:
            parsed_cursor = int(cursor)
        except ValueError as exc:
            raise BadRequestError(
                error_code=ErrorCode.VALIDATION_ERROR,
                detail="cursor must be a value returned by next_cursor.",
                hint="Pass the cursor returned in the previous response unchanged.",
            ) from exc
    service = TeamsService(db)
    page = await service.list_teams(
        namespace_key=principal.namespace_key,
        cursor=parsed_cursor,
        limit=limit,
    )
    return ListTeamsResponse(
        teams=[
            _to_summary(team, page.member_counts.get(team.id, 0)) for team in page.teams
        ],
        pagination=PaginationInfo(
            limit=limit,
            total=page.total,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        ),
    )


@router.get(
    "/{slug}",
    response_model=GetTeamResponse,
    summary="Get a team and its members",
    response_description="The requested team",
)
async def get_team(
    slug: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_READ)),
) -> GetTeamResponse:
    """Read one team by slug, including every agent in it."""
    service = TeamsService(db)
    team = await service.get_team_or_404(
        namespace_key=principal.namespace_key, slug=slug
    )
    members = await service.list_members(
        namespace_key=principal.namespace_key, team_id=team.id
    )
    summary = _to_summary(team, len(members))
    return GetTeamResponse(
        **summary.model_dump(),
        members=[
            TeamMemberRef(agent_name=m.agent_name, joined_at=m.joined_at)
            for m in members
        ],
    )


@router.get(
    "/{slug}/milestones",
    response_model=ListTeamMilestonesResponse,
    summary="Get a team's Linear milestones",
    response_description="Milestones for the mapped Linear team",
)
async def list_team_milestones(
    slug: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_READ)),
    milestones: LinearMilestoneService = Depends(get_milestone_service),
) -> ListTeamMilestonesResponse:
    """Read the milestones of the Linear team this team maps to.

    The server holds the Linear API key and makes the call itself; the key is
    never part of a response, and no field of this one can carry it.

    Only an unknown ``slug`` produces an error status. Everything else is a 200
    carrying a ``status`` for the client to branch on:

    * ``not_configured`` - the server has no Linear API key.
    * ``not_linked`` - the team has no ``linear_team_key``. Set one with PATCH.
    * ``error`` - Linear was unreachable, rate-limited us, or refused. An
      unavailable third party is not an Agent Control failure, so this is a
      200 with an empty list and a short reason.
    * ``empty`` - Linear answered and the team's projects hold no milestones.
    * ``ok`` - at least one milestone, ordered by target date with undated
      milestones last.

    Reads are cached briefly and shared between concurrent callers. A response
    with ``cached`` set may predate the request by up to the cache lifetime,
    which ``fetched_at`` reports exactly; the last good read is also what gets
    served while Linear is failing, in preference to an error panel.
    """
    teams_service = TeamsService(db)
    team = await teams_service.get_team_or_404(
        namespace_key=principal.namespace_key, slug=slug
    )
    result = await milestones.get_milestones(
        namespace_key=principal.namespace_key,
        linear_team_key=team.linear_team_key,
    )
    return ListTeamMilestonesResponse(
        status=result.status,
        slug=team.slug,
        linear_team_key=team.linear_team_key,
        milestones=[
            Milestone(
                id=m.id,
                name=m.name,
                description=m.description,
                target_date=m.target_date,
                status=m.status,
                progress=m.progress,
                project_id=m.project_id,
                project_name=m.project_name,
                project_url=m.project_url,
            )
            for m in result.milestones
        ],
        error=result.error,
        retry_after_seconds=result.retry_after_seconds,
        cached=result.cached,
        fetched_at=result.fetched_at,
    )


@router.patch(
    "/{slug}",
    response_model=PatchTeamResponse,
    summary="Update a team's display name or description",
    response_description="Updated team fields",
)
async def patch_team(
    slug: str,
    request: PatchTeamRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_WRITE)),
) -> PatchTeamResponse:
    """Update the mutable fields of a team.

    Omitted fields are left as they are, so an explicit ``"description": null``
    is the only way to clear a description, and the same holds for
    ``linear_team_key``. ``display_name`` is required on a team and cannot be
    cleared, so an explicit null there is a no-op. The slug cannot be changed;
    a body carrying one is rejected as an unknown field.
    """
    service = TeamsService(db)
    team = await service.update_team(
        namespace_key=principal.namespace_key,
        slug=slug,
        display_name=request.display_name,
        description=request.description,
        update_description="description" in request.model_fields_set,
        linear_team_key=request.linear_team_key,
        update_linear_team_key="linear_team_key" in request.model_fields_set,
    )
    await db.commit()
    return PatchTeamResponse(
        success=True,
        slug=team.slug,
        display_name=team.display_name,
        description=team.description,
        linear_team_key=team.linear_team_key,
    )


@router.delete(
    "/{slug}",
    response_model=DeleteTeamResponse,
    summary="Delete a team",
    response_description="Deletion confirmation",
)
async def delete_team(
    slug: str,
    force: bool = Query(
        False,
        description=(
            "Delete the team even when it still has members, removing their "
            "memberships along with it."
        ),
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_WRITE)),
) -> DeleteTeamResponse:
    """Delete a team by slug.

    A team that still has members is a 409 unless ``force=true``, so losing
    membership takes a deliberate second call. Deleting a team never touches
    the agents themselves.
    """
    service = TeamsService(db)
    removed = await service.delete_team(
        namespace_key=principal.namespace_key, slug=slug, force=force
    )
    await db.commit()
    return DeleteTeamResponse(success=True, removed_member_count=removed)


@router.post(
    "/{slug}/members/{agent_name}",
    response_model=AddTeamMemberResponse,
    summary="Add an agent to a team (idempotent)",
    response_description="The resulting membership",
)
async def add_team_member(
    slug: str,
    agent_name: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_WRITE)),
) -> AddTeamMemberResponse:
    """Add an agent to a team.

    Both the team and the agent must already exist in the namespace; either
    one missing is a 404. Repeating the call returns the existing membership
    with ``added=false`` and the original ``joined_at``.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    service = TeamsService(db)
    member, added = await service.add_member(
        namespace_key=principal.namespace_key, slug=slug, agent_name=normalized
    )
    await db.commit()
    await db.refresh(member)
    return AddTeamMemberResponse(
        added=added,
        team_id=member.team_id,
        agent_name=member.agent_name,
        joined_at=member.joined_at,
    )


@router.delete(
    "/{slug}/members/{agent_name}",
    response_model=RemoveTeamMemberResponse,
    summary="Remove an agent from a team (idempotent)",
    response_description="Whether a membership was removed",
)
async def remove_team_member(
    slug: str,
    agent_name: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.TEAMS_WRITE)),
) -> RemoveTeamMemberResponse:
    """Remove an agent from a team.

    The team must exist. An agent that is not a member yields
    ``removed=false`` rather than a 404, so retries are safe.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    service = TeamsService(db)
    removed = await service.remove_member(
        namespace_key=principal.namespace_key, slug=slug, agent_name=normalized
    )
    await db.commit()
    return RemoveTeamMemberResponse(removed=removed)
