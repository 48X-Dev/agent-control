"""HTTP endpoints for binding agents to the executors that run them.

An executor binding is deployment configuration, in the same class as a control
binding: it names a host this server will send requests to. So writes are admin,
and reads are not - a caller who can list sessions needs to know which agents
can hold one at all.

The ``base_url`` on a binding is the one field here that turns into an outbound
request. It is validated at the model boundary rather than at the call site, so
a malformed one is a 422 on the request that wrote it instead of a surprise on
the first session someone opens.
"""

from __future__ import annotations

from agent_control_models.agent_runtimes import (
    AgentRuntimeResponse,
    DeleteAgentRuntimeResponse,
    ExecutorKind,
    ListAgentRuntimesResponse,
    UpsertAgentRuntimeRequest,
)
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..db import get_async_db
from ..models import AgentRuntime as AgentRuntimeRow
from ..services.agent_names import normalize_agent_name_or_422
from ..services.agent_runtimes import AgentRuntimesService

router = APIRouter(prefix="/agent-runtimes", tags=["agent-runtimes"])


def _to_response(
    binding: AgentRuntimeRow, *, created: bool | None = None
) -> AgentRuntimeResponse:
    return AgentRuntimeResponse(
        namespace_key=binding.namespace_key,
        agent_name=binding.agent_name,
        executor_kind=ExecutorKind(binding.executor_kind),
        base_url=binding.base_url,
        executor_app_name=binding.executor_app_name,
        enabled=binding.enabled,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
        created=created,
    )


@router.get(
    "",
    response_model=ListAgentRuntimesResponse,
    summary="List executor bindings",
    response_description="Bindings in the request namespace",
)
async def list_agent_runtimes(
    agent: str | None = Query(
        None,
        description=(
            "Optional agent name. Returns just that agent's binding, or an "
            "empty list when it has none."
        ),
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_READ)),
) -> ListAgentRuntimesResponse:
    """Return the executor bindings in this namespace, ordered by agent name.

    Unpaginated: an agent has at most one binding, so this list is as long as
    the namespace has chat-capable agents.
    """
    agent_name = normalize_agent_name_or_422(agent, field_name="agent") if agent else None
    service = AgentRuntimesService(db)
    bindings = await service.list_runtimes(
        namespace_key=principal.namespace_key, agent_name=agent_name
    )
    return ListAgentRuntimesResponse(
        runtimes=[_to_response(binding) for binding in bindings]
    )


@router.put(
    "/{agent_name}",
    response_model=AgentRuntimeResponse,
    summary="Bind an agent to an executor (idempotent)",
    response_description="The resulting binding",
)
async def upsert_agent_runtime(
    agent_name: str,
    request: UpsertAgentRuntimeRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_RUNTIMES_WRITE)),
) -> AgentRuntimeResponse:
    """Point an agent at the process that runs it.

    Replace semantics: every field is overwritten, so moving an executor is the
    same call with a new ``base_url``. The agent must already be registered;
    binding an unknown one is a 404.

    One agent, one executor. That is the topology the Python SDK enforces - it
    holds a single agent per process and the ADK plugin refuses to initialize
    under a second name - so a team of five agents is five executor processes,
    each with its own binding.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    service = AgentRuntimesService(db)
    binding, created = await service.upsert_binding(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        base_url=request.base_url,
        executor_app_name=request.executor_app_name,
        # Already a plain str: the shared BaseModel sets use_enum_values=True, so a
        # supplied value validates to str while an omitted one keeps the enum
        # default. Calling .value here worked only on the omitted path.
        executor_kind=str(request.executor_kind),
        enabled=request.enabled,
    )
    await db.commit()
    await db.refresh(binding)
    return _to_response(binding, created=created)


@router.delete(
    "/{agent_name}",
    response_model=DeleteAgentRuntimeResponse,
    summary="Remove an agent's executor binding (idempotent)",
    response_description="Whether a binding was removed",
)
async def delete_agent_runtime(
    agent_name: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_RUNTIMES_WRITE)),
) -> DeleteAgentRuntimeResponse:
    """Unbind an agent from its executor.

    Existing sessions keep their own copy of the executor coordinates, so this
    stops new sessions rather than ending current ones. It does leave those
    sessions unreadable and undeletable until the agent is bound again, because
    the binding is where the base URL lives; unbinding an agent with live
    sessions is a drain step, not a cleanup step.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    service = AgentRuntimesService(db)
    deleted = await service.delete_binding(
        namespace_key=principal.namespace_key, agent_name=normalized
    )
    await db.commit()
    return DeleteAgentRuntimeResponse(deleted=deleted)
