"""Persistence helpers for the ``teams`` and ``team_members`` tables.

Teams are descriptive groupings. Nothing here resolves controls or policies:
membership records which agents belong together and has no runtime effect.

Every method takes ``namespace_key`` and filters on it. A team is only ever
reachable through the namespace it was created in, and a membership row can
only join an agent to a team in that same namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from agent_control_models.errors import ErrorCode
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import ConflictError, NotFoundError
from ..models import Agent, Team, TeamMember
from .agent_sessions import AgentSessionsService


@dataclass(frozen=True)
class TeamListPage:
    """One page of teams plus the member count for each row."""

    teams: list[Team]
    member_counts: dict[int, int]
    total: int
    has_more: bool
    next_cursor: str | None


class TeamsService:
    """Persistence helpers for teams and their membership."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_team_or_404(self, *, namespace_key: str, slug: str) -> Team:
        """Load a team by its slug within ``namespace_key`` or raise 404."""
        team = await self._find_team(namespace_key=namespace_key, slug=slug)
        if team is None:
            raise NotFoundError(
                error_code=ErrorCode.TEAM_NOT_FOUND,
                detail=f"Team with slug '{slug}' not found",
                resource="Team",
                resource_id=slug,
                hint="Verify the slug and that the team belongs to this namespace.",
            )
        return team

    async def upsert_team(
        self,
        *,
        namespace_key: str,
        slug: str,
        display_name: str,
        description: str | None = None,
        linear_team_key: str | None = None,
    ) -> tuple[Team, bool]:
        """Idempotent create-or-replace keyed by ``(namespace_key, slug)``.

        Returns ``(team, created)``. An existing team keeps its slug and has
        ``display_name``, ``description``, and ``linear_team_key`` replaced
        with the supplied values.

        Concurrent callers for the same slug are handled the same way control
        bindings handle their natural key: the loser of the unique-constraint
        race rolls back its insert inside a savepoint, re-reads the winning
        row, and applies its values as an update.
        """
        existing = await self._find_team(namespace_key=namespace_key, slug=slug)
        if existing is not None:
            existing.display_name = display_name
            existing.description = description
            existing.linear_team_key = linear_team_key
            await self._db.flush()
            return existing, False

        team = Team(
            namespace_key=namespace_key,
            slug=slug,
            display_name=display_name,
            description=description,
            linear_team_key=linear_team_key,
        )
        # ``begin_nested`` opens a SAVEPOINT so a unique-constraint collision
        # rolls back only the conflicting insert, leaving any unrelated
        # pending writes in the surrounding transaction intact.
        try:
            async with self._db.begin_nested():
                self._db.add(team)
                await self._db.flush()
            return team, True
        except IntegrityError:
            existing = await self._find_team(namespace_key=namespace_key, slug=slug)
            if existing is None:
                raise
            existing.display_name = display_name
            existing.description = description
            existing.linear_team_key = linear_team_key
            await self._db.flush()
            return existing, False

    async def update_team(
        self,
        *,
        namespace_key: str,
        slug: str,
        display_name: str | None = None,
        description: str | None = None,
        update_description: bool = False,
        linear_team_key: str | None = None,
        update_linear_team_key: bool = False,
    ) -> Team:
        """Partially update a team. The slug is never changed.

        ``display_name`` is left alone when ``None`` because it can never be
        cleared. ``description`` and ``linear_team_key`` are both nullable, so
        each takes a separate flag distinguishing "clear it" from "leave it".
        """
        team = await self.get_team_or_404(namespace_key=namespace_key, slug=slug)
        if display_name is not None:
            team.display_name = display_name
        if update_description:
            team.description = description
        if update_linear_team_key:
            team.linear_team_key = linear_team_key
        await self._db.flush()
        return team

    async def delete_team(
        self, *, namespace_key: str, slug: str, force: bool = False
    ) -> int:
        """Delete a team and return how many memberships went with it.

        Raises ``ConflictError`` when the team still has members and ``force``
        is not set, so an accidental delete cannot silently drop membership.
        """
        team = await self.get_team_or_404(namespace_key=namespace_key, slug=slug)
        member_count = await self.count_members(
            namespace_key=namespace_key, team_id=team.id
        )
        if member_count and not force:
            raise ConflictError(
                error_code=ErrorCode.TEAM_HAS_MEMBERS,
                detail=(
                    f"Team '{slug}' still has {member_count} member(s) and was "
                    f"not deleted."
                ),
                resource="Team",
                resource_id=slug,
                hint=(
                    "Remove the members first, or repeat the request with "
                    "force=true to delete the team and its memberships."
                ),
            )
        # Chat sessions opened under this team survive it, with no team. There
        # is no foreign key doing that for us - see ``clear_team`` for why one
        # would break this endpoint outright - so the detach is an explicit
        # step, and it happens before the delete.
        await AgentSessionsService(self._db).clear_team(
            namespace_key=namespace_key, team_id=team.id
        )
        # Memberships are removed by the composite foreign key's ON DELETE
        # CASCADE; the relationship is configured with passive_deletes so
        # SQLAlchemy does not load them just to delete them one by one.
        await self._db.delete(team)
        await self._db.flush()
        return member_count

    async def list_teams(
        self, *, namespace_key: str, cursor: int | None = None, limit: int = 20
    ) -> TeamListPage:
        """List teams in ``namespace_key`` ordered by ID descending.

        Pass the ``next_cursor`` from one page back as ``cursor`` to fetch the
        following page. Member counts are resolved in a single grouped query
        over the returned page rather than per row.
        """
        page_stmt = (
            select(Team)
            .where(Team.namespace_key == namespace_key)
            .order_by(Team.id.desc())
        )
        if cursor is not None:
            page_stmt = page_stmt.where(Team.id < cursor)
        result = await self._db.execute(page_stmt.limit(limit + 1))
        rows = list(result.scalars().all())
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        next_cursor = str(rows[-1].id) if has_more and rows else None

        total_stmt = (
            select(func.count())
            .select_from(Team)
            .where(Team.namespace_key == namespace_key)
        )
        total_result = await self._db.execute(total_stmt)
        total = int(total_result.scalar_one())

        return TeamListPage(
            teams=rows,
            member_counts=await self._member_counts(
                namespace_key=namespace_key, team_ids=[team.id for team in rows]
            ),
            total=total,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    async def list_members(
        self, *, namespace_key: str, team_id: int
    ) -> list[TeamMember]:
        """Return a team's memberships ordered by agent name.

        Unpaginated: teams are expected to hold tens of agents, not thousands.
        """
        stmt = (
            select(TeamMember)
            .where(
                TeamMember.namespace_key == namespace_key,
                TeamMember.team_id == team_id,
            )
            .order_by(TeamMember.agent_name.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def teams_for_agents(
        self, *, namespace_key: str, agent_names: list[str]
    ) -> dict[str, Team]:
        """Map each named agent to the one team used to label it.

        Membership is many-to-many, so an agent can match several teams. Views
        that show a single team per agent take the alphabetically first slug:
        arbitrary, but stable across calls and independent of the order teams
        were created or joined in. Agents on no team are absent from the map.
        """
        if not agent_names:
            return {}
        stmt = (
            select(TeamMember.agent_name, Team)
            .join(
                Team,
                (Team.id == TeamMember.team_id)
                & (Team.namespace_key == TeamMember.namespace_key),
            )
            .where(
                TeamMember.namespace_key == namespace_key,
                TeamMember.agent_name.in_(agent_names),
            )
            .order_by(TeamMember.agent_name.asc(), Team.slug.asc())
        )
        result = await self._db.execute(stmt)
        teams: dict[str, Team] = {}
        for agent_name, team in result.all():
            teams.setdefault(agent_name, team)
        return teams

    async def count_members(self, *, namespace_key: str, team_id: int) -> int:
        """Count the agents in one team."""
        counts = await self._member_counts(
            namespace_key=namespace_key, team_ids=[team_id]
        )
        return counts.get(team_id, 0)

    async def add_member(
        self, *, namespace_key: str, slug: str, agent_name: str
    ) -> tuple[TeamMember, bool]:
        """Add an agent to a team. Returns ``(membership, added)``.

        Idempotent: a repeat add returns the existing row with ``added=False``
        and leaves ``joined_at`` untouched. Raises 404 when either the team or
        the agent is missing from the namespace.
        """
        team = await self.get_team_or_404(namespace_key=namespace_key, slug=slug)
        await self._require_agent(namespace_key=namespace_key, agent_name=agent_name)

        existing = await self._find_member(
            namespace_key=namespace_key, team_id=team.id, agent_name=agent_name
        )
        if existing is not None:
            return existing, False

        member = TeamMember(
            namespace_key=namespace_key,
            team_id=team.id,
            agent_name=agent_name,
        )
        # Savepoint rationale matches ``upsert_team``: a concurrent add for the
        # same (team, agent) must not poison the surrounding transaction.
        try:
            async with self._db.begin_nested():
                self._db.add(member)
                await self._db.flush()
            return member, True
        except IntegrityError:
            existing = await self._find_member(
                namespace_key=namespace_key, team_id=team.id, agent_name=agent_name
            )
            if existing is None:
                raise
            return existing, False

    async def remove_member(
        self, *, namespace_key: str, slug: str, agent_name: str
    ) -> bool:
        """Remove an agent from a team. Returns whether a row was deleted.

        Idempotent: removing a non-member returns ``False``. The team itself
        must exist; an unknown slug is a 404.
        """
        team = await self.get_team_or_404(namespace_key=namespace_key, slug=slug)
        existing = await self._find_member(
            namespace_key=namespace_key, team_id=team.id, agent_name=agent_name
        )
        if existing is None:
            return False
        await self._db.delete(existing)
        await self._db.flush()
        return True

    async def _find_team(self, *, namespace_key: str, slug: str) -> Team | None:
        stmt = select(Team).where(
            Team.namespace_key == namespace_key,
            Team.slug == slug,
        )
        result = await self._db.execute(stmt)
        return cast(Team | None, result.scalars().first())

    async def _find_member(
        self, *, namespace_key: str, team_id: int, agent_name: str
    ) -> TeamMember | None:
        stmt = select(TeamMember).where(
            TeamMember.namespace_key == namespace_key,
            TeamMember.team_id == team_id,
            TeamMember.agent_name == agent_name,
        )
        result = await self._db.execute(stmt)
        return cast(TeamMember | None, result.scalars().first())

    async def _member_counts(
        self, *, namespace_key: str, team_ids: list[int]
    ) -> dict[int, int]:
        if not team_ids:
            return {}
        stmt = (
            select(TeamMember.team_id, func.count())
            .where(
                TeamMember.namespace_key == namespace_key,
                TeamMember.team_id.in_(team_ids),
            )
            .group_by(TeamMember.team_id)
        )
        result = await self._db.execute(stmt)
        counts = {int(team_id): int(count) for team_id, count in result.all()}
        return {team_id: counts.get(team_id, 0) for team_id in team_ids}

    async def _require_agent(self, *, namespace_key: str, agent_name: str) -> None:
        """Require a registered agent in this namespace.

        Membership carries no foreign key to ``agents`` so that grouping does
        not depend on registration order, which means the existence check has
        to happen here rather than in the schema.
        """
        stmt = select(Agent.name).where(
            Agent.namespace_key == namespace_key,
            Agent.name == agent_name,
        )
        result = await self._db.execute(stmt)
        if result.first() is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_NOT_FOUND,
                detail=f"Agent '{agent_name}' not found",
                resource="Agent",
                resource_id=agent_name,
                hint=(
                    "Register the agent before adding it to a team, and verify "
                    "it belongs to this namespace."
                ),
            )
