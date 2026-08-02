"""Persistence for agent-to-executor bindings.

A binding answers one question: which process serves this agent. Without an
answer, opening a session is not "slow" or "degraded", it is undefined, so this
module owns the two refusals that come before any executor is contacted.

Their order matters and is not arbitrary. An agent that was never registered is
a 404: the caller named something that does not exist. An agent that exists but
has no enabled binding is a 409: the caller named something real that this
deployment has not been configured to run. Answering 409 for an unregistered
agent would send an operator hunting for a missing binding for an agent that
was never there; answering 404 for a missing binding would suggest the agent
itself is gone.

Every method takes ``namespace_key`` and filters on it.
"""

from __future__ import annotations

from typing import cast

from agent_control_models.errors import ErrorCode
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import ConflictError, NotFoundError
from ..models import Agent, AgentRuntime


async def require_registered_agent(
    db: AsyncSession, *, namespace_key: str, agent_name: str
) -> None:
    """Raise 404 unless the agent is registered in this namespace.

    Sessions carry no foreign key to ``agents`` - a conversation should outlive
    a re-registration, and the executor's copy of it certainly does - so the
    existence check lives here rather than in the schema.
    """
    stmt = select(Agent.name).where(
        Agent.namespace_key == namespace_key,
        Agent.name == agent_name,
    )
    result = await db.execute(stmt)
    if result.first() is None:
        raise NotFoundError(
            error_code=ErrorCode.AGENT_NOT_FOUND,
            detail=f"Agent '{agent_name}' not found",
            resource="Agent",
            resource_id=agent_name,
            hint=(
                "Register the agent before binding it to an executor, and "
                "verify it belongs to this namespace."
            ),
        )


class AgentRuntimesService:
    """Reads and writes the ``agent_runtimes`` table."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_runtimes(
        self, *, namespace_key: str, agent_name: str | None = None
    ) -> list[AgentRuntime]:
        """Return bindings in this namespace, ordered by agent name.

        Unpaginated: there is at most one binding per agent, and a namespace
        with more agents than fit in one response has a different problem.
        """
        stmt = select(AgentRuntime).where(AgentRuntime.namespace_key == namespace_key)
        if agent_name is not None:
            stmt = stmt.where(AgentRuntime.agent_name == agent_name)
        stmt = stmt.order_by(AgentRuntime.agent_name.asc())
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def find_binding(
        self, *, namespace_key: str, agent_name: str
    ) -> AgentRuntime | None:
        """Return the binding for one agent, enabled or not."""
        stmt = select(AgentRuntime).where(
            AgentRuntime.namespace_key == namespace_key,
            AgentRuntime.agent_name == agent_name,
        )
        result = await self._db.execute(stmt)
        return cast(AgentRuntime | None, result.scalars().first())

    async def require_enabled_binding(
        self, *, namespace_key: str, agent_name: str
    ) -> AgentRuntime:
        """Return the enabled binding for an agent, or explain what is missing.

        Runs the registration check first so an unknown agent never reads as a
        configuration gap. A disabled binding gets its own sentence, because
        "drained on purpose" and "never configured" call for different actions
        from whoever reads the message.
        """
        await require_registered_agent(
            self._db, namespace_key=namespace_key, agent_name=agent_name
        )
        binding = await self.find_binding(
            namespace_key=namespace_key, agent_name=agent_name
        )
        if binding is None:
            raise ConflictError(
                error_code=ErrorCode.AGENT_RUNTIME_NOT_BOUND,
                detail=(
                    f"Agent '{agent_name}' is not bound to an executor, so it "
                    f"has no process to hold a conversation."
                ),
                resource="AgentRuntime",
                resource_id=agent_name,
                hint=(
                    "Bind the agent with PUT /agent-runtimes/{agent_name} "
                    "before opening a session."
                ),
            )
        if not binding.enabled:
            raise ConflictError(
                error_code=ErrorCode.AGENT_RUNTIME_NOT_BOUND,
                detail=(
                    f"The executor binding for agent '{agent_name}' is disabled "
                    f"and is not accepting new sessions."
                ),
                resource="AgentRuntime",
                resource_id=agent_name,
                hint=(
                    "Re-enable the binding with PUT /agent-runtimes/{agent_name}, "
                    "or point the agent at a different executor."
                ),
            )
        return binding

    async def upsert_binding(
        self,
        *,
        namespace_key: str,
        agent_name: str,
        base_url: str,
        executor_app_name: str,
        executor_kind: str,
        enabled: bool,
    ) -> tuple[AgentRuntime, bool]:
        """Create or replace one agent's binding. Returns ``(binding, created)``.

        Replace semantics: every field is overwritten, so re-running the same
        call after an executor moves is the whole migration.
        """
        await require_registered_agent(
            self._db, namespace_key=namespace_key, agent_name=agent_name
        )
        existing = await self.find_binding(
            namespace_key=namespace_key, agent_name=agent_name
        )
        if existing is not None:
            existing.base_url = base_url
            existing.executor_app_name = executor_app_name
            existing.executor_kind = executor_kind
            existing.enabled = enabled
            await self._db.flush()
            return existing, False

        binding = AgentRuntime(
            namespace_key=namespace_key,
            agent_name=agent_name,
            base_url=base_url,
            executor_app_name=executor_app_name,
            executor_kind=executor_kind,
            enabled=enabled,
        )
        # Savepoint rationale matches ``TeamsService.upsert_team``: the loser of
        # a concurrent insert on the same key rolls back only its own statement
        # and re-reads the winner, instead of poisoning the transaction.
        try:
            async with self._db.begin_nested():
                self._db.add(binding)
                await self._db.flush()
            return binding, True
        except IntegrityError:
            existing = await self.find_binding(
                namespace_key=namespace_key, agent_name=agent_name
            )
            if existing is None:
                raise
            existing.base_url = base_url
            existing.executor_app_name = executor_app_name
            existing.executor_kind = executor_kind
            existing.enabled = enabled
            await self._db.flush()
            return existing, False

    async def delete_binding(self, *, namespace_key: str, agent_name: str) -> bool:
        """Remove a binding. Returns whether one was there.

        Idempotent, and deliberately silent about existing sessions: they keep
        their own copy of the executor coordinates, so unbinding stops new
        sessions rather than orphaning current ones.
        """
        binding = await self.find_binding(
            namespace_key=namespace_key, agent_name=agent_name
        )
        if binding is None:
            return False
        await self._db.delete(binding)
        await self._db.flush()
        return True
