"""agent session nudges and halts

Adds the two tables behind the two human actions on a running agent:
`agent_session_nudges` (guidance that arrives at the next model call and lets
the agent carry on) and `agent_session_halts` (a stop that lands at the next
model or tool boundary and ends the turn).

One migration for both, deliberately. They ship together, they share the
session foreign key, and splitting them would spend two revisions to separate
two tables nobody will ever deploy apart.

Three things here are not the obvious choice.

Both foreign keys are composite on `(namespace_key, session_id)` against
`uq_agent_sessions_namespace_id`, not on `session_id` alone. The single-column
version would let a row in one namespace reference a session in another, which
is the boundary this whole feature sits behind - the executor's own store has
no namespace concept, so these tables must not be the place it leaks.

`uq_agent_session_halts_turn` is a full unique constraint rather than a partial
index on live statuses. A halt is a latch: two halts against one turn are the
same event, and a full constraint makes a double-click idempotent by
construction rather than by service logic that has to remember to be.

The nudge table carries `claim_count` and `injection_attempts` as two columns.
Merging them would let a claim cycle expire a nudge nobody ever tried to
inject, and the human would be shown "undelivered" for text that was never
attempted.

Additive only. No backfill.

Revision ID: f4c7a2b9e310
Revises: c8d1e5a3f720
Create Date: 2026-08-02 16:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f4c7a2b9e310"
down_revision = "c8d1e5a3f720"
branch_labels = None
depends_on = None


_NAMESPACE_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    op.create_table(
        "agent_session_nudges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("created_by_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_trace_id", sa.String(64), nullable=True),
        sa.Column(
            "claim_count", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "injection_attempts",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("rejected_by_control", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id", name="agent_session_nudges_pkey"),
        sa.ForeignKeyConstraint(
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_nudges_session_fkey",
            ondelete="CASCADE",
        ),
    )

    # The claim query: one session's queue, oldest first, by status.
    op.create_index(
        "idx_agent_session_nudges_drain",
        "agent_session_nudges",
        ["namespace_key", "session_id", "status", "created_at"],
    )

    op.create_table(
        "agent_session_halts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("target_trace_id", sa.String(64), nullable=False),
        sa.Column(
            "mode", sa.String(16), nullable=False, server_default=sa.text("'graceful'")
        ),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("created_by_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at_boundary", sa.String(8), nullable=True),
        sa.Column("applied_tool_name", sa.String(64), nullable=True),
        sa.Column("turn_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="agent_session_halts_pkey"),
        sa.ForeignKeyConstraint(
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_halts_session_fkey",
            ondelete="CASCADE",
        ),
        # See the module docstring: unconditional, not partial.
        sa.UniqueConstraint(
            "namespace_key",
            "session_id",
            "target_trace_id",
            name="uq_agent_session_halts_turn",
        ),
    )

    op.create_index(
        "idx_agent_session_halts_drain",
        "agent_session_halts",
        ["namespace_key", "session_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_session_halts_drain", table_name="agent_session_halts")
    op.drop_table("agent_session_halts")
    op.drop_index("idx_agent_session_nudges_drain", table_name="agent_session_nudges")
    op.drop_table("agent_session_nudges")
