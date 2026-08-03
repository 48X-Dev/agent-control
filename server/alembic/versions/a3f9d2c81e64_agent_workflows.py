"""agent workflows and the team default agent

Two additive changes, no backfill, and both inert until somebody configures
something.

`agent_workflows` holds the ordered list of agents a task is handed between. It
is server-side configuration on purpose: an issue body, an issue label and a
YAML line all arrive from whoever has access to the source, and none of them may
reach a decision about which agent runs. Writing one of these rows is ADMIN, at
the tier that authors controls.

`teams.default_agent_name` is the second and last place an agent can come from.
A workflow step that names no agent falls back to it; when neither names one the
task is blocked rather than assigned to whichever agent happens to be handy. It
is nullable with no backfill because a deployment that has configured nothing
still runs its one-step default with the agent an operator names on the command
line, exactly as it did before this revision.

Neither column carries a foreign key to `agents` or `teams`, matching
`team_members.agent_name`. Grouping is descriptive and must not depend on
registration order, and a cascade here would silently delete a workflow when a
team was renamed - taking the record of what four running tasks were configured
to do with it.

Revision ID: a3f9d2c81e64
Revises: f1a6c30d8e77
Create Date: 2026-08-03 15:40:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a3f9d2c81e64"
down_revision = "f1a6c30d8e77"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_workflows",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("workflow_key", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        # No foreign key. A workflow that outlives its team stops resolving and
        # shows up as blocked, which is legible; a cascade would delete the
        # record of what a running task was configured to do.
        sa.Column("team_slug", sa.String(64), nullable=True),
        sa.Column(
            "steps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "namespace_key", "workflow_key", name="ux_agent_workflows_key"
        ),
    )
    op.create_index(
        "ix_agent_workflows_team",
        "agent_workflows",
        ["namespace_key", "team_slug"],
    )
    op.add_column(
        "teams",
        sa.Column("default_agent_name", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("teams", "default_agent_name")
    op.drop_index("ix_agent_workflows_team", table_name="agent_workflows")
    op.drop_table("agent_workflows")
