"""agent dispatch state

One row per namespace holding the ceilings on the dispatch loop and the two
flags that stop it. Additive, no backfill, and inert on a deployment with no
dispatcher pointed at it: the row is created on first use by the turn path, and
until then every read of it reports the defaults below.

Three notes on the shape.

The primary key is `namespace_key` alone. There is exactly one of these rows per
namespace by construction, which is what lets the turn path's refusal be one
`INSERT ... ON CONFLICT DO UPDATE` rather than a read followed by a write - the
same argument `turn_locks.py` makes about the lock it owns, and for the same
reason: a read-then-write here would let two dispatch turns both see "under
budget" and both spend.

`turns_window_start` plus `turns_in_window` is a fixed window rather than a
sliding one. A sliding window needs a row per turn to count over; this needs two
integers and one statement, at the cost of permitting up to twice the ceiling
across a window boundary. That is the right trade for a ceiling whose job is to
stop an autonomous loop rather than to smooth a chatty client, and it is the
only shape that can be enforced in one statement on the turn path.

There is deliberately no `tasks_in_window`. Tasks are rows in `agent_tasks` with
a `created_at`, so the import ceiling counts them directly, in the transaction
that inserts them. A counter column for something already recorded as rows is a
second source of truth waiting to disagree with the first.

Revision ID: f1a6c30d8e77
Revises: d7e4a91c60b2
Create Date: 2026-08-03 14:10:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f1a6c30d8e77"
down_revision = "d7e4a91c60b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_dispatch_state",
        sa.Column("namespace_key", sa.String(255), nullable=False),
        sa.Column(
            "max_tasks_per_hour",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("20"),
        ),
        sa.Column(
            "max_turns_per_hour",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column(
            "turns_window_start",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "turns_in_window", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("dispatch_paused_at", sa.DateTime(timezone=True), nullable=True),
        # A credential tag rather than a person. Browser callers all hash to the
        # same value because the session token carries no subject, which the
        # console has to say wherever it renders this.
        sa.Column("dispatch_paused_by", sa.String(64), nullable=True),
        sa.Column("dispatch_paused_reason", sa.String(500), nullable=True),
        sa.Column("executors_halted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executors_halted_by", sa.String(64), nullable=True),
        sa.Column("executors_halted_reason", sa.String(500), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("namespace_key", name="agent_dispatch_state_pkey"),
    )


def downgrade() -> None:
    op.drop_table("agent_dispatch_state")
