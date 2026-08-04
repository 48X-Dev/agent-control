"""agent task writebacks

Adds `agent_task_writebacks`, the queue for the two writes this system ever
makes against a tracker: a step's comment, and a proposal to close the issue
that waits for a human. Additive only, no backfill. A deployment with
`AGENT_CONTROL_LINEAR_WRITE_ENABLED` unset (the default) accumulates rows and
sends nothing.

Three things here are load-bearing.

The foreign key is composite on `(namespace_key, task_id)` against
`uq_agent_tasks_namespace_id`, matching `agent_task_steps`. A single-column
version would let a row in one namespace reference a task in another.

`ux_agent_task_writebacks_step_kind` is unique over `(task_id, step_index,
kind)`, which makes the enqueue idempotent: a reclaimed step that re-runs
lands in the row that already exists instead of queueing a second comment.
The duplicate the plan accepts as residual is two processes passing the
comment-marker check concurrently, not two rows.

`status` is a plain string checked in the service, like every other status
column in this schema. The review queue reads
`(namespace_key, status, created_at)`, which is what the index covers.

Revision ID: e9d3b7a54c12
Revises: c4a91e7b3d26
Create Date: 2026-08-04 17:30:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e9d3b7a54c12"
down_revision = "c4a91e7b3d26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_task_writebacks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column(
            "kind", sa.String(16), nullable=False, server_default=sa.text("'comment'")
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("target_state_id", sa.String(64), nullable=True),
        sa.Column("decision_digest", sa.String(80), nullable=True),
        sa.Column("approved_by_hash", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["namespace_key", "task_id"],
            ["agent_tasks.namespace_key", "agent_tasks.id"],
            name="agent_task_writebacks_task_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "task_id", "step_index", "kind", name="ux_agent_task_writebacks_step_kind"
        ),
    )
    op.create_index(
        "ix_agent_task_writebacks_review",
        "agent_task_writebacks",
        ["namespace_key", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_task_writebacks_review", table_name="agent_task_writebacks"
    )
    op.drop_table("agent_task_writebacks")
