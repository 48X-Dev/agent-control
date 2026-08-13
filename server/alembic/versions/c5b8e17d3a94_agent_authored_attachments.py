"""agent-authored attachments: the draft marker and the tracker's copy

Two nullable columns and one check, additive, no backfill. Every existing row
predates the ``agent`` origin, so the check holds over them unchanged.

``linear_asset_url`` is written by a later phase and read by this one: while it
is null the row is the only copy of the file and the blob sweep must leave it
alone.

Revision ID: c5b8e17d3a94
Revises: c3f8a1d90e57
Create Date: 2026-08-13 12:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c5b8e17d3a94"
down_revision = "c3f8a1d90e57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_session_attachments",
        sa.Column("agent_output_kind", sa.String(8), nullable=True),
    )
    op.add_column(
        "agent_session_attachments",
        sa.Column("linear_asset_url", sa.String(1024), nullable=True),
    )
    op.create_check_constraint(
        "ck_agent_session_attachments_agent_output",
        "agent_session_attachments",
        "(agent_output_kind IS NULL) = (origin <> 'agent')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_session_attachments_agent_output",
        "agent_session_attachments",
        type_="check",
    )
    op.drop_column("agent_session_attachments", "linear_asset_url")
    op.drop_column("agent_session_attachments", "agent_output_kind")
