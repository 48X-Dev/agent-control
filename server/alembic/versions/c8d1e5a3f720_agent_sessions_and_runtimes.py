"""agent sessions and executor bindings

Adds `agent_runtimes` (which executor process serves an agent) and
`agent_sessions` (the namespace-scoped mapping onto an executor-side
conversation).

Two constraints here are load-bearing and are not the obvious choice.

`uq_agent_sessions_executor_global` deliberately omits `namespace_key`. The
executor's own session store has no namespace concept, so this table is the
only boundary between one namespace's transcripts and another's. Scoped per
namespace, the constraint would have permitted a row in namespace B pointing at
exactly the same executor session as a row in namespace A, which reads as A's
transcript through a lookup that satisfies every namespace filter above it. The
constraint exists to prevent adoption, not duplication.

`agent_sessions.team_id` carries no foreign key. A composite
`ON DELETE SET NULL` nulls every referencing column in Postgres unless the
PG15+ column-list form is used, so it would try to write `namespace_key = NULL`
and abort against the NOT NULL constraint - turning `DELETE /teams/{slug}` into
a 500 for any team with a live session. Same-namespace membership is enforced
in the service layer instead, and the team-delete path clears this column.

`in_flight_since` and `in_flight_trace_id` land here rather than with the turn
endpoints that read them, so this phase keeps its one-migration budget and the
next one needs none.

Additive only. No backfill.

Revision ID: c8d1e5a3f720
Revises: b6f1c92d4a07
Create Date: 2026-08-02 10:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c8d1e5a3f720"
down_revision = "b6f1c92d4a07"
branch_labels = None
depends_on = None


_NAMESPACE_DEFAULT = sa.text("'default'")
_EXECUTOR_KIND_DEFAULT = sa.text("'google_adk'")


def upgrade() -> None:
    op.create_table(
        "agent_runtimes",
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column(
            "executor_kind",
            sa.String(32),
            nullable=False,
            server_default=_EXECUTOR_KIND_DEFAULT,
        ),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("executor_app_name", sa.String(255), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
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
        sa.PrimaryKeyConstraint("namespace_key", "agent_name", name="agent_runtimes_pkey"),
        sa.ForeignKeyConstraint(
            ["namespace_key", "agent_name"],
            ["agents.namespace_key", "agents.name"],
            name="agent_runtimes_agent_fkey",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("session_key", sa.String(64), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column(
            "executor_kind",
            sa.String(32),
            nullable=False,
            server_default=_EXECUTOR_KIND_DEFAULT,
        ),
        sa.Column("executor_app_name", sa.String(255), nullable=False),
        sa.Column("executor_user_id", sa.String(255), nullable=False),
        sa.Column("executor_session_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("created_by_hash", sa.String(64), nullable=True),
        sa.Column("last_trace_id", sa.String(64), nullable=True),
        sa.Column("in_flight_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("in_flight_trace_id", sa.String(64), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name="agent_sessions_pkey"),
        sa.UniqueConstraint(
            "namespace_key", "session_key", name="uq_agent_sessions_namespace_key"
        ),
        # Global on purpose. See the module docstring.
        sa.UniqueConstraint(
            "executor_app_name",
            "executor_user_id",
            "executor_session_id",
            name="uq_agent_sessions_executor_global",
        ),
        # Target for the composite same-namespace foreign keys the nudge and
        # plan tables will add in later phases.
        sa.UniqueConstraint("namespace_key", "id", name="uq_agent_sessions_namespace_id"),
    )

    # Session list for one agent, newest first.
    op.create_index(
        "idx_agent_sessions_agent_recent",
        "agent_sessions",
        ["namespace_key", "agent_name", sa.text("last_activity_at DESC")],
    )
    # Sweep for sessions stuck with a turn in flight.
    op.create_index(
        "idx_agent_sessions_in_flight",
        "agent_sessions",
        ["namespace_key", "status", "in_flight_since"],
    )
    # Covers both the ``?team=`` filter and the team-delete path that clears
    # this column, neither of which has a foreign key to lean on.
    op.create_index(
        "idx_agent_sessions_team",
        "agent_sessions",
        ["namespace_key", "team_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_sessions_team", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_in_flight", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_agent_recent", table_name="agent_sessions")
    op.drop_table("agent_sessions")
    op.drop_table("agent_runtimes")
