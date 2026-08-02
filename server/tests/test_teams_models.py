"""Coverage for the ``teams`` and ``team_members`` tables: slug uniqueness,
membership cardinality, and same-namespace integrity."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_control_server.models import Team, TeamMember


@pytest.fixture
def clean_tables(db_engine: Engine):
    def _truncate() -> None:
        with db_engine.begin() as conn:
            conn.execute(
                text("TRUNCATE TABLE team_members, teams RESTART IDENTITY CASCADE")
            )

    _truncate()
    yield
    _truncate()


def _insert_team(
    engine: Engine,
    *,
    namespace_key: str,
    slug: str,
    display_name: str | None = None,
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO teams (namespace_key, slug, display_name) "
                    "VALUES (:ns, :slug, :display_name) RETURNING id"
                ),
                {
                    "ns": namespace_key,
                    "slug": slug,
                    "display_name": display_name or slug,
                },
            ).scalar_one()
        )


def _insert_member(
    engine: Engine, *, namespace_key: str, team_id: int, agent_name: str
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO team_members (namespace_key, team_id, agent_name) "
                "VALUES (:ns, :team_id, :agent_name)"
            ),
            {"ns": namespace_key, "team_id": team_id, "agent_name": agent_name},
        )


def _member_count(engine: Engine, *, namespace_key: str, team_id: int) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM team_members "
                    "WHERE namespace_key = :ns AND team_id = :team_id"
                ),
                {"ns": namespace_key, "team_id": team_id},
            ).scalar_one()
        )


def test_team_inserts_with_verbatim_display_name(
    db_engine: Engine, clean_tables: None
) -> None:
    team_id = _insert_team(
        db_engine,
        namespace_key="ns-one",
        slug="sales-outreach",
        display_name="Sales & Outreach",
    )

    with db_engine.begin() as conn:
        row = conn.execute(
            text("SELECT slug, display_name FROM teams WHERE id = :id"),
            {"id": team_id},
        ).one()

    assert row.slug == "sales-outreach"
    assert row.display_name == "Sales & Outreach"


def test_duplicate_slug_within_namespace_rejected(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_team(db_engine, namespace_key="ns-one", slug="operations")
    with pytest.raises(IntegrityError):
        _insert_team(db_engine, namespace_key="ns-one", slug="operations")


def test_same_slug_allowed_across_namespaces(
    db_engine: Engine, clean_tables: None
) -> None:
    first = _insert_team(db_engine, namespace_key="ns-one", slug="engineering")
    second = _insert_team(db_engine, namespace_key="ns-two", slug="engineering")
    assert first != second


def test_agent_can_belong_to_several_teams(
    db_engine: Engine, clean_tables: None
) -> None:
    marketing = _insert_team(db_engine, namespace_key="ns-one", slug="marketing")
    operations = _insert_team(db_engine, namespace_key="ns-one", slug="operations")

    _insert_member(
        db_engine,
        namespace_key="ns-one",
        team_id=marketing,
        agent_name="outreach-bot-one",
    )
    _insert_member(
        db_engine,
        namespace_key="ns-one",
        team_id=operations,
        agent_name="outreach-bot-one",
    )

    assert _member_count(db_engine, namespace_key="ns-one", team_id=marketing) == 1
    assert _member_count(db_engine, namespace_key="ns-one", team_id=operations) == 1


def test_duplicate_membership_rejected(db_engine: Engine, clean_tables: None) -> None:
    team_id = _insert_team(db_engine, namespace_key="ns-one", slug="engineering")
    _insert_member(
        db_engine,
        namespace_key="ns-one",
        team_id=team_id,
        agent_name="build-agent-one",
    )
    with pytest.raises(IntegrityError):
        _insert_member(
            db_engine,
            namespace_key="ns-one",
            team_id=team_id,
            agent_name="build-agent-one",
        )


def test_membership_rejects_cross_namespace_team_reference(
    db_engine: Engine, clean_tables: None
) -> None:
    team_id = _insert_team(db_engine, namespace_key="ns-two", slug="engineering")
    with pytest.raises(IntegrityError):
        _insert_member(
            db_engine,
            namespace_key="ns-one",
            team_id=team_id,
            agent_name="build-agent-one",
        )


def test_membership_cascades_on_team_delete(
    db_engine: Engine, clean_tables: None
) -> None:
    doomed = _insert_team(db_engine, namespace_key="ns-one", slug="marketing")
    survivor = _insert_team(db_engine, namespace_key="ns-one", slug="operations")
    for team_id in (doomed, survivor):
        _insert_member(
            db_engine,
            namespace_key="ns-one",
            team_id=team_id,
            agent_name="outreach-bot-one",
        )

    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM teams WHERE id = :id"), {"id": doomed})

    assert _member_count(db_engine, namespace_key="ns-one", team_id=doomed) == 0
    assert _member_count(db_engine, namespace_key="ns-one", team_id=survivor) == 1


def test_membership_does_not_require_a_registered_agent(
    db_engine: Engine, clean_tables: None
) -> None:
    # Deliberate: team_members carries no foreign key to agents, so grouping
    # does not depend on registration order.
    team_id = _insert_team(db_engine, namespace_key="ns-one", slug="engineering")
    _insert_member(
        db_engine,
        namespace_key="ns-one",
        team_id=team_id,
        agent_name="never-registered-agent",
    )
    assert _member_count(db_engine, namespace_key="ns-one", team_id=team_id) == 1


def test_namespace_key_falls_back_to_the_server_default(
    db_engine: Engine, clean_tables: None
) -> None:
    with db_engine.begin() as conn:
        namespace_key = conn.execute(
            text(
                "INSERT INTO teams (slug, display_name) "
                "VALUES ('operations', 'Operations') RETURNING namespace_key"
            )
        ).scalar_one()

    assert namespace_key == "default"


def test_empty_slug_is_not_rejected_by_the_database(
    db_engine: Engine, clean_tables: None
) -> None:
    # Pins where the gate lives. ``slugify`` returns "" for input with no
    # alphanumeric content and the schema stays permissive so bad rows remain
    # repairable, so callers MUST validate against TeamSlug before inserting.
    team_id = _insert_team(db_engine, namespace_key="ns-one", slug="", display_name="!!!")
    assert team_id > 0


# =============================================================================
# ORM behavior
# =============================================================================


def _namespace_teams(session: Session, namespace_key: str) -> list[Team]:
    return list(
        session.scalars(select(Team).where(Team.namespace_key == namespace_key)).all()
    )


def test_orm_round_trip_normalizes_agent_name(
    db_engine: Engine, clean_tables: None
) -> None:
    with Session(db_engine) as session:
        team = Team(
            namespace_key="ns-one", slug="sales-outreach", display_name="Sales & Outreach"
        )
        team.members.append(TeamMember(namespace_key="ns-one", agent_name="  Outreach-Bot-One  "))
        session.add(team)
        session.commit()
        team_id = team.id

    with Session(db_engine) as session:
        member = session.scalars(
            select(TeamMember).where(TeamMember.team_id == team_id)
        ).one()
        assert member.agent_name == "outreach-bot-one"


@pytest.mark.parametrize("agent_name", ["short", "has spaces here", "UPPER!!!NAME"])
def test_orm_rejects_invalid_agent_name(
    db_engine: Engine, clean_tables: None, agent_name: str
) -> None:
    with pytest.raises(ValueError):
        TeamMember(namespace_key="ns-one", team_id=1, agent_name=agent_name)


def test_orm_delete_cascades_to_members(db_engine: Engine, clean_tables: None) -> None:
    with Session(db_engine) as session:
        team = Team(namespace_key="ns-one", slug="marketing", display_name="Marketing")
        team.members.append(TeamMember(namespace_key="ns-one", agent_name="outreach-bot-one"))
        session.add(team)
        session.commit()
        team_id = team.id

    with Session(db_engine) as session:
        session.delete(session.get(Team, team_id))
        session.commit()

    assert _member_count(db_engine, namespace_key="ns-one", team_id=team_id) == 0


def test_teams_in_another_namespace_are_invisible(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_team(db_engine, namespace_key="ns-one", slug="engineering")
    _insert_team(db_engine, namespace_key="ns-two", slug="engineering")
    _insert_team(db_engine, namespace_key="ns-two", slug="marketing")

    with Session(db_engine) as session:
        assert [team.slug for team in _namespace_teams(session, "ns-one")] == ["engineering"]
        assert sorted(team.slug for team in _namespace_teams(session, "ns-two")) == [
            "engineering",
            "marketing",
        ]
        assert _namespace_teams(session, "ns-three") == []


def test_members_in_another_namespace_are_invisible(
    db_engine: Engine, clean_tables: None
) -> None:
    first = _insert_team(db_engine, namespace_key="ns-one", slug="engineering")
    second = _insert_team(db_engine, namespace_key="ns-two", slug="engineering")
    _insert_member(
        db_engine, namespace_key="ns-one", team_id=first, agent_name="build-agent-one"
    )
    _insert_member(
        db_engine, namespace_key="ns-two", team_id=second, agent_name="build-agent-one"
    )

    with Session(db_engine) as session:
        rows = session.scalars(
            select(TeamMember).where(TeamMember.namespace_key == "ns-one")
        ).all()

    assert [row.team_id for row in rows] == [first]


def test_updated_at_advances_on_display_name_change(
    db_engine: Engine, clean_tables: None
) -> None:
    with Session(db_engine) as session:
        team = Team(namespace_key="ns-one", slug="operations", display_name="Operations")
        session.add(team)
        session.commit()
        team_id = team.id
        created_at = team.created_at
        original_updated_at = team.updated_at

    with db_engine.begin() as conn:
        conn.execute(text("SELECT pg_sleep(0.01)"))

    with Session(db_engine) as session:
        reloaded = session.get(Team, team_id)
        assert reloaded is not None
        reloaded.display_name = "Ops"
        session.commit()
        session.refresh(reloaded)

        assert reloaded.updated_at > original_updated_at
        assert reloaded.created_at == created_at
