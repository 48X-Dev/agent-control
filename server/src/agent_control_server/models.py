import datetime as dt
from typing import Any

from agent_control_models.agent import StepSchema, normalize_agent_name
from agent_control_models.attachments import ATTACHMENT_HARD_MAX_BYTES
from agent_control_models.base import BaseModel
from agent_control_models.server import EvaluatorSchema
from pydantic import Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .db import Base

DEFAULT_NAMESPACE_KEY = "default"
_NAMESPACE_SERVER_DEFAULT = text("'default'")


class AgentData(BaseModel):
    """Agent metadata stored in JSONB."""

    agent_metadata: dict[str, Any]
    steps: list[StepSchema] = Field(default_factory=list)
    evaluators: list[EvaluatorSchema] = Field(default_factory=list)


# Association table for Policy <> Control many-to-many relationship.
# Composite FKs enforce same-namespace references on both sides.
policy_controls: Table = Table(
    "policy_controls",
    Base.metadata,
    Column(
        "namespace_key",
        String(255),
        primary_key=True,
        nullable=False,
        server_default=_NAMESPACE_SERVER_DEFAULT,
    ),
    Column("policy_id", Integer, primary_key=True, index=True),
    Column("control_id", Integer, primary_key=True, index=True),
    ForeignKeyConstraint(
        ["namespace_key", "policy_id"],
        ["policies.namespace_key", "policies.id"],
        name="policy_controls_policy_fkey",
    ),
    ForeignKeyConstraint(
        ["namespace_key", "control_id"],
        ["controls.namespace_key", "controls.id"],
        name="policy_controls_control_fkey",
    ),
)

# Association table for Agent <> Policy many-to-many relationship.
agent_policies: Table = Table(
    "agent_policies",
    Base.metadata,
    Column(
        "namespace_key",
        String(255),
        primary_key=True,
        nullable=False,
        server_default=_NAMESPACE_SERVER_DEFAULT,
    ),
    Column("agent_name", String(255), primary_key=True, index=True),
    Column("policy_id", Integer, primary_key=True, index=True),
    ForeignKeyConstraint(
        ["namespace_key", "agent_name"],
        ["agents.namespace_key", "agents.name"],
        name="agent_policies_agent_fkey",
    ),
    ForeignKeyConstraint(
        ["namespace_key", "policy_id"],
        ["policies.namespace_key", "policies.id"],
        name="agent_policies_policy_fkey",
    ),
)

# Association table for Agent <> Control direct many-to-many relationship.
agent_controls: Table = Table(
    "agent_controls",
    Base.metadata,
    Column(
        "namespace_key",
        String(255),
        primary_key=True,
        nullable=False,
        server_default=_NAMESPACE_SERVER_DEFAULT,
    ),
    Column("agent_name", String(255), primary_key=True, index=True),
    Column("control_id", Integer, primary_key=True, index=True),
    ForeignKeyConstraint(
        ["namespace_key", "agent_name"],
        ["agents.namespace_key", "agents.name"],
        name="agent_controls_agent_fkey",
    ),
    ForeignKeyConstraint(
        ["namespace_key", "control_id"],
        ["controls.namespace_key", "controls.id"],
        name="agent_controls_control_fkey",
    ),
)


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint(
            "namespace_key", "name", name="uq_policies_namespace_name"
        ),
        UniqueConstraint(
            "namespace_key", "id", name="uq_policies_namespace_id"
        ),
        # Plain index on name preserves name-only lookup performance while
        # service code is still namespace-blind.
        Index("ix_policies_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agents: Mapped[list["Agent"]] = relationship(
        "Agent", secondary=lambda: agent_policies, back_populates="policies"
    )
    # Many-to-many: Policy <> Control (direct relationship, no ControlSet layer)
    controls: Mapped[list["Control"]] = relationship(
        "Control", secondary=lambda: policy_controls, back_populates="policies"
    )


