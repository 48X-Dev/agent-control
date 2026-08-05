"""Count consecutive same-cause parks, so a dead executor cannot loop forever.

``paused_quota`` retries by design: the lease lapses, the row is reclaimed and
tried again. Right for a budget that refills on the hour; wrong, without a
bound, for an executor that has been down for hours - the operator watches
"1 running" while nothing will ever run. The counter lets the server convert
the third consecutive executor-unreachable park into ``blocked``, which a
human clears with cancel once the process is back.

Revision ID: a1c8f52e9b40
Revises: e9d3b7a54c12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a1c8f52e9b40"
down_revision = "e9d3b7a54c12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_tasks",
        sa.Column(
            "repeat_park_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_tasks", "repeat_park_count")
