"""agent session attachments, their bytes, and their turn bindings

Three tables and one column, all additive, no backfill, and every one of them
inert until a deployment turns the executor on.

`agent_session_attachments` holds the metadata. `agent_session_attachment_blobs`
holds the bytes, and the split is deliberate: listing attachments, evaluating a
metadata gate and rendering a transcript must never pull a twenty-megabyte
`bytea` into memory, and one table would put that one careless `select()` away
from every reader.

`agent_turn_attachments` records which files a turn carried and what happened to
each. The verdict lives on the binding rather than on the file because controls
change between turns: a `blocked` marker on the attachment would leave a row
permanently condemned by a control that may no longer exist.

Two constraint choices are worth stating rather than inferring.

`uq_agent_session_attachments_content` is scoped per session, not per namespace.
Per namespace would let a caller in a shared namespace discover that somebody
else had already uploaded a given file by observing a dedupe hit, which is a
content oracle over a hash.

The `CHECK` on both size columns is 52,428,800 rather than the configured
ceiling. The setting defaults well below it; this is the bound a direct database
write cannot smuggle past, which is why it exists in addition to the streamed
count in the handler.

`agent_task_steps.attachments_summary` is the durable record of what one hop
carried. It survives the session and it survives the blob TTL that reclaims the
bytes.

Revision ID: b2e7c94a1d55
Revises: a3f9d2c81e64
Create Date: 2026-08-03 17:10:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b2e7c94a1d55"
down_revision = "a3f9d2c81e64"
branch_labels = None
depends_on = None


_NAMESPACE_DEFAULT = sa.text("'default'")
_HARD_MAX_BYTES = 52428800
_SIZE_CHECK = f"size_bytes > 0 AND size_bytes <= {_HARD_MAX_BYTES}"


def upgrade() -> None:
    op.create_table(
        "agent_session_attachments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_key", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column(
            "display_name_normalized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("original_name_sha256", sa.String(64), nullable=False),
        sa.Column("declared_mime", sa.String(128), nullable=False),
        sa.Column("sniffed_mime", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("delivered_sha256", sa.String(64), nullable=True),
        sa.Column("delivered_mime", sa.String(128), nullable=True),
        sa.Column("delivered_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("failure_code", sa.String(32), nullable=True),
        # Null until a deployment runs the converter. Counting pages means
        # opening the file, and this server does not open files.
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("estimated_tokens", sa.Integer(), nullable=True),
        sa.Column("converted_from", sa.String(128), nullable=True),
        sa.Column(
            "origin",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'operator_upload'"),
        ),
        sa.Column("origin_ref", sa.String(128), nullable=True),
        sa.Column("created_by_hash", sa.String(64), nullable=True),
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
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_attachments_session_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "namespace_key", "id", name="uq_agent_session_attachments_ns_id"
        ),
        sa.UniqueConstraint(
            "namespace_key", "attachment_key", name="uq_agent_session_attachments_key"
        ),
        sa.UniqueConstraint(
            "namespace_key",
            "session_id",
            "source_sha256",
            name="uq_agent_session_attachments_content",
        ),
        sa.CheckConstraint(_SIZE_CHECK, name="ck_agent_session_attachments_size"),
    )
    op.create_index(
        "idx_agent_session_attachments_session",
        "agent_session_attachments",
        ["namespace_key", "session_id", "created_at"],
    )
    op.create_index(
        "idx_agent_session_attachments_sweep",
        "agent_session_attachments",
        ["namespace_key", "status", "created_at"],
    )
    op.create_index(
        "idx_agent_session_attachments_origin",
        "agent_session_attachments",
        ["namespace_key", "session_id", "origin"],
    )

    op.create_table(
        "agent_session_attachment_blobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "variant",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'original'"),
        ),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["namespace_key", "attachment_id"],
            [
                "agent_session_attachments.namespace_key",
                "agent_session_attachments.id",
            ],
            name="agent_session_attachment_blobs_attachment_fkey",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "namespace_key",
            "attachment_id",
            "variant",
            name="uq_attachment_blobs_variant",
        ),
        sa.CheckConstraint(_SIZE_CHECK, name="ck_attachment_blobs_size"),
    )

    op.create_table(
        "agent_turn_attachments",
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "verdict",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("blocked_by_control_id", sa.Integer(), nullable=True),
        sa.Column("blocked_reason", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "namespace_key",
            "session_id",
            "trace_id",
            "attachment_id",
            name="agent_turn_attachments_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_key", "attachment_id"],
            [
                "agent_session_attachments.namespace_key",
                "agent_session_attachments.id",
            ],
            name="agent_turn_attachments_attachment_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_agent_turn_attachments_recent",
        "agent_turn_attachments",
        ["namespace_key", "attachment_id", "created_at"],
    )

    op.add_column(
        "agent_task_steps",
        sa.Column(
            "attachments_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_task_steps", "attachments_summary")
    op.drop_index(
        "idx_agent_turn_attachments_recent", table_name="agent_turn_attachments"
    )
    op.drop_table("agent_turn_attachments")
    op.drop_table("agent_session_attachment_blobs")
    op.drop_index(
        "idx_agent_session_attachments_origin", table_name="agent_session_attachments"
    )
    op.drop_index(
        "idx_agent_session_attachments_sweep", table_name="agent_session_attachments"
    )
    op.drop_index(
        "idx_agent_session_attachments_session", table_name="agent_session_attachments"
    )
    op.drop_table("agent_session_attachments")
