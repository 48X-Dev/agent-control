"""agent teams

Adds the `teams` and `team_members` tables so agents can be grouped into
first-class teams (Sales & Outreach, Operations, Marketing, Engineering).

Membership is many-to-many through `team_members`. Restricting agents to a
single team later only requires adding a unique constraint on
`(namespace_key, agent_name)`; no table needs reshaping.

Teams are descriptive at this stage: nothing here participates in control
resolution. The migration is additive only and performs no backfill.

Revision ID: d3a5c81f7b42
Revises: e2b7f4a9c6d1
Create Date: 2026-08-01 12:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d3a5c81f7b42"
down_revision = "e2b7f4a9c6d1"
branch_labels = None
depends_on = None


_NAMESPACE_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="teams_pkey"),
        sa.UniqueConstraint("namespace_key", "slug", name="uq_teams_namespace_slug"),
        # Target for the composite same-namespace foreign key on team_members.
        sa.UniqueConstraint("namespace_key", "id", name="uq_teams_namespace_id"),
    )

    op.create_table(
        "team_members",
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "namespace_key", "team_id", "agent_name", name="team_members_pkey"
        ),
        sa.ForeignKeyConstraint(
            ["namespace_key", "team_id"],
            ["teams.namespace_key", "teams.id"],
            name="team_members_team_fkey",
            ondelete="CASCADE",
        ),
    )
    # Reverse lookup ("which teams is this agent in"). Leads with namespace_key
    # because every membership query is namespace-filtered.
    op.create_index(
        "idx_team_members_agent",
        "team_members",
        ["namespace_key", "agent_name"],
    )


def downgrade() -> None:
    op.drop_index("idx_team_members_agent", table_name="team_members")
    op.drop_table("team_members")
    op.drop_table("teams")
