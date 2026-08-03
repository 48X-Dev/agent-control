"""the conversion cache, keyed by content rather than by attachment

One table, additive, no backfill, inert until a deployment installs a converter.

The key is the whole design. Converting is roughly twenty seconds of OCR per
image on the measured corpus, and the same bytes arrive repeatedly - the same
spec re-uploaded into a second session, the same tracker file fetched by two
steps of one chain. Keying on content makes the second arrival free.

`cache_key` rather than `source_sha256` alone is what makes an entry safe to
reuse: it folds in the conversion contract version and which converters are
installed, so installing OCR does not leave every zero-character image
answering from the day OCR was unavailable. `source_sha256` is stored beside it
so a human can join an entry back to an attachment, and it is indexed because
that is the lookup the delivery path makes.

`text_body` is a separate concern from every other column here and is mapped
deferred for it. It can run to millions of characters and this row is read on
every turn that carries a file.

Revision ID: c4a91e7b3d26
Revises: b2e7c94a1d55
Create Date: 2026-08-03 21:55:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4a91e7b3d26"
down_revision = "b2e7c94a1d55"
branch_labels = None
depends_on = None


_NAMESPACE_DEFAULT = sa.text("'default'")


def upgrade() -> None:
    op.create_table(
        "agent_attachment_conversions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("cache_key", sa.String(96), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("status", sa.String(24), nullable=True),
        sa.Column("converter", sa.String(64), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "meaningful_chars",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "stored_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("failure_code", sa.String(32), nullable=True),
        sa.Column(
            "duration_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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
        sa.PrimaryKeyConstraint("id", name="agent_attachment_conversions_pkey"),
        sa.UniqueConstraint(
            "namespace_key", "cache_key", name="uq_agent_attachment_conversions_key"
        ),
    )
    op.create_index(
        "idx_agent_attachment_conversions_content",
        "agent_attachment_conversions",
        ["namespace_key", "source_sha256"],
    )
    op.create_index(
        "idx_agent_attachment_conversions_sweep",
        "agent_attachment_conversions",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_agent_attachment_conversions_sweep",
        table_name="agent_attachment_conversions",
    )
    op.drop_index(
        "idx_agent_attachment_conversions_content",
        table_name="agent_attachment_conversions",
    )
    op.drop_table("agent_attachment_conversions")