class Control(Base):
    __tablename__ = "controls"
    __table_args__ = (
        Index(
            "idx_controls_namespace_name_active",
            "namespace_key",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        UniqueConstraint(
            "namespace_key", "id", name="uq_controls_namespace_id"
        ),
        # Hard deletes of clone sources are restricted. The request path
        # soft-deletes controls so clone lineage remains intact.
        ForeignKeyConstraint(
            ["namespace_key", "cloned_from_control_id"],
            ["controls.namespace_key", "controls.id"],
            name="controls_cloned_from_control_fkey",
        ),
        # Plain partial index on name preserves name-only lookup performance
        # while service code is still namespace-blind. Mirrors the pattern
        # used for agents and policies; the partial filter matches the
        # existing call sites that already require deleted_at IS NULL.
        Index(
            "ix_controls_name",
            "name",
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_controls_cloned_from",
            "namespace_key",
            "cloned_from_control_id",
            postgresql_where=text("cloned_from_control_id IS NOT NULL"),
            sqlite_where=text("cloned_from_control_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # JSONB payload describing control specifics
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    cloned_from_control_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Many-to-many backref: Control <> Policy
    policies: Mapped[list["Policy"]] = relationship(
        "Policy", secondary=lambda: policy_controls, back_populates="controls"
    )
    # Many-to-many backref: Control <> Agent (direct relationship)
    agents: Mapped[list["Agent"]] = relationship(
        "Agent", secondary=lambda: agent_controls, back_populates="controls"
    )


class ControlVersion(Base):
    __tablename__ = "control_versions"
    __table_args__ = (
        UniqueConstraint("control_id", "version_num", name="uq_control_versions_control_version"),
        Index("idx_control_versions_control_created", "control_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    control_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("controls.id"), nullable=False
    )
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("char_length(name) >= 10", name="ck_agents_name_min_length"),
        CheckConstraint("name ~ '^[a-z0-9:_-]+$'", name="ck_agents_name_format"),
        # Plain index on name preserves name-only lookup performance while
        # service code is still namespace-blind.
        Index("ix_agents_name", "name"),
    )

    namespace_key: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        nullable=False,
        server_default=_NAMESPACE_SERVER_DEFAULT,
    )
    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    policies: Mapped[list["Policy"]] = relationship(
        "Policy", secondary=lambda: agent_policies, back_populates="agents"
    )
    controls: Mapped[list["Control"]] = relationship(
        "Control", secondary=lambda: agent_controls, back_populates="agents"
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(), server_default=text("CURRENT_TIMESTAMP"), nullable=False, index=True
    )

    @validates("name")
    def _normalize_name(self, _key: str, value: str) -> str:
        return normalize_agent_name(value)


class ControlBinding(Base):
    """Attaches a control to an opaque external target.

    Each row is a single attachment scoped to a namespace. Uniqueness is
    enforced on ``(namespace_key, target_type, target_id, control_id)``.
    The ``enabled`` flag is a soft toggle - a disabled binding is preserved
    but excluded from the effective control set at runtime.

    Same-namespace integrity is enforced by the composite foreign key on
    ``(namespace_key, control_id)``: a binding cannot reference a control
    from another namespace.

    Soft deletes on the parent control (``deleted_at IS NOT NULL``) do not
    cascade to bindings; only hard deletes do. The runtime resolver is
    responsible for excluding soft-deleted controls when computing the
    effective control set.

    Future evolution: per-agent overrides and exemptions within a target
    are intentionally not modeled here. Two paths are possible if and when
    they become a product requirement:

    - re-introduce an ``agent_name`` column (with a partial-index pair on
      ``agent_name IS NULL`` / ``IS NOT NULL``) and an ``enabled``-aware
      most-specific-wins resolver. Supports both per-agent additions and
      per-agent exemptions.
    - or merge target-bearing resolution with the existing
      ``agent_controls`` table at runtime. Supports per-agent additions
      only; exemptions still require schema work because ``agent_controls``
      has no ``enabled`` flag.
    """

    __tablename__ = "control_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_key", "control_id"],
            ["controls.namespace_key", "controls.id"],
            name="control_bindings_control_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "namespace_key",
            "target_type",
            "target_id",
            "control_id",
            name="uq_control_bindings_target_control",
        ),
        Index(
            "idx_control_bindings_lookup",
            "namespace_key",
            "target_type",
            "target_id",
        ),
        # Leading-control_id index covers list_bindings(control_id=...)
        # filters and the ON DELETE CASCADE path from controls.
        Index(
            "idx_control_bindings_control",
            "namespace_key",
            "control_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    control_id: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


# =============================================================================
# Team Models
# =============================================================================


class Team(Base):
    """A named group of agents, scoped to a namespace.

    Teams are descriptive: binding a control to a team has no effect on the
    team's members. ``slug`` is the stable key and is immutable once the row
    exists (enforced at the request boundary, not by the schema); a rename
    changes ``display_name`` only.
    """

    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("namespace_key", "slug", name="uq_teams_namespace_slug"),
        # Required so team_members can reference this table through a composite
        # same-namespace foreign key.
        UniqueConstraint("namespace_key", "id", name="uq_teams_namespace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Key of the Linear team milestones are read from. Nullable and
    # deliberately unconstrained beyond length: teams that predate the Linear
    # integration, and teams that will never be linked to it, stay valid.
    linear_team_key: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The agent a workflow step falls back to when it names none. Nullable and
    # carrying no foreign key, for the same reason ``team_members.agent_name``
    # carries none: grouping is descriptive and must not depend on registration
    # order. Membership is checked at the request boundary instead, where the
    # refusal can say which of the two rows is missing.
    default_agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    members: Mapped[list["TeamMember"]] = relationship(
        "TeamMember",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TeamMember(Base):
    """Membership of one agent in one team.

    The composite primary key lets an agent belong to several teams; a unique
    constraint on ``(namespace_key, agent_name)`` would later restrict agents to
    one team each without reshaping either table. ``agent_name`` carries no
    foreign key to ``agents`` on purpose: grouping is descriptive and should not
    depend on registration order. The composite foreign key on
    ``(namespace_key, team_id)`` keeps a member and its team in one namespace.
    """

    __tablename__ = "team_members"
    __table_args__ = (
        PrimaryKeyConstraint(
            "namespace_key", "team_id", "agent_name", name="team_members_pkey"
        ),
        ForeignKeyConstraint(
            ["namespace_key", "team_id"],
            ["teams.namespace_key", "teams.id"],
            name="team_members_team_fkey",
            ondelete="CASCADE",
        ),
        # Reverse lookup ("which teams is this agent in") always filters on the
        # namespace too, so the index leads with namespace_key.
        Index("idx_team_members_agent", "namespace_key", "agent_name"),
    )

    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    team_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    team: Mapped["Team"] = relationship("Team", back_populates="members")

    @validates("agent_name")
    def _normalize_agent_name(self, _key: str, value: str) -> str:
        return normalize_agent_name(value)


# =============================================================================
# Executor Binding and Chat Session Models
# =============================================================================


class AgentRuntime(Base):
    """Which executor process serves one agent.

    Without this row nothing can answer "where do I send a turn for this
    agent", so it is the precondition for a session existing at all. One row
    per agent: the Python SDK holds a single agent per process and the ADK
    plugin refuses to initialize under a second name, so an agent maps to its
    own executor rather than sharing one.

    The composite foreign key ties the binding to a registered agent in the
    same namespace and removes it when the agent goes. ``enabled`` is a soft
    toggle so an executor can be drained without losing its coordinates.
    """

    __tablename__ = "agent_runtimes"
    __table_args__ = (
        PrimaryKeyConstraint("namespace_key", "agent_name", name="agent_runtimes_pkey"),
        ForeignKeyConstraint(
            ["namespace_key", "agent_name"],
            ["agents.namespace_key", "agents.name"],
            name="agent_runtimes_agent_fkey",
            ondelete="CASCADE",
        ),
    )

    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    executor_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'google_adk'")
    )
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    executor_app_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    @validates("agent_name")
    def _normalize_agent_name(self, _key: str, value: str) -> str:
        return normalize_agent_name(value)


class AgentConfig(Base):
    """One agent's system prompt and its model, versioned together.

    Both fields on one row because they are one editing decision: an operator
    who moves an agent to a cheaper model usually adjusts its prompt in the same
    sitting, and the diff of that change belongs in one history. One row also
    means one ``current_version``, which doubles as the optimistic-concurrency
    token, so a write validates without a subquery over the versions table.

    **There is no ``base_url``, ``api_base``, ``endpoint`` or ``api_key`` column
    here, and there is not going to be one.** A per-agent endpoint means every
    prompt, tool result and piece of customer data an agent handles is posted to
    a host of the writer's choosing - exfiltration wearing a config field, plus
    SSRF onto the segment the executor sits on. ADMIN does not defend it,
    because ``api_key_enabled`` defaults false and installs a provider that
    authorizes every operation for everyone. The endpoint is the executor
    process's own environment.

    ``ck_agent_configs_model_id_shape`` is load-bearing rather than cosmetic. A
    slash prefix re-selects the LiteLLM provider and a configured ``api_base``
    is ignored for routing, so a slashed id is a destination selector in a field
    the UI describes as a name. It is rejected at settings load, at the write
    boundary, here, and again by the SDK.

    There is deliberately **no** constraint enumerating valid model ids and no
    foreign key to a models table. The allowlist is server configuration an
    operator edits without a migration, and a membership constraint would turn
    removing one line of env config into a deployment that will not start
    against existing rows. Shape is invariant; membership is not.
    """

    __tablename__ = "agent_configs"
    __table_args__ = (
        PrimaryKeyConstraint("namespace_key", "agent_name", name="agent_configs_pkey"),
        ForeignKeyConstraint(
            ["namespace_key", "agent_name"],
            ["agents.namespace_key", "agents.name"],
            name="agent_configs_agent_fkey",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "body IS NULL OR char_length(body) <= 32000",
            name="ck_agent_configs_body_max_length",
        ),
        CheckConstraint(
            "body_format IN ('text')",
            name="ck_agent_configs_body_format",
        ),
        CheckConstraint(
            "model_id IS NULL OR ("
            "char_length(model_id) BETWEEN 1 AND 128"
            " AND model_id NOT LIKE '%/%'"
            " AND model_id NOT LIKE '%://%')",
            name="ck_agent_configs_model_id_shape",
        ),
    )

    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # NULL means cleared or never set. The agent falls back to whatever its own
    # code declares, which is what makes the rollout zero-risk: an agent with no
    # row runs exactly as it does today.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_format: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'text'")
    )
    prompt_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Reported by the agent process under an AUTHENTICATED operation, so this is
    # untrusted text. Never sent to a model by Agent Control and never used to
    # pre-fill an editor.
    source_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_reported_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    @validates("agent_name")
    def _normalize_agent_name(self, _key: str, value: str) -> str:
        return normalize_agent_name(value)


class AgentConfigVersion(Base):
    """One entry in an agent config's audit log.

    Full bodies rather than diffs: a prompt is at most tens of kilobytes, and
    reconstructing text from a diff chain is a class of bug nobody needs. "From
    what to what" is answered by diffing consecutive rows, which the client does.

    Two divergences from ``ControlVersion``, both deliberate.

    The foreign key targets ``agents``, not ``agent_configs``. Clearing a field
    is a state change, not a row removal, and history has to survive it - that
    is the whole point of having history. Only deleting the agent takes the log
    with it, which is right, since the agent row is the tenancy anchor.

    ``namespace_key`` is a column here. ``ControlVersion`` has none and gets its
    isolation from the call site loading the parent first, which is correct
    today and makes every future query against that table namespace-blind by
    default. Carrying it locally means the filter is a property of the query.
    """

    __tablename__ = "agent_config_versions"
    __table_args__ = (
        UniqueConstraint(
            "namespace_key",
            "agent_name",
            "version_num",
            name="uq_agent_config_versions_agent_version",
        ),
        Index(
            "idx_agent_config_versions_agent_recent",
            "namespace_key",
            "agent_name",
            text("version_num DESC"),
        ),
        ForeignKeyConstraint(
            ["namespace_key", "agent_name"],
            ["agents.namespace_key", "agents.name"],
            name="agent_config_versions_agent_fkey",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "event_type IN ('created','updated','prompt_cleared','model_cleared',"
            "'restored','enabled','disabled')",
            name="ck_agent_config_versions_event_type",
        ),
        CheckConstraint(
            "origin IN ('authored','copied_from_reported','restored')",
            name="ck_agent_config_versions_origin",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'authored'")
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_format: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'text'")
    )
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Advisory only. The value is the record, including the record that a human
    # saw a finding and saved anyway.
    scan_findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Identifies a credential, not a person. Under the shipped default provider
    # every dashboard caller hashes to the same value, so the UI column is
    # labelled "credential".
    changed_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    @validates("agent_name")
    def _normalize_agent_name(self, _key: str, value: str) -> str:
        return normalize_agent_name(value)


class AgentSession(Base):
    """One chat session, mapped from namespace-scoped identity onto an executor.

    The executor owns the conversation. This row owns who is allowed to see it.
    That makes four things load-bearing, and each one is here for a reason that
    is not obvious from the column list.

    ``uq_agent_sessions_executor_global`` has no ``namespace_key`` in it. The
    executor's session store knows nothing about namespaces, so a per-namespace
    constraint would let namespace B insert a row pointing at exactly the same
    executor session as a namespace-A row and then read A's whole transcript
    through a lookup that passes every namespace filter in the service layer.
    The constraint has to prevent adoption, not merely prevent duplication.
    ``executor_user_id`` is minted as ``f"{namespace_key}:{uuid4().hex}"`` for
    the same reason, and no request model accepts any part of the triple.

    ``team_id`` carries no foreign key. A composite ``ON DELETE SET NULL`` would
    null every referencing column, including ``namespace_key``, which is NOT
    NULL - so deleting a team with a live session would abort. Same-namespace
    membership is enforced in the service, and the team-delete path clears this
    column so the sessions survive.

    ``in_flight_since`` and ``in_flight_trace_id`` are unused until turns exist.
    They are here anyway, because splitting them into a second migration to save
    two columns nobody reads is not a saving.

    ``created_by_hash`` is a hash, not an identifier. ``caller_id`` is the first
    eight characters of a live API key under the default provider; storing it
    raw and serializing it would be credential disclosure. It is never returned
    by any endpoint.
    """

    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint(
            "namespace_key", "session_key", name="uq_agent_sessions_namespace_key"
        ),
        UniqueConstraint(
            "executor_app_name",
            "executor_user_id",
            "executor_session_id",
            name="uq_agent_sessions_executor_global",
        ),
        UniqueConstraint("namespace_key", "id", name="uq_agent_sessions_namespace_id"),
        Index(
            "idx_agent_sessions_agent_recent",
            "namespace_key",
            "agent_name",
            text("last_activity_at DESC"),
        ),
        Index(
            "idx_agent_sessions_in_flight",
            "namespace_key",
            "status",
            "in_flight_since",
        ),
        Index("idx_agent_sessions_team", "namespace_key", "team_id"),
        Index("idx_agent_sessions_task", "namespace_key", "agent_task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    session_key: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set when the dispatcher opens a session for one step of one task. It is
    # what lets the turn path tell a fleet turn from a human chat turn, which
    # is how the namespace budget, the dispatch pause and the executor kill
    # switch get to be refusals inside ``_acquire_turn`` rather than checks in
    # the process being budgeted. Also the third branch of
    # ``require_content_access``: a task's session has no human owner, so
    # oversight of it cannot be restricted to the caller who opened it.
    #
    # No foreign key, and for the same reason ``team_id`` has none: a composite
    # ``ON DELETE SET NULL`` would null ``namespace_key`` with it, which is NOT
    # NULL, so deleting a task with a live session would abort. Sessions
    # belonging to a task are deleted by the dispatcher when the task ends.
    agent_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    executor_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'google_adk'")
    )
    executor_app_name: Mapped[str] = mapped_column(String(255), nullable=False)
    executor_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    executor_session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    created_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    in_flight_since: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    in_flight_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    @validates("agent_name")
    def _normalize_agent_name(self, _key: str, value: str) -> str:
        return normalize_agent_name(value)


class AgentSessionNudge(Base):
    """One piece of human guidance waiting for an agent's next model call.

    A queue, with the two properties that make at-least-once delivery honest.

    ``claim_count`` and ``injection_attempts`` are separate columns because
    they answer different questions and only one of them may expire a row.
    A claim taken by an executor that then died has to be redelivered, so
    ``claim_count`` moves and nothing else does. An injection that was really
    attempted and failed is the only thing that counts against the row's life,
    so expiry keys on ``injection_attempts`` alone. Collapse them into one
    counter and a queue of ten nudges reports seven as undelivered after three
    claim cycles without any of them having been attempted - which is the
    failure the whole design exists to prevent, arrived at from the other side.

    ``claimed_by`` records the actor the claiming runtime token was minted for.
    Under the session-bound token that identifies the *session*, not the
    process, so it is constant for every claim on one session and cannot
    distinguish a swallowed nudge from a delivered one. It is kept because it
    is the only machine-side attribution available and it costs a column; it is
    deliberately not surfaced as "who read this".
    """

    __tablename__ = "agent_session_nudges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_nudges_session_fkey",
            ondelete="CASCADE",
        ),
        Index(
            "idx_agent_session_nudges_drain",
            "namespace_key",
            "session_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    created_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    injection_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    rejected_by_control: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AgentSessionHalt(Base):
    """One operator stop, latched to one turn.

    Not a ``kind`` column on the nudge table, because the two behave in
    opposite directions on every mechanism that table has. A nudge is bound to
    a session and lands at whatever model call happens next, possibly hours
    later; a halt is bound to ``target_trace_id`` and is unclaimable outside
    that turn, which is what stops a stale stop from killing a turn the human
    deliberately started afterwards. A nudge queue has an ordering and a
    per-call cap; a halt is a latch, which the unique constraint states
    directly. And a nudge whose claiming process died must be redelivered,
    while a halt whose claiming process died already got what the human asked
    for.

    ``target_trace_id`` is copied from ``agent_sessions.in_flight_trace_id``,
    the liveness marker, and never from ``in_flight_since``, the lock. Those
    two stop being synonyms the moment a turn outlives this server's patience,
    and binding to the lock would hide the stop button at exactly T+timeout -
    the single most likely moment for somebody to reach for it, behind a panel
    showing nothing in flight. That the marker outlives the lock does not mean
    the invocation does: the executor ends one when the request it arrived on
    is dropped, so a halt bound to a timed-out turn is a record rather than a
    delivery, and the next acquire ages it out.
    """

    __tablename__ = "agent_session_halts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_halts_session_fkey",
            ondelete="CASCADE",
        ),
        # One row per turn, unconditionally rather than partially. A halt is a
        # latch, so two halts against one turn are the same event, and a full
        # unique constraint makes double-clicking idempotent by construction.
        UniqueConstraint(
            "namespace_key",
            "session_id",
            "target_trace_id",
            name="uq_agent_session_halts_turn",
        ),
        Index(
            "idx_agent_session_halts_drain",
            "namespace_key",
            "session_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'graceful'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    created_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    applied_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_at_boundary: Mapped[str | None] = mapped_column(String(8), nullable=True)
    applied_tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    turn_ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentSessionPlanStep(Base):
    """One step of one plan an agent declared for itself.

    Every row here is a claim by the agent, not an observation of it. That is
    the reason the table exists at all: an executor's events are not progress,
    and any number derived from counting them moves without meaning. A declared
    plan is the only progress signal in this stack with an author, and it is
    stored so a console can attribute it to that author rather than present it
    as measurement.

    The primary key carries ``plan_revision`` because agents replan, and a
    re-declared plan is a new revision rather than an edit. Earlier revisions
    are kept: a replan is a thing that happened, and a table that overwrote it
    would show a person different steps than the ones they read a minute ago,
    with nothing saying why.

    ``updated_at`` is per step and is what staleness is read from. A plan whose
    last write was an hour ago is an old plan; it is emphatically not a plan
    that is 40% done, and no column here can be turned into that number.

    ``declared_at`` is stored rather than derived from the earliest
    ``updated_at``. The two agree only until every step has been marked, after
    which the earliest update is later than the declaration and a console would
    report a plan as declared at a moment it was not.
    """

    __tablename__ = "agent_session_plan_steps"
    __table_args__ = (
        PrimaryKeyConstraint(
            "namespace_key",
            "session_id",
            "plan_revision",
            "step_index",
            name="agent_session_plan_steps_pkey",
        ),
        ForeignKeyConstraint(
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_plan_steps_session_fkey",
            ondelete="CASCADE",
        ),
    )

    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plan_revision: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    step_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AgentSessionAttachment(Base):
    """One file attached to one session, and everything about it except bytes.

    Split from the blob table on purpose, and the split is load-bearing. This
    row is read by listing, by the metadata gate and by anything rendering a
    transcript; the bytes are read by exactly two paths, download and delivery.
    One table would put a twenty-megabyte ``bytea`` one careless
    ``select(AgentSessionAttachment)`` away from every one of those readers.

    Neither of those two paths streams as shipped. A download reads the whole
    ``data`` column into memory and answers with it, so a concurrent download
    holds up to ``attachment_max_bytes`` resident in the process that is also
    evaluating policy for every other agent. The split is what keeps that cost
    on the two paths that have a reason to pay it instead of on all of them.

    ``source_sha256`` and ``delivered_sha256`` are separate columns because for
    a converted file they are different artifacts. The delivery path hashes the
    blob it reads and refuses to send on a mismatch, so the control layer and
    the model are guaranteed to have seen the same bytes.

    **Content uniqueness is per session, not per namespace.** Per namespace
    would let a caller in a shared namespace learn that somebody else had
    already uploaded a given file by observing a dedupe hit, which is a content
    oracle over a hash. Per session it tells a caller only about their own
    conversation.

    ``created_by_hash`` identifies a credential, not a person, and under the
    default provider it is NULL on every row because ``NoAuthProvider`` supplies
    no caller at all. "Who attached this" is not answerable in either state and
    no endpoint claims it is.
    """

    __tablename__ = "agent_session_attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_key", "session_id"],
            ["agent_sessions.namespace_key", "agent_sessions.id"],
            name="agent_session_attachments_session_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "namespace_key", "id", name="uq_agent_session_attachments_ns_id"
        ),
        UniqueConstraint(
            "namespace_key", "attachment_key", name="uq_agent_session_attachments_key"
        ),
        UniqueConstraint(
            "namespace_key",
            "session_id",
            "source_sha256",
            name="uq_agent_session_attachments_content",
        ),
        Index(
            "idx_agent_session_attachments_session",
            "namespace_key",
            "session_id",
            "created_at",
        ),
        Index(
            "idx_agent_session_attachments_sweep",
            "namespace_key",
            "status",
            "created_at",
        ),
        Index(
            "idx_agent_session_attachments_origin",
            "namespace_key",
            "session_id",
            "origin",
        ),
        CheckConstraint(
            f"size_bytes > 0 AND size_bytes <= {ATTACHMENT_HARD_MAX_BYTES}",
            name="ck_agent_session_attachments_size",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attachment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name_normalized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # The name as supplied survives only as a hash. A name that had to be
    # defused is not a name to store and render later.
    original_name_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    declared_mime: Mapped[str] = mapped_column(String(128), nullable=False)
    sniffed_mime: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    delivered_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivered_mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivered_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Null until a deployment runs the converter. Counting pages means opening
    # the file, and this process does not open files.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    converted_from: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'operator_upload'")
    )
    origin_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AgentSessionAttachmentBlob(Base):
    """The bytes. One row per artifact of one attachment.

    ``extracted_text`` lives here rather than in a ``TEXT`` column on the parent
    for the same reason the split exists at all: it can reach millions of
    characters and must never be pulled by an incautious ``select()`` of the
    metadata row.

    Deleting rows here is the ordinary way an attachment ends. The bytes are
    reclaimed on a timer and the metadata row stays as a tombstone, so the
    order is: bytes on a clock, metadata on the cascade, and the cascade may
    never run.
    """

    __tablename__ = "agent_session_attachment_blobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_key", "attachment_id"],
            [
                "agent_session_attachments.namespace_key",
                "agent_session_attachments.id",
            ],
            name="agent_session_attachment_blobs_attachment_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "namespace_key",
            "attachment_id",
            "variant",
            name="uq_attachment_blobs_variant",
        ),
        CheckConstraint(
            f"size_bytes > 0 AND size_bytes <= {ATTACHMENT_HARD_MAX_BYTES}",
            name="ck_attachment_blobs_size",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    attachment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    variant: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'original'")
    )
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AgentTurnAttachment(Base):
    """Which files one turn carried, and what happened to each.

    The composite primary key makes binding one file to one turn twice
    idempotent by construction, which is the reasoning one-halt-per-turn
    already uses.

    **The verdict lives here rather than on the attachment.** Controls change
    between turns, so a ``blocked`` marker on the file itself would leave a row
    permanently condemned by a control that may no longer exist, or leave a
    ``ready`` row unchanged after a control was added that would now deny it.
    Per binding, "was this ever sent" is answerable and each answer is about one
    evaluation at one moment.
    """

    __tablename__ = "agent_turn_attachments"
    __table_args__ = (
        PrimaryKeyConstraint(
            "namespace_key",
            "session_id",
            "trace_id",
            "attachment_id",
            name="agent_turn_attachments_pkey",
        ),
        ForeignKeyConstraint(
            ["namespace_key", "attachment_id"],
            [
                "agent_session_attachments.namespace_key",
                "agent_session_attachments.id",
            ],
            name="agent_turn_attachments_attachment_fkey",
            ondelete="CASCADE",
        ),
        Index(
            "idx_agent_turn_attachments_recent",
            "namespace_key",
            "attachment_id",
            "created_at",
        ),
    )

    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attachment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    verdict: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    blocked_by_control_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AgentAttachmentConversion(Base):
    """One conversion result, keyed by what was converted rather than by which
    attachment asked for it.

    **Not a column on the attachment**, and the key is the reason. Converting
    is expensive - about twenty seconds of OCR per image on the measured
    corpus - and the same bytes arrive repeatedly: the same spec re-uploaded
    into a second session, the same tracker file fetched by two steps of one
    chain. Keying on content means the second arrival is free. Keying on the
    attachment would mean paying again for a file this deployment has already
    read.

    ``cache_key`` rather than ``source_sha256`` alone is what makes the entry
    safe to reuse. It folds in the contract version and which converters are
    installed, so installing Docling does not leave every zero-character PNG
    answering from the day OCR was unavailable. ``source_sha256`` is kept
    beside it only so a human can join this back to an attachment.

    ``capability_fingerprint`` is stamped by every stored verdict and consulted
    only for failed ones whose ``failure_code`` names an absent capability. The
    key sees whole converters appearing; this sees the finer grain - a format
    extra arriving inside an installed converter - and a failed row whose stamp
    no longer matches the installed set is read as a miss and converted once
    more. ``NULL`` marks rows written before the column existed, which reads as
    "unknown" and buys the same single retry.

    ``text`` is deferred. It can run to millions of characters and this row is
    read by the delivery path on every turn that carries a file; an incautious
    ``select(AgentAttachmentConversion)`` pulling the text of every cached
    conversion into the process is the same defect the blob table was split out
    to avoid, one table further down.
    """

    __tablename__ = "agent_attachment_conversions"
    __table_args__ = (
        UniqueConstraint(
            "namespace_key", "cache_key", name="uq_agent_attachment_conversions_key"
        ),
        Index(
            "idx_agent_attachment_conversions_content",
            "namespace_key",
            "source_sha256",
        ),
        Index("idx_agent_attachment_conversions_sweep", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    cache_key: Mapped[str] = mapped_column(String(96), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'queued'")
    )
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    converter: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text_body: Mapped[str | None] = mapped_column(
        "text_body", Text, nullable=True, deferred=True
    )
    text_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    meaningful_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    stored_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capability_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


_TERMINAL_TASK_STATUS_SQL = "status NOT IN ('completed', 'failed', 'cancelled')"
"""The predicate both task indexes are partial on.

It names the three terminal statuses and nothing else, so every other status -
``paused_quota`` and ``running_unknown`` included - holds its source ref. That
is paired with a reclaim predicate covering the same set, so a held slot is
always recoverable by something rather than merely held."""


class AgentWorkflow(Base):
    """The ordered list of agents a task is handed between.

    Server-side configuration, and that placement is the whole security
    argument for this table. An issue body, an issue label and a YAML line all
    arrive from whoever has access to the source; none of them reaches which
    agent runs, how many turns it gets, or what it is asked to do. Writing one
    of these rows is ADMIN, at the tier that authors controls, because naming
    agents and shaping prompts is the same class of authority: agents differ in
    system prompt, in bound controls and in tools, so choosing the agent is
    choosing the blast radius.

    ``steps`` is JSONB validated by ``AgentWorkflowStep`` on the way in and on
    the way out. A column per step would need a second table and a position
    column to express something that is always read whole, in order, four
    entries at most; the shape a workflow is read in is the shape it is stored
    in.

    ``team_slug`` is nullable and carries no foreign key, matching
    ``team_members.agent_name``: a workflow that outlives a renamed or deleted
    team stops resolving and shows up as ``blocked``, rather than cascading
    away and leaving queued tasks pointing at nothing.

    **Nothing here is a channel between agents.** There is no field naming
    another step, no message an agent can address, and no way for a step to
    learn a later one exists. The dispatcher walks this list, writes each
    agent's output to ``agent_task_steps``, and starts a separate guarded turn
    on a separate session for the next one.
    """

    __tablename__ = "agent_workflows"
    __table_args__ = (
        UniqueConstraint(
            "namespace_key", "workflow_key", name="ux_agent_workflows_key"
        ),
        # "Which workflows belong to this team" on the console's team page, and
        # the delete guard's count. Always filtered by namespace first.
        Index("ix_agent_workflows_team", "namespace_key", "team_slug"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    workflow_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    team_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AgentTask(Base):
    """One unit of work in the dispatch ledger, and the claim over it.

    The loop that executes this row runs in another process. What this table
    owns is the *claim*, and it owns it because Postgres is the only thing two
    dispatchers share. Linear has no compare-and-swap, so moving an issue's
    state and reading it back is a read-then-write across a network: the bug
    ``turn_locks.py`` exists to prevent, with worse latency.

    Four things here are load-bearing.

    ``ux_agent_tasks_open_source_ref`` is partial, and both halves of that
    matter. Partial, because a finished task must not block the same issue
    being queued again next month - reopened issues are real. Unique over the
    non-terminal set, because that is what makes "the same issue claimed twice"
    impossible for two dispatchers, two replicas and a double-clicked button at
    once, in the database rather than in a handler.

    ``source_kind`` stays ``'linear'`` for both the milestone path and the
    team-label path. Had the milestone path used its own kind, one issue queued
    by a press and again by a cron poll would produce two open tasks and two
    agents working it, and the index that exists to prevent exactly that would
    not fire.

    ``source_scope_name`` and ``source_team_key`` are copies taken at import,
    not joins. A milestone deleted in Linear must still leave a legible
    history, and an operator re-linking a team must not silently retarget the
    write-back of four tasks that are already running.

    ``chain_trace_id`` is minted by the server at claim time and is never
    accepted from a caller. The audited party does not author its own audit
    record: a caller-chosen trace could attach one team's hops into another
    team's chain, or make a chain read as fewer hops than actually happened.
    """

    __tablename__ = "agent_tasks"
    __table_args__ = (
        UniqueConstraint("namespace_key", "task_key", name="uq_agent_tasks_key"),
        UniqueConstraint("namespace_key", "id", name="uq_agent_tasks_namespace_id"),
        Index(
            "ux_agent_tasks_open_source_ref",
            "namespace_key",
            "source_kind",
            "source_ref",
            unique=True,
            postgresql_where=text(_TERMINAL_TASK_STATUS_SQL),
            sqlite_where=text(_TERMINAL_TASK_STATUS_SQL),
        ),
        Index(
            "ix_agent_tasks_scope",
            "namespace_key",
            "source_scope_kind",
            "source_scope_ref",
            postgresql_where=text(_TERMINAL_TASK_STATUS_SQL),
            sqlite_where=text(_TERMINAL_TASK_STATUS_SQL),
        ),
        # The claim poll: "queued tasks in this namespace, oldest first". Also
        # what the reclaim sweep reads, which is the same query with a
        # different status.
        Index("ix_agent_tasks_queue", "namespace_key", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    task_key: Mapped[str] = mapped_column(String(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_scope_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_scope_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_scope_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_team_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # Untrusted input, stored in full. The envelope truncates it and marks the
    # cut inline; storing the truncated version would show an operator less
    # than the tracker holds with nothing saying so.
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    team_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'queued'")
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Who imported it, and who claimed it. Two hashes rather than one, because
    # the accept path compares them: a credential that ran the agents may not
    # also approve their work, and the local-credential path has three tiers
    # and no per-key operation allowlist, so that separation cannot be a tier.
    created_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    chain_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Bookkeeping, and deliberately not the resume rule. A dispatcher that dies
    # between a completed step and this counter leaves it behind, which is the
    # half that is allowed to be wrong.
    current_step: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    turns_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Consecutive parks with the same failure code, reset by any step that
    # actually starts. What turns "retry every lease, forever" into a bounded
    # number of tries when the cause is a dead executor rather than a budget.
    repeat_park_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AgentTaskStep(Base):
    """What one agent produced on one hop, and the reason resume is sound.

    Without this table the reclaim rule is unsound. A dispatcher dying between
    a 200 from ``POST /turns`` and its own bookkeeping would resume at the next
    step with no prior report, and the envelope would then either carry an
    empty prior-report block - which is how the next agent invents the missing
    work and reports it confidently - or fail a step that actually succeeded,
    already spent money, and possibly already acted through a tool.

    ``output_text`` is the durable record, not a pointer to one. Sessions are
    deleted when a task ends, so a transcript link would 404 within a
    fortnight; the text is what survives to be posted back to the tracker.

    ``attempts`` exists because the unique index is on ``(task_id,
    step_index)`` and a reclaimed task resumes at the index it abandoned. The
    row is reused rather than duplicated, and the counter is what keeps
    "abandoned once, then re-run" visible instead of overwritten. It is also
    the only place a duplicated side effect would show, which is the honest
    cost of resuming at all.

    ``turn_trace_id`` is this hop's own trace. The chain is assembled from
    these rows, never from a trace a caller supplied.
    """

    __tablename__ = "agent_task_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_key", "task_id"],
            ["agent_tasks.namespace_key", "agent_tasks.id"],
            name="agent_task_steps_task_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint("task_id", "step_index", name="ux_agent_task_steps_index"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # Nullable because the session is deleted when the task ends. That is the
    # ordinary end state, not a fault.
    session_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    turn_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'running'")
    )
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What this hop actually carried, one small object per delivered or refused
    # file. The step row is the queryable audit record of one hop: it survives
    # whether or not the session does, and after the blob TTL reclaims the bytes
    # it is what still answers "did this step have the spec" a week later.
    # Bounded by the per-turn attachment ceiling, so it is a small column and
    # not a blob wearing a JSON costume. No bytes, no text, no URL.
    attachments_summary: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @validates("agent_name")
    def _normalize_agent_name(self, _key: str, value: str) -> str:
        return normalize_agent_name(value)


class AgentTaskWriteback(Base):
    """What one task proposes to write back to its tracker, and where that is.

    Plan section 5.6. The queue is its own table so the write-back retries
    independently of the task: a task reaches ``completed`` whether or not its
    comment landed, because conflating "the work is done" with "the ticket was
    updated" makes a Linear outage look like failed work, and the operator
    response to those two is completely different.

    ``kind`` splits the two writes this system ever makes. A ``comment`` row is
    sent by the server on the finish path, behind the write flag, after the
    body passes controls evaluation. A ``status_change`` row is created in
    ``awaiting_approval`` and does nothing until a human presses accept - it is
    5.7's review queue, and it never moves by timer, retry, or dispatcher.

    ``ux_agent_task_writebacks_step_kind`` makes the enqueue idempotent: a
    reclaimed step that re-runs re-enqueues into the same row rather than
    queueing a second comment. The residual duplicate the plan accepts is two
    *processes* passing the marker check concurrently, not two rows.

    ``approved_by_hash`` identifies a credential, not a person, the same caveat
    ``caller_identity.py`` documents, and a console must not render it as a
    name. It is written on accept; a rejection records only its reason, which
    is what the plan asks and no more.
    """

    __tablename__ = "agent_task_writebacks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_key", "task_id"],
            ["agent_tasks.namespace_key", "agent_tasks.id"],
            name="agent_task_writebacks_task_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "task_id", "step_index", "kind", name="ux_agent_task_writebacks_step_kind"
        ),
        # The review queue read: awaiting rows in a namespace, oldest first.
        Index("ix_agent_task_writebacks_review", "namespace_key", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    namespace_key: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=_NAMESPACE_SERVER_DEFAULT
    )
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'comment'")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    # The full composed comment for a comment row; the task's final output for
    # a status_change row. Sanitized before it lands here, so what was shown to
    # the reviewer is byte-for-byte what the digest was computed over.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    target_state_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    approved_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class AgentDispatchState(Base):
    """One namespace's dispatch ceilings, and the two switches that stop it.

    A single row per namespace, created on first use. Everything on it is a
    ceiling on a loop that runs in another process, which is the only reason it
    is in this database at all: **a budget enforced by the process being
    budgeted is not a control.** A dispatcher in a retry loop, a second
    dispatcher started by a different operator, or any holder of an ordinary key
    calling ``POST /turns`` directly all spend without consulting a limit held
    in the dispatcher's memory.

    Four things here are load-bearing.

    ``turns_window_start`` and ``turns_in_window`` are a *fixed* window rather
    than a sliding one, and that is a deliberate trade. A sliding window needs a
    row per turn to count over; a fixed window needs two integers and one
    statement, at the cost of allowing up to twice the ceiling across a window
    boundary. For a rate limit on human chat that would be sloppy. For a ceiling
    whose job is to stop an autonomous loop before it spends a fortune, an
    allowance that is occasionally 2x and never unbounded is the right shape,
    and it is the shape that can be enforced in one statement on the turn path.

    There is no ``tasks_in_window`` counter. Tasks are rows in ``agent_tasks``
    with a ``created_at``, so the import ceiling is counted from them directly.
    A counter column for something already recorded as rows is a second source
    of truth waiting to disagree with the first.

    ``dispatch_paused_at`` and ``executors_halted_at`` are two flags and not one
    enum, because they are different authorities that can be held at the same
    time: an operator pauses new work, then escalates to refusing everything,
    and clearing the escalation must not silently clear the pause underneath it.

    Neither flag is a binding change. An earlier draft stopped the fleet by
    setting ``enabled = false`` on every ``agent_runtimes`` row; bindings
    disabled for unrelated reasons then become indistinguishable, so recovery
    turns on things somebody deliberately turned off. An emergency stop that
    destroys the state you need to recover from it makes operators reluctant to
    press it, which is the worst property an emergency stop can have.
    """

    __tablename__ = "agent_dispatch_state"

    namespace_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    max_tasks_per_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("20")
    )
    max_turns_per_hour: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )
    turns_window_start: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    turns_in_window: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    dispatch_paused_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A credential tag, never a person: browser callers all hash identically
    # because the session token carries no subject. Named ``_by`` to match the
    # plan's column list, and documented here so nobody reads it as an author.
    dispatch_paused_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_paused_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    executors_halted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executors_halted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executors_halted_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


# =============================================================================
# Observability Models
# =============================================================================


class ControlExecutionEventDB(Base):
    """
    Raw control execution events with minimal indexed columns + JSONB.

    Schema designed for simplicity and flexibility:
    - Indexed columns: namespace_key, control_execution_id, timestamp, agent_name
    - Full event stored in JSONB 'data' column
    - Query-time aggregation from JSONB fields
    - No migrations needed for new event fields

    Primary access pattern: (namespace_key, agent_name, timestamp DESC) for stats queries.
    Expression index on (data->>'control_id') for grouping.
    """

    __tablename__ = "control_execution_events"

    # Primary key
    control_execution_id: Mapped[str] = mapped_column(
        String(36)
    )

    # Minimal indexed columns for efficient queries
    namespace_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default=_NAMESPACE_SERVER_DEFAULT,
    )
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Full event data as JSONB
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False,
    )

    # Composite index for agent + time queries (primary access pattern)
    __table_args__ = (
        PrimaryKeyConstraint(
            "namespace_key",
            "control_execution_id",
            name="control_execution_events_pkey",
        ),
        Index("ix_events_namespace_agent_time", "namespace_key", "agent_name", timestamp.desc()),
        Index("ix_events_data_control_id", text("(data ->> 'control_id'::text)")),
    )
