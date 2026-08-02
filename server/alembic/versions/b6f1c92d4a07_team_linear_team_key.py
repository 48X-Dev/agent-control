"""team linear team key

Adds the nullable `teams.linear_team_key` column, which maps an Agent Control
team to a team in Linear so its milestones can be read through.

Additive and reversible: existing teams keep a NULL key and read as unlinked.
No backfill, and no unique constraint - two Agent Control teams pointing at the
same Linear team is a legitimate arrangement.

Revision ID: b6f1c92d4a07
Revises: d3a5c81f7b42
Create Date: 2026-08-01 14:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b6f1c92d4a07"
down_revision = "d3a5c81f7b42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "teams",
        sa.Column("linear_team_key", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("teams", "linear_team_key")
