"""orphan pre-fleet agent sessions

Revision ID: c3f8a1d90e57
Revises: b7d2e94c5a18
Create Date: 2026-08-11 15:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3f8a1d90e57"
down_revision = "b7d2e94c5a18"
branch_labels = None
depends_on = None

# One executor serves one agent under its own name, so from the fleet onward
# executor_app_name equals agent_name. A row where it does not was written
# against the shared executor app, whose sessions live in process memory.
_PRE_FLEET = "executor_app_name <> agent_name"


def upgrade() -> None:
    """Orphan every active session bound to a shared executor app."""
    op.execute(
        sa.text(
            f"""
            UPDATE agent_sessions
            SET status = 'orphaned', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'active'
              AND {_PRE_FLEET}
            """
        )
    )


def downgrade() -> None:
    """Return those same sessions to active."""
    op.execute(
        sa.text(
            f"""
            UPDATE agent_sessions
            SET status = 'active', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'orphaned'
              AND {_PRE_FLEET}
            """
        )
    )
