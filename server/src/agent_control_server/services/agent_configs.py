"""Persistence and resolution for per-agent runtime configuration.

One row holds an agent's system prompt and its model. This module owns three
things that are easy to get subtly wrong and expensive to debug afterwards.

**Resolution is server-side and happens on every read.** ``prompt_source`` and
``model_source`` are computed here, against the current allowlist and the
current delivery gate, and the SDK does not re-derive them. That is what makes
"a model removed from the allowlist stops being applied" work without ever
rewriting a stored row: membership is a property of the read, not of the write.

**Every write goes through the same validator.** ``model_id`` is writable only
through the set route and the restore route, and both call
:func:`AgentConfigService.validate_model_allowed`. Section 6 of the design
deliberately refuses a database constraint enumerating valid ids, which makes
"every write is validated" a property of the code rather than of the schema. So
it is stated as an invariant with tests behind it: any future template, clone,
team-provisioning or import path routes through that method or it does not ship.
Under the shipped default provider a missed call site is an *anonymous* write of
an arbitrary model id.

**Version numbers only ever go up.** Restoring copies a version's fields forward
as a new version. A shared history that can be rewritten is a history nobody can
reason about.

Every method takes ``namespace_key`` and filters on it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import cast

from agent_control_models.agent_configs import (
    AgentModelOption,
    BodyFormat,
    ConfigEventType,
    ConfigOrigin,
    DeliveryState,
    ModelSource,
    PromptSource,
    ScanFinding,
)
from agent_control_models.errors import ErrorCode
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config as server_config
from ..errors import BadRequestError, ConflictError, NotFoundError
from ..models import Agent, AgentConfig, AgentConfigVersion

_DEFAULT_PAGINATION_LIMIT = 50
_MAX_PAGINATION_LIMIT = 200


@dataclass(frozen=True)
class ResolvedAgentConfig:
    """A config row plus everything that had to be recomputed to serve it.

    A dataclass rather than extra columns because every field below is derived
    from server state that changes without a write: the allowlist, and whether
    the startup gate opened. Persisting any of them would mean a stored row
    disagreeing with the server that stored it.
    """

    row: AgentConfig | None
    agent_name: str
    prompt_source: PromptSource
    model_source: ModelSource
    model_allowed: bool
    model_entry: AgentModelOption | None
    delivery_state: DeliveryState

    @property
    def current_version(self) -> int:
        return self.row.current_version if self.row is not None else 0


@dataclass(frozen=True)
class AgentConfigVersionPage:
    """One newest-first page of history rows."""

    versions: list[AgentConfigVersion]
    total: int
    has_more: bool
    next_cursor: str | None


def compute_etag(*, current_version: int, body: str | None, model_id: str | None) -> str:
    """Return the opaque token that covers both fields.

    Version *and* content, so a restore that reproduces an earlier state is
    still distinguishable from that earlier state. Both fields, so a model-only
    change produces a new value - a body-only hash would miss exactly the change
    an operator is most likely to make on its own. Opaque, so a client cannot
    fabricate one without having fetched it, which is what makes the divergence
    between a reported etag and the server's own view a usable tamper signal.
    """
    material = f"{body or ''}\x00{model_id or ''}".encode()
    return f"v{current_version}-{hashlib.sha256(material).hexdigest()[:12]}"


async def require_registered_agent(
    db: AsyncSession, *, namespace_key: str, agent_name: str
) -> None:
    """Raise 404 unless the agent is registered in this namespace.

    Runs before any config logic, so naming an agent that does not exist reads
    as "no such agent" rather than as "no configuration", which sends an
    operator hunting for the wrong thing.
    """
    result = await db.execute(
        select(Agent.name).where(
            Agent.namespace_key == namespace_key,
            Agent.name == agent_name,
        )
    )
    if result.first() is None:
        raise NotFoundError(
            error_code=ErrorCode.AGENT_NOT_FOUND,
            detail=f"Agent '{agent_name}' not found",
            resource="Agent",
            resource_id=agent_name,
            hint=(
                "Register the agent before configuring it, and verify it "
                "belongs to this namespace."
            ),
        )


class AgentConfigService:
    """Reads and writes ``agent_configs`` and its version log."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ---------------------------------------------------------------- reads

    async def find_row(self, *, namespace_key: str, agent_name: str) -> AgentConfig | None:
        result = await self._db.execute(
            select(AgentConfig).where(
                AgentConfig.namespace_key == namespace_key,
                AgentConfig.agent_name == agent_name,
            )
        )
        return cast(AgentConfig | None, result.scalars().first())

    async def resolve(self, *, namespace_key: str, agent_name: str) -> ResolvedAgentConfig:
        """Load one agent's config and work out what actually reaches the agent."""
        row = await self.find_row(namespace_key=namespace_key, agent_name=agent_name)
        return self.resolve_row(row, agent_name=agent_name)

    @staticmethod
    def resolve_row(row: AgentConfig | None, *, agent_name: str) -> ResolvedAgentConfig:
        """Derive the source and delivery fields from a row and server state.

        Static so the delivery-gate and allowlist logic has exactly one
        implementation, reachable from a test without a database session.
        """
        delivery_allowed = server_config.AGENT_CONFIG_DELIVERY_ALLOWED
        tier_limit = server_config.AGENT_CONFIG_MODEL_TIER_LIMIT

        if row is None:
            return ResolvedAgentConfig(
                row=None,
                agent_name=agent_name,
                prompt_source=PromptSource.NONE,
                model_source=ModelSource.CODE,
                model_allowed=True,
                model_entry=None,
                delivery_state=(
                    DeliveryState.ACTIVE
                    if delivery_allowed
                    else DeliveryState.BLOCKED_INSECURE_AUTH
                ),
            )

        entry = (
            server_config.model_settings.find(row.model_id)
            if row.model_id is not None
            else None
        )
        model_allowed = row.model_id is None or entry is not None

        # The tier limit only exists on the local-dev override path. It caps the
        # model half while leaving the prompt half fully open, because a prompt
        # has no spend attached and a model on an unauthenticated server does.
        tier_suppressed = (
            entry is not None
            and tier_limit is not None
            and entry.cost_tier != tier_limit
        )

        if row.body is not None and row.prompt_enabled and delivery_allowed:
            prompt_source = PromptSource.MANAGED
        elif row.body is not None or row.source_instruction is not None:
            prompt_source = PromptSource.CODE
        else:
            prompt_source = PromptSource.NONE

        model_source = (
            ModelSource.MANAGED
            if (row.model_id is not None and entry is not None and delivery_allowed
                and not tier_suppressed)
            else ModelSource.CODE
        )

        if not delivery_allowed or tier_suppressed:
            delivery_state = DeliveryState.BLOCKED_INSECURE_AUTH
        elif not row.prompt_enabled:
            delivery_state = DeliveryState.DISABLED
        else:
            delivery_state = DeliveryState.ACTIVE

        return ResolvedAgentConfig(
            row=row,
            agent_name=agent_name,
            prompt_source=prompt_source,
            model_source=model_source,
            model_allowed=model_allowed,
            model_entry=entry if model_allowed else None,
            delivery_state=delivery_state,
        )

    async def list_versions(
        self,
        *,
        namespace_key: str,
        agent_name: str,
        cursor: int | None = None,
        limit: int = _DEFAULT_PAGINATION_LIMIT,
    ) -> AgentConfigVersionPage:
        """Newest-first cursor page, mirroring ``list_control_versions``."""
        limit = max(1, min(limit, _MAX_PAGINATION_LIMIT))

        total_result = await self._db.execute(
            select(func.count())
            .select_from(AgentConfigVersion)
            .where(
                AgentConfigVersion.namespace_key == namespace_key,
                AgentConfigVersion.agent_name == agent_name,
            )
        )
        total = int(total_result.scalar_one())

        stmt = select(AgentConfigVersion).where(
            AgentConfigVersion.namespace_key == namespace_key,
            AgentConfigVersion.agent_name == agent_name,
        )
        if cursor is not None:
            stmt = stmt.where(AgentConfigVersion.version_num < cursor)
        stmt = stmt.order_by(AgentConfigVersion.version_num.desc()).limit(limit + 1)

        rows = list((await self._db.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        next_cursor = str(rows[-1].version_num) if has_more and rows else None
        return AgentConfigVersionPage(
            versions=rows, total=total, has_more=has_more, next_cursor=next_cursor
        )

    async def get_version_or_404(
        self, *, namespace_key: str, agent_name: str, version_num: int
    ) -> AgentConfigVersion:
        result = await self._db.execute(
            select(AgentConfigVersion).where(
                AgentConfigVersion.namespace_key == namespace_key,
                AgentConfigVersion.agent_name == agent_name,
                AgentConfigVersion.version_num == version_num,
            )
        )
        version = cast(AgentConfigVersion | None, result.scalars().first())
        if version is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_CONFIG_NOT_FOUND,
                detail=(
                    f"Version '{version_num}' of the configuration for agent "
                    f"'{agent_name}' not found"
                ),
                resource="AgentConfigVersion",
                resource_id=f"{agent_name}:{version_num}",
                hint="List the versions to see which numbers exist.",
            )
        return version

    # --------------------------------------------------------------- writes

    @staticmethod
    def validate_model_allowed(
        model_id: str | None, *, on_restore: bool = False
    ) -> AgentModelOption | None:
        """The single gate on ``model_id``. Every write path calls this one.

        Shape first, membership second, and the order matters: somebody who
        pasted a URL should be told they pasted a URL, not that their URL is not
        on the allowlist.

        ``on_restore`` changes the status code, not the rule. A save naming an
        unavailable model is a 400 - the caller can pick another. A restore
        naming one is a 409 - the request was well formed and would have been
        correct before an operator edited server config, which is the same shape
        as ``SCHEMA_INCOMPATIBLE``.
        """
        if model_id is None:
            return None

        if "://" in model_id or "/" in model_id:
            raise BadRequestError(
                error_code=ErrorCode.VALIDATION_ERROR,
                detail=(
                    f"Model id {model_id!r} may not contain '/' or '://'. This "
                    "field names a model, not an endpoint."
                ),
                hint=(
                    "A slash prefix re-selects the underlying provider and the "
                    "configured endpoint is ignored for routing, so a slashed id "
                    "would send traffic somewhere nobody chose. The endpoint "
                    "comes from the executor process's own environment; there is "
                    "no per-agent endpoint."
                ),
            )

        entry = server_config.model_settings.find(model_id)
        if entry is not None:
            return entry

        allowed = sorted(e.id for e in server_config.model_settings.allowlist)
        detail = (
            f"Model {model_id!r} is not offered by this server. "
            + (f"Allowed: {', '.join(allowed)}." if allowed else "No models are configured.")
        )
        hint = (
            "Set AGENT_CONTROL_MODELS_ALLOWLIST on the server to offer it."
            if allowed
            else "Configure AGENT_CONTROL_MODELS_ALLOWLIST before choosing a model."
        )
        if on_restore:
            raise ConflictError(
                error_code=ErrorCode.MODEL_NOT_ALLOWED,
                detail=detail,
                resource="AgentConfig",
                resource_id=model_id,
                hint=(
                    "Restore the prompt text and keep the current model instead, "
                    "or re-add the model to the server allowlist."
                ),
            )
        raise BadRequestError(
            error_code=ErrorCode.MODEL_NOT_ALLOWED, detail=detail, hint=hint
        )

    async def _lock_row(self, *, namespace_key: str, agent_name: str) -> None:
        """Serialize version creation on one agent by taking a row lock.

        Without it, two requests both read version 7, both pass the
        ``expected_version`` check, and both write. The lock is a no-op on
        SQLite, which is why the concurrency test has to run against Postgres
        and skip rather than pass vacuously when it is unavailable.
        """
        await self._db.execute(
            select(AgentConfig.current_version)
            .where(
                AgentConfig.namespace_key == namespace_key,
                AgentConfig.agent_name == agent_name,
            )
            .with_for_update()
        )

    @staticmethod
    def _require_expected_version(row: AgentConfig | None, expected_version: int) -> None:
        actual = row.current_version if row is not None else 0
        if actual == expected_version:
            return
        raise ConflictError(
            error_code=ErrorCode.AGENT_CONFIG_VERSION_CONFLICT,
            detail=(
                f"This configuration is at version {actual}, not "
                f"{expected_version}. Somebody else saved while this edit was "
                "open."
            ),
            resource="AgentConfig",
            resource_id=str(actual),
            hint=(
                "Reload the configuration and re-apply the change. One row "
                "carries both the prompt and the model, so a prompt edit and a "
                "model edit conflict with each other by design."
            ),
        )

    async def _append_version(
        self,
        row: AgentConfig,
        *,
        event_type: ConfigEventType,
        origin: ConfigOrigin,
        note: str | None,
        changed_by_hash: str | None,
        scan_findings: list[ScanFinding],
    ) -> AgentConfigVersion:
        """Bump ``current_version``, recompute the etag, and log the new state.

        ``current_version`` is the counter and the concurrency token, so it is
        incremented here rather than derived from ``max(version_num)`` on read.
        The version row still carries the number, which is what makes the unique
        constraint catch a bug in this method rather than letting it through.
        """
        row.current_version += 1
        row.etag = compute_etag(
            current_version=row.current_version, body=row.body, model_id=row.model_id
        )
        version = AgentConfigVersion(
            namespace_key=row.namespace_key,
            agent_name=row.agent_name,
            version_num=row.current_version,
            event_type=event_type.value,
            origin=origin.value,
            body=row.body,
            body_format=row.body_format,
            model_id=row.model_id,
            etag=row.etag,
            note=note,
            scan_findings=[f.model_dump(mode="json") for f in scan_findings],
            changed_by_hash=changed_by_hash,
        )
        self._db.add(version)
        await self._db.flush()
        return version

    async def _load_for_write(
        self, *, namespace_key: str, agent_name: str, expected_version: int
    ) -> AgentConfig | None:
        await require_registered_agent(
            self._db, namespace_key=namespace_key, agent_name=agent_name
        )
        await self._lock_row(namespace_key=namespace_key, agent_name=agent_name)
        row = await self.find_row(namespace_key=namespace_key, agent_name=agent_name)
        self._require_expected_version(row, expected_version)
        return row

    async def _create_row(
        self, *, namespace_key: str, agent_name: str, caller_hash: str | None
    ) -> AgentConfig:
        """Insert the row, or 409 if a concurrent writer created it first.

        ``_lock_row`` cannot serialize the *first* write on an agent, because
        ``SELECT ... FOR UPDATE`` against a row that does not exist yet locks
        nothing: two concurrent creates both read version 0, both pass the
        ``expected_version`` check, and both insert. The savepoint turns the
        loser's primary-key violation into the same 409 every other concurrent
        write produces, rather than a 500 and a poisoned transaction. Same shape
        as ``AgentRuntimeService.upsert_binding``.
        """
        row = AgentConfig(
            namespace_key=namespace_key,
            agent_name=agent_name,
            body=None,
            body_format=BodyFormat.TEXT.value,
            prompt_enabled=True,
            model_id=None,
            current_version=0,
            created_by_hash=caller_hash,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(row)
                await self._db.flush()
        except IntegrityError:
            existing = await self.find_row(
                namespace_key=namespace_key, agent_name=agent_name
            )
            if existing is None:
                raise
            raise ConflictError(
                error_code=ErrorCode.AGENT_CONFIG_VERSION_CONFLICT,
                detail=(
                    f"This configuration is at version {existing.current_version}, "
                    "not 0. Somebody else saved the first version while this edit "
                    "was open."
                ),
                resource="AgentConfig",
                resource_id=str(existing.current_version),
                hint="Reload the configuration and re-apply the change.",
            ) from None
        return row

    async def set_config(
        self,
        *,
        namespace_key: str,
        agent_name: str,
        expected_version: int,
        body: str | None,
        model_id: str | None,
        prompt_enabled: bool,
        origin: ConfigOrigin,
        note: str | None,
        caller_hash: str | None,
        scan_findings: list[ScanFinding],
    ) -> tuple[AgentConfigVersion, ResolvedAgentConfig]:
        """Write either field, or both, as one version.

        Omitting a field leaves it alone. That is what lets a model-only save
        skip round-tripping a 32000-character body and a prompt-only save skip
        restating the model, without either one being able to null the other by
        accident.
        """
        self.validate_model_allowed(model_id)

        row = await self._load_for_write(
            namespace_key=namespace_key,
            agent_name=agent_name,
            expected_version=expected_version,
        )
        creating = row is None
        if row is None:
            row = await self._create_row(
                namespace_key=namespace_key, agent_name=agent_name, caller_hash=caller_hash
            )

        if body is not None:
            row.body = body
            row.body_format = BodyFormat.TEXT.value
            row.prompt_enabled = prompt_enabled
        if model_id is not None:
            row.model_id = model_id
        row.updated_by_hash = caller_hash

        version = await self._append_version(
            row,
            event_type=ConfigEventType.CREATED if creating else ConfigEventType.UPDATED,
            origin=origin,
            note=note,
            changed_by_hash=caller_hash,
            scan_findings=scan_findings,
        )
        return version, self.resolve_row(row, agent_name=agent_name)

    async def clear_prompt(
        self,
        *,
        namespace_key: str,
        agent_name: str,
        expected_version: int,
        note: str | None,
        caller_hash: str | None,
    ) -> tuple[AgentConfigVersion | None, ResolvedAgentConfig]:
        """Stop using the managed prompt. Idempotent.

        Clearing is a state, not a row removal, and the version log outlives it.
        That is deliberate: history is what makes clearing recoverable, and the
        version rows' foreign key points at ``agents`` rather than at this table
        so nothing here can delete them.
        """
        row = await self._load_for_write(
            namespace_key=namespace_key,
            agent_name=agent_name,
            expected_version=expected_version,
        )
        if row is None or row.body is None:
            resolved = self.resolve_row(row, agent_name=agent_name)
            return None, resolved

        row.body = None
        row.prompt_enabled = False
        row.updated_by_hash = caller_hash
        version = await self._append_version(
            row,
            event_type=ConfigEventType.PROMPT_CLEARED,
            origin=ConfigOrigin.AUTHORED,
            note=note,
            changed_by_hash=caller_hash,
            scan_findings=[],
        )
        return version, self.resolve_row(row, agent_name=agent_name)

    async def clear_model(
        self,
        *,
        namespace_key: str,
        agent_name: str,
        expected_version: int,
        note: str | None,
        caller_hash: str | None,
    ) -> tuple[AgentConfigVersion | None, ResolvedAgentConfig]:
        """Stop using the managed model. Idempotent."""
        row = await self._load_for_write(
            namespace_key=namespace_key,
            agent_name=agent_name,
            expected_version=expected_version,
        )
        if row is None or row.model_id is None:
            return None, self.resolve_row(row, agent_name=agent_name)

        row.model_id = None
        row.updated_by_hash = caller_hash
        version = await self._append_version(
            row,
            event_type=ConfigEventType.MODEL_CLEARED,
            origin=ConfigOrigin.AUTHORED,
            note=note,
            changed_by_hash=caller_hash,
            scan_findings=[],
        )
        return version, self.resolve_row(row, agent_name=agent_name)

    async def set_prompt_enabled(
        self,
        *,
        namespace_key: str,
        agent_name: str,
        expected_version: int,
        prompt_enabled: bool,
        note: str | None,
        caller_hash: str | None,
    ) -> tuple[AgentConfigVersion, ResolvedAgentConfig]:
        """Toggle delivery without touching the body.

        Writes a version row even though no text changed, so the history
        explains a behaviour change that involved no edit.
        """
        row = await self._load_for_write(
            namespace_key=namespace_key,
            agent_name=agent_name,
            expected_version=expected_version,
        )
        if row is None:
            row = await self._create_row(
                namespace_key=namespace_key, agent_name=agent_name, caller_hash=caller_hash
            )

        row.prompt_enabled = prompt_enabled
        row.updated_by_hash = caller_hash
        version = await self._append_version(
            row,
            event_type=(
                ConfigEventType.ENABLED if prompt_enabled else ConfigEventType.DISABLED
            ),
            origin=ConfigOrigin.AUTHORED,
            note=note,
            changed_by_hash=caller_hash,
            scan_findings=[],
        )
        return version, self.resolve_row(row, agent_name=agent_name)

    async def restore_version(
        self,
        *,
        namespace_key: str,
        agent_name: str,
        version_num: int,
        expected_version: int,
        note: str | None,
        caller_hash: str | None,
        scan_findings: list[ScanFinding],
    ) -> tuple[AgentConfigVersion, ResolvedAgentConfig]:
        """Copy an old version forward as a new one.

        Two refusals before anything is written, and the restore never partially
        applies. A stored ``body_format`` the server no longer understands is a
        409 ``SCHEMA_INCOMPATIBLE``; a stored ``model_id`` that has left the
        allowlist is a 409 ``MODEL_NOT_ALLOWED`` naming the model. A restore that
        quietly dropped the model half would be a rewind nobody could see in the
        history.

        ``prompt_enabled`` is deliberately not restored. Re-enabling is a
        separate call, because a restore that quietly switched delivery back on
        would be a surprise.
        """
        source = await self.get_version_or_404(
            namespace_key=namespace_key, agent_name=agent_name, version_num=version_num
        )

        if source.body_format not in {fmt.value for fmt in BodyFormat}:
            raise ConflictError(
                error_code=ErrorCode.SCHEMA_INCOMPATIBLE,
                detail=(
                    f"Version {version_num} stores a body in format "
                    f"{source.body_format!r}, which this server does not "
                    "understand."
                ),
                resource="AgentConfigVersion",
                resource_id=f"{agent_name}:{version_num}",
                hint="Restore a version stored in a format this server supports.",
            )

        # Same validator as the set route. Per the closed-write-path invariant,
        # these are the only two paths to this column.
        self.validate_model_allowed(source.model_id, on_restore=True)

        row = await self._load_for_write(
            namespace_key=namespace_key,
            agent_name=agent_name,
            expected_version=expected_version,
        )
        if row is None:
            row = await self._create_row(
                namespace_key=namespace_key, agent_name=agent_name, caller_hash=caller_hash
            )

        row.body = source.body
        row.body_format = source.body_format
        row.model_id = source.model_id
        row.updated_by_hash = caller_hash

        version = await self._append_version(
            row,
            event_type=ConfigEventType.RESTORED,
            origin=ConfigOrigin.RESTORED,
            note=note,
            changed_by_hash=caller_hash,
            scan_findings=scan_findings,
        )
        return version, self.resolve_row(row, agent_name=agent_name)

    async def record_source_instruction(
        self, *, namespace_key: str, agent_name: str, source_instruction: str | None
    ) -> None:
        """Store what an agent process reports its own code declares.

        Unverified, and treated as such everywhere it surfaces. It arrives on
        the registration payload under an AUTHENTICATED operation, so it is not
        admin-authored text; it is never sent to a model by Agent Control and
        never pre-fills the editor. Writing it does not bump ``current_version``
        and does not append a version row, because the agent restarting is not
        an operator decision and would otherwise fill the history with noise.
        """
        row = await self.find_row(namespace_key=namespace_key, agent_name=agent_name)
        if row is None:
            row = await self._create_row(
                namespace_key=namespace_key, agent_name=agent_name, caller_hash=None
            )
        row.source_instruction = source_instruction
        row.source_reported_at = dt.datetime.now(dt.UTC)
        await self._db.flush()
