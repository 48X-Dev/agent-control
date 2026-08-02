"""agent configs: per-agent system prompt and model, versioned together

Adds `agent_configs` (one row per agent, holding the system prompt body and the
model id) and `agent_config_versions` (the audit log for both fields).

**There is no `base_url`, `api_base`, `endpoint` or `api_key` column on either
table, and adding one is not a follow-up.** A per-agent endpoint means every
prompt, every tool result and every piece of customer data that agent handles is
posted to a host of the writer's choosing. That is data exfiltration wearing a
config field, and SSRF against whatever network segment the executor sits on.
ADMIN does not defend it: `AuthSettings.api_key_enabled` defaults false, which
installs `NoAuthProvider` and authorizes every operation - ADMIN included - for
anyone who can open a TCP connection to the server port. The endpoint comes from
the executor process's own environment (`AGENT_CONTROL_MODEL_BASE_URL` or
`OPENAI_BASE_URL`, co-equal), and the control plane never sets, reads or stores
either. A different endpoint means a different process, which the
one-agent-per-process topology already requires.

`ck_agent_configs_model_id_shape` is load-bearing for the same reason. A slash
prefix re-selects the LiteLLM provider and a configured `api_base` is ignored for
routing - verified: `litellm.get_llm_provider('bedrock/anthropic.claude-v2',
api_base='http://127.0.0.1:10531/v1')` returns provider `bedrock`. So a slashed
id is a per-agent endpoint by another name. It is refused at settings load, at
the write boundary, by this constraint, and again by the SDK before it constructs
a client.

There is deliberately no constraint enumerating valid model ids and no foreign
key to a models table. The allowlist is server configuration an operator edits
without a migration; a membership constraint would turn removing one line of env
config into a deployment that will not start against existing rows. Shape is
invariant, membership is not.

`agent_config_versions` points its foreign key at `agents` rather than at
`agent_configs`, because clearing a field must not destroy the history that makes
clearing recoverable. It also carries its own `namespace_key`, unlike
`control_versions`, so the isolation filter is local to the query instead of a
property of the call site.

Additive only. No backfill, and no data migration of any existing instruction or
model. An agent with no row here runs exactly as it does today.

Revision ID: e2b7d4a15c93
Revises: a1c4e7b93d80
Create Date: 2026-08-02 12:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "e2b7d4a15c93"
down_revision = "a1c4e7b93d80"
branch_labels = None
depends_on = None


_NAMESPACE_DEFAULT = sa.text("'default'")
_BODY_FORMAT_DEFAULT = sa.text("'text'")
_ORIGIN_DEFAULT = sa.text("'authored'")

_MODEL_ID_SHAPE = (
    "model_id IS NULL OR ("
    "char_length(model_id) BETWEEN 1 AND 128"
    " AND model_id NOT LIKE '%/%'"
    " AND model_id NOT LIKE '%://%')"
)


def upgrade() -> None:
    op.create_table(
        "agent_configs",
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "body_format",
            sa.String(16),
            nullable=False,
            server_default=_BODY_FORMAT_DEFAULT,
        ),
        sa.Column(
            "prompt_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column(
            "current_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("etag", sa.String(64), nullable=True),
        sa.Column("source_instruction", sa.Text(), nullable=True),
        sa.Column("source_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_hash", sa.String(64), nullable=True),
        sa.Column("updated_by_hash", sa.String(64), nullable=True),
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
        sa.PrimaryKeyConstraint("namespace_key", "agent_name", name="agent_configs_pkey"),
        sa.ForeignKeyConstraint(
            ["namespace_key", "agent_name"],
            ["agents.namespace_key", "agents.name"],
            name="agent_configs_agent_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "body IS NULL OR char_length(body) <= 32000",
            name="ck_agent_configs_body_max_length",
        ),
        sa.CheckConstraint(
            "body_format IN ('text')",
            name="ck_agent_configs_body_format",
        ),
        sa.CheckConstraint(_MODEL_ID_SHAPE, name="ck_agent_configs_model_id_shape"),
    )

    op.create_table(
        "agent_config_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "namespace_key",
            sa.String(255),
            nullable=False,
            server_default=_NAMESPACE_DEFAULT,
        ),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("version_num", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "origin",
            sa.String(32),
            nullable=False,
            server_default=_ORIGIN_DEFAULT,
        ),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "body_format",
            sa.String(16),
            nullable=False,
            server_default=_BODY_FORMAT_DEFAULT,
        ),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("etag", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "scan_findings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("changed_by_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "namespace_key",
            "agent_name",
            "version_num",
            name="uq_agent_config_versions_agent_version",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_key", "agent_name"],
            ["agents.namespace_key", "agents.name"],
            name="agent_config_versions_agent_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "event_type IN ('created','updated','prompt_cleared','model_cleared',"
            "'restored','enabled','disabled')",
            name="ck_agent_config_versions_event_type",
        ),
        sa.CheckConstraint(
            "origin IN ('authored','copied_from_reported','restored')",
            name="ck_agent_config_versions_origin",
        ),
    )
    op.create_index(
        "idx_agent_config_versions_agent_recent",
        "agent_config_versions",
        ["namespace_key", "agent_name", sa.text("version_num DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_agent_config_versions_agent_recent",
        table_name="agent_config_versions",
    )
    op.drop_table("agent_config_versions")
    op.drop_table("agent_configs")
