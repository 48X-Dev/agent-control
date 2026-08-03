"""agent task ledger

Adds `agent_tasks` and `agent_task_steps`, and one nullable column on
`agent_sessions`. Additive only, no backfill, and nothing here changes the
behaviour of an existing deployment: with no dispatcher pointed at it, these
two tables stay empty and `agent_sessions.agent_task_id` stays null on every
row.

Four things in this revision are load-bearing rather than shape.

`ux_agent_tasks_open_source_ref` is a **partial** unique index over
`(namespace_key, source_kind, source_ref)`, excluding exactly the three
terminal statuses. Unique, because that is what makes "the same source item
claimed twice" impossible for two dispatchers, two replicas and a
double-clicked button at once, in the database rather than in a handler.
Partial, because a finished task must not block the same item being queued
again next month - reopened issues are real. Every non-terminal status,
`paused_quota` and `running_unknown` included, therefore holds the slot, and
the claim statement's reclaim predicate covers the same set from the other
side so a held slot is always recoverable by something.

`source_kind` is one column with `'linear'` covering both the milestone path
and the team-label path. Splitting them would let one issue queued by a human
press and again by a scheduled poll produce two open tasks and two agents
working it, because the index above would not fire.

The foreign key on `agent_task_steps` is composite on `(namespace_key,
task_id)` against `uq_agent_tasks_namespace_id`, matching the nudge, halt and
plan-step tables. A single-column version would let a row in one namespace
reference a task in another.

`agent_sessions.agent_task_id` carries **no** foreign key, for the same reason
`team_id` carries none: a composite `ON DELETE SET NULL` would null
`namespace_key` along with it, and that column is NOT NULL, so deleting a task
with a live session would abort. Sessions belonging to a task are deleted by
the dispatcher when the task ends.

Revision ID: d7e4a91c60b2
Revises: e2b7d4a15c93
Create Date: 2026-08-03 09:30:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d7e4a91c60b2"
down_revision = "e2b7d4a15c93"
branch_labels = None
depends_on = None

_OPEN_TASK_PREDICATE = sa.text("status NOT IN ('completed', 'failed', 'cancelled')")


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("task_key", sa.String(32), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("source_scope_kind", sa.String(32), nullable=True),
        sa.Column("source_scope_ref", sa.String(255), nullable=True),
        # A copy taken at import, not a join. A milestone deleted upstream must
        # still leave a legible history; the row outlives the thing it names.
        sa.Column("source_scope_name", sa.String(255), nullable=True),
        # Resolved at import and read at write-back, so re-linking a team
        # cannot retarget a task that is already running.
        sa.Column("source_team_key", sa.String(32), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("team_slug", sa.String(64), nullable=True),
        sa.Column("workflow_key", sa.String(64), nullable=False),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default=sa.text("'queued'")
        ),
        sa.Column(
            "dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        # Two hashes, not one. The accept path compares them: a credential that
        # ran the agents may not also accept their work, and the local
        # credential path has three tiers and no per-key operation allowlist,
        # so that separation cannot be expressed as a tier.
        sa.Column("created_by_hash", sa.String(64), nullable=True),
        sa.Column("claimed_by_hash", sa.String(64), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chain_trace_id", sa.String(64), nullable=True),
        sa.Column(
            "current_step", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "turns_used", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="agent_tasks_pkey"),
        sa.UniqueConstraint("namespace_key", "task_key", name="uq_agent_tasks_key"),
        # The target of the steps table's composite foreign key.
        sa.UniqueConstraint("namespace_key", "id", name="uq_agent_tasks_namespace_id"),
    )
    op.create_index(
        "ux_agent_tasks_open_source_ref",
        "agent_tasks",
        ["namespace_key", "source_kind", "source_ref"],
        unique=True,
        postgresql_where=_OPEN_TASK_PREDICATE,
        sqlite_where=_OPEN_TASK_PREDICATE,
    )
    op.create_index(
        "ix_agent_tasks_scope",
        "agent_tasks",
        ["namespace_key", "source_scope_kind", "source_scope_ref"],
        postgresql_where=_OPEN_TASK_PREDICATE,
        sqlite_where=_OPEN_TASK_PREDICATE,
    )
    op.create_index(
        "ix_agent_tasks_queue",
        "agent_tasks",
        ["namespace_key", "status", "created_at"],
    )

    op.create_table(
        "agent_task_steps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False, server_default=sa.text("''")),
        # Nullable because the session is deleted when the task ends. That is
        # the ordinary end state and not a fault, which is also why the step's
        # output is stored here rather than linked to a transcript.
        sa.Column("session_key", sa.String(64), nullable=True),
        sa.Column("turn_trace_id", sa.String(64), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'running'")
        ),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column(
            "output_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # A reclaimed task resumes at the index it abandoned, and the unique
        # index below is on (task_id, step_index), so the row is reused rather
        # than duplicated. This counter is what keeps "abandoned once, then
        # re-run" visible instead of overwritten - and it is the only place a
        # duplicated side effect would show.
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="agent_task_steps_pkey"),
        sa.ForeignKeyConstraint(
            ["namespace_key", "task_id"],
            ["agent_tasks.namespace_key", "agent_tasks.id"],
            name="agent_task_steps_task_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("task_id", "step_index", name="ux_agent_task_steps_index"),
    )

    op.add_column(
        "agent_sessions",
        sa.Column("agent_task_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "idx_agent_sessions_task",
        "agent_sessions",
        ["namespace_key", "agent_task_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_sessions_task", table_name="agent_sessions")
    op.drop_column("agent_sessions", "agent_task_id")
    op.drop_table("agent_task_steps")
    op.drop_index("ix_agent_tasks_queue", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_scope", table_name="agent_tasks")
    op.drop_index("ux_agent_tasks_open_source_ref", table_name="agent_tasks")
    op.drop_table("agent_tasks")
