"""a capability fingerprint on stored conversion verdicts

One nullable column, additive, no backfill. A failed conversion whose
``failure_code`` names an absent capability is only as durable as the
installed set it was measured against; the fingerprint records that set so the
cache can tell "still true" from "written before the rebuild that fixed it".
``NULL`` - every row written before this column - reads as "unknown
provenance" and buys exactly one retry.

Revision ID: b7d2e94c5a18
Revises: a1c8f52e9b40
Create Date: 2026-08-05 17:30:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7d2e94c5a18"
down_revision = "a1c8f52e9b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_attachment_conversions",
        sa.Column("capability_fingerprint", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_attachment_conversions", "capability_fingerprint")
