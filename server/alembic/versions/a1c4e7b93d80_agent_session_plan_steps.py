"""agent session plan steps

Adds `agent_session_plan_steps`, the table behind the only honest progress
signal in this product: a plan the agent declared for itself and steps it marked
itself. Nothing derived from executor event counts is stored here, because a
number that moves without meaning is worse than no number.

Three things are deliberate.

The primary key is `(namespace_key, session_id, plan_revision, step_index)`.
`plan_revision` is part of it because agents replan, and a re-declared plan is a
new revision rather than an edit of the old one. Earlier revisions stay in the
table: a replan is an event worth showing, and overwriting it would silently
change the steps a person had already read.

The foreign key is composite on `(namespace_key, session_id)` against
`uq_agent_sessions_namespace_id`, matching the nudge and halt tables. A
single-column version would let a row in one namespace reference a session in
another.

There is no index beyond the primary key. Every read of this table is "the
steps of one session", which the leading `(namespace_key, session_id)` columns
of the primary key already serve; a second index on the same prefix would be
write cost for nothing.

`declared_at` and `updated_at` are separate columns because they answer
different questions and one cannot be derived from the other. Every row of a
revision is inserted in one transaction, so `declared_at` is the same instant
across the revision and stays put; `updated_at` moves every time the agent
marks that step. Deriving the declaration time from the earliest `updated_at`
would be right until the last step was marked and then quietly report a
declaration that happened later than it did.

Additive only. No backfill.

Revision ID: a1c4e7b93d80
Revises: f4c7a2b9e310
Create Date: 2026-08-02 18:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1c4e7b93d80"
down_revision = "f4c7a2b9e310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_session_plan_steps",
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "plan_revision",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("step_index", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "declared_at",
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
        sa.PrimaryKeyConstraint(
            "namespace_key",
            "session_id",
            "plan_revision",
            "step_index",
            name="agent_session_plan_steps_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_plan_steps_session_fkey",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_session_plan_steps")
