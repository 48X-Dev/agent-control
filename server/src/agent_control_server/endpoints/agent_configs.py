"""HTTP endpoints for an agent's system prompt and its model.

Two things a reader of these handlers needs to know before trusting them.

**A managed system prompt is invisible to every control in the deployment.** The
body lands verbatim in Google ADK's ``config.system_instruction``, and the SDK's
``extract_request_text`` reads ``llm_request.contents[-1].parts`` and nothing
else. So no guardrail evaluates anything written here, by construction. That is
correct for authored configuration - a system prompt belongs in the highest-trust
field - and it is exactly why the write operation is ADMIN. An operator who
assumes their controls cover this field will be wrong.

**Writes are ADMIN, and on a default-configured server that is not a boundary.**
``AGENT_CONTROL_API_KEY_ENABLED`` defaults false, which installs a provider that
authorizes every operation including ADMIN for anyone who can reach the port. So
storage is always open and *delivery* is gated at startup: on such a server both
sources resolve to ``"code"`` and ``delivery_state`` is
``blocked_insecure_auth``. Editing, versioning and the audit trail keep working,
because a laptop with no credentials is how everybody first meets this.

Routes hang off the agents prefix because a config has no identity apart from
its agent. Every handler resolves the agent first, so an unknown name is a 404
before any config logic runs, and every service call is scoped to the
principal's namespace.
"""

from __future__ import annotations

from agent_control_models import PaginationInfo
from agent_control_models.agent_configs import (
    AgentConfigVersionDetail,
    AgentConfigVersionSummary,
    BodyFormat,
    ClearAgentConfigFieldRequest,
    ClearAgentConfigFieldResponse,
    ConfigEventType,
    ConfigOrigin,
    GetAgentConfigResponse,
    GetAgentConfigVersionResponse,
    ListAgentConfigVersionsResponse,
    ListAgentModelsResponse,
    ModelProvider,
    RestoreAgentConfigVersionRequest,
    ScanFinding,
    SetAgentConfigRequest,
    SetAgentConfigResponse,
    SetPromptEnabledRequest,
)
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..config import model_settings
from ..db import get_async_db
from ..models import AgentConfig as AgentConfigRow
from ..models import AgentConfigVersion as AgentConfigVersionRow
from ..services.agent_config_scan import scan_prompt_body
from ..services.agent_configs import (
    AgentConfigService,
    ResolvedAgentConfig,
    require_registered_agent,
)
from ..services.agent_names import normalize_agent_name_or_422
from ..services.caller_identity import hash_caller_id

router = APIRouter(prefix="/agents", tags=["agent-configs"])

# Separate router: the allowlist is deployment-wide and namespace-independent,
# so it does not belong under an agent path.
model_router = APIRouter(prefix="/agent-models", tags=["agent-configs"])

_DEFAULT_PAGINATION_LIMIT = 50
_MAX_PAGINATION_LIMIT = 200


def _to_config_response(resolved: ResolvedAgentConfig) -> GetAgentConfigResponse:
    row = resolved.row
    entry = resolved.model_entry
    if row is None:
        return GetAgentConfigResponse(
            agent_name=resolved.agent_name,
            prompt_source=resolved.prompt_source,
            model_source=resolved.model_source,
            delivery_state=resolved.delivery_state,
            current_version=0,
        )
    return GetAgentConfigResponse(
        agent_name=resolved.agent_name,
        body=row.body,
        body_format=BodyFormat(row.body_format),
        prompt_enabled=row.prompt_enabled,
        prompt_source=resolved.prompt_source,
        model_id=row.model_id,
        # Null whenever the stored id is no longer offered. The SDK refuses to
        # construct anything without it rather than inferring a provider from
        # the id string, which is the exfiltration path this whole design is
        # arranged to avoid.
        model_provider=ModelProvider(entry.provider) if entry is not None else None,
        model_allowed=resolved.model_allowed,
        model_cost_tier=entry.cost_tier if entry is not None else None,
        model_source=resolved.model_source,
        delivery_state=resolved.delivery_state,
        etag=row.etag,
        current_version=row.current_version,
        source_instruction=row.source_instruction,
        source_reported_at=row.source_reported_at,
        updated_by_hash=row.updated_by_hash,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_version_summary(version: AgentConfigVersionRow) -> AgentConfigVersionSummary:
    return AgentConfigVersionSummary(
        version_num=version.version_num,
        event_type=ConfigEventType(version.event_type),
        origin=ConfigOrigin(version.origin),
        model_id=version.model_id,
        note=version.note,
        has_body=version.body is not None,
        scan_findings=[ScanFinding.model_validate(f) for f in version.scan_findings],
        changed_by_hash=version.changed_by_hash,
        created_at=version.created_at,
    )


def _to_version_detail(version: AgentConfigVersionRow) -> AgentConfigVersionDetail:
    summary = _to_version_summary(version)
    return AgentConfigVersionDetail(
        **summary.model_dump(),
        body=version.body,
        body_format=BodyFormat(version.body_format),
        etag=version.etag,
    )


def _clear_response(
    version: AgentConfigVersionRow | None, resolved: ResolvedAgentConfig
) -> ClearAgentConfigFieldResponse:
    row: AgentConfigRow | None = resolved.row
    return ClearAgentConfigFieldResponse(
        cleared=version is not None,
        version_num=version.version_num if version is not None else None,
        current_version=resolved.current_version,
        etag=row.etag if row is not None else None,
        prompt_source=resolved.prompt_source,
        model_source=resolved.model_source,
        delivery_state=resolved.delivery_state,
    )


@router.get(
    "/{agent_name}/config",
    response_model=GetAgentConfigResponse,
    summary="Read an agent's system prompt and model",
    response_description="The current configuration, resolved against server state",
)
async def get_agent_config(
    agent_name: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_READ)),
) -> GetAgentConfigResponse:
    """Return what this agent is configured to run, and what will reach it.

    This is also the delivery channel: the agent process fetches its own config
    here on the refresh loop under an ordinary agent key, which is why the read
    is AUTHENTICATED rather than ADMIN. Making it ADMIN would put an admin key
    in every agent process.

    The exposure that accepts, stated rather than glossed: every key in a
    namespace can read every other agent's prompt and its full version history,
    and because clearing preserves history, that outlives the decision to remove
    a prompt.

    ``prompt_source`` and ``model_source`` are resolved here, once, against the
    current allowlist and the current delivery gate. Clients do not re-derive
    them. ``model_allowed`` is recomputed on every read and never written back,
    which is what lets a model leave the allowlist without rewriting stored rows.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    await require_registered_agent(
        db, namespace_key=principal.namespace_key, agent_name=normalized
    )
    resolved = await AgentConfigService(db).resolve(
        namespace_key=principal.namespace_key, agent_name=normalized
    )
    return _to_config_response(resolved)


@router.put(
    "/{agent_name}/config",
    response_model=SetAgentConfigResponse,
    summary="Save an agent's system prompt and/or model",
    response_description="The new version, plus any advisory scan findings",
)
async def set_agent_config(
    agent_name: str,
    request: SetAgentConfigRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_WRITE)),
) -> SetAgentConfigResponse:
    """Write either field, or both, as one version.

    Replace semantics against the agent's code: when a body is set, it is what
    the operator owns, and clearing restores whatever the code declares. A field
    left out of the request is left alone rather than nulled.

    ADMIN, on two independent grounds. The body lands in a field no control
    reads, so a lower-privileged write here would override ADMIN-authored
    control policy with text no guardrail evaluates. And the model spends the
    operator's quota on every turn of every session, indefinitely.

    ``expected_version`` is required and compared under a row lock. One row
    carries both fields, so a prompt edit and a model edit conflict with each
    other - which is correct, they are one version. Last-write-wins would
    destroy a colleague's paragraph with no signal until somebody read the
    history days later.

    The save-time scan runs on the body and records what it finds on the version
    row and in this response. It never rejects.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    findings = scan_prompt_body(request.body)
    service = AgentConfigService(db)
    version, resolved = await service.set_config(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        expected_version=request.expected_version,
        body=request.body,
        model_id=request.model_id,
        prompt_enabled=request.prompt_enabled,
        origin=ConfigOrigin(request.origin),
        note=request.note,
        caller_hash=hash_caller_id(principal.caller_id),
        scan_findings=findings,
    )
    await db.commit()
    return SetAgentConfigResponse(
        version_num=version.version_num,
        current_version=resolved.current_version,
        etag=resolved.row.etag if resolved.row is not None else None,
        prompt_source=resolved.prompt_source,
        model_source=resolved.model_source,
        delivery_state=resolved.delivery_state,
        scan_findings=findings,
    )


@router.post(
    "/{agent_name}/config:clear-prompt",
    response_model=ClearAgentConfigFieldResponse,
    summary="Stop using the managed system prompt (idempotent)",
    response_description="Whether a prompt was there to clear",
)
async def clear_agent_config_prompt(
    agent_name: str,
    request: ClearAgentConfigFieldRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_WRITE)),
) -> ClearAgentConfigFieldResponse:
    """Null the body and switch delivery off. The agent falls back to its code.

    ``POST`` with a verb suffix rather than ``DELETE`` because the call needs a
    body for ``expected_version``, and bodies on ``DELETE`` get dropped by some
    proxies and clients. The endpoint would fail closed with a 422 rather than
    clearing without the concurrency check, so this is about producing an
    attributable failure rather than closing a hole.

    Clearing is a state, not a row removal. The version history survives it,
    which is the point of having history, and the version rows' foreign key
    points at ``agents`` so nothing here can take them with it.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    version, resolved = await AgentConfigService(db).clear_prompt(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        expected_version=request.expected_version,
        note=request.note,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()
    return _clear_response(version, resolved)


@router.post(
    "/{agent_name}/config:clear-model",
    response_model=ClearAgentConfigFieldResponse,
    summary="Stop using the managed model (idempotent)",
    response_description="Whether a model was there to clear",
)
async def clear_agent_config_model(
    agent_name: str,
    request: ClearAgentConfigFieldRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_WRITE)),
) -> ClearAgentConfigFieldResponse:
    """Null the model id. The agent goes back to what its own code declares.

    Two verb routes rather than one taking a field list, because the version
    row's ``event_type`` has to name what happened and a list makes it
    ambiguous: "cleared" against a row whose prompt is intact is a history entry
    nobody can read.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    version, resolved = await AgentConfigService(db).clear_model(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        expected_version=request.expected_version,
        note=request.note,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()
    return _clear_response(version, resolved)


@router.patch(
    "/{agent_name}/config",
    response_model=SetAgentConfigResponse,
    summary="Switch managed-prompt delivery on or off",
    response_description="The new version",
)
async def set_agent_config_prompt_enabled(
    agent_name: str,
    request: SetPromptEnabledRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_WRITE)),
) -> SetAgentConfigResponse:
    """Toggle delivery while preserving the body.

    A prompt body is expensive to retype, so a toggle that keeps it earns its
    column. There is no model equivalent: clearing the id is the same thing, and
    a second boolean would only ever mean "the dropdown says X, ignore it".

    Writes a version row even though no text changed, so the history explains a
    behaviour change that involved no edit.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    version, resolved = await AgentConfigService(db).set_prompt_enabled(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        expected_version=request.expected_version,
        prompt_enabled=request.prompt_enabled,
        note=request.note,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()
    return SetAgentConfigResponse(
        version_num=version.version_num,
        current_version=resolved.current_version,
        etag=resolved.row.etag if resolved.row is not None else None,
        prompt_source=resolved.prompt_source,
        model_source=resolved.model_source,
        delivery_state=resolved.delivery_state,
    )


@router.get(
    "/{agent_name}/config/versions",
    response_model=ListAgentConfigVersionsResponse,
    summary="List an agent config's version history",
    response_description="Paginated version summaries, newest first",
)
async def list_agent_config_versions(
    agent_name: str,
    cursor: int | None = Query(
        None, description="Version number to start after (newest-first pagination)"
    ),
    limit: int = Query(_DEFAULT_PAGINATION_LIMIT, ge=1, le=_MAX_PAGINATION_LIMIT),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_READ)),
) -> ListAgentConfigVersionsResponse:
    """Newest-first history. Summaries omit the body but keep ``model_id``.

    Same operation and same tier as reading the current config: anyone who can
    see what an agent runs today can see what it ran before.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    await require_registered_agent(
        db, namespace_key=principal.namespace_key, agent_name=normalized
    )
    page = await AgentConfigService(db).list_versions(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        cursor=cursor,
        limit=limit,
    )
    return ListAgentConfigVersionsResponse(
        versions=[_to_version_summary(v) for v in page.versions],
        pagination=PaginationInfo(
            limit=limit,
            total=page.total,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        ),
    )


@router.get(
    "/{agent_name}/config/versions/{version_num}",
    response_model=GetAgentConfigVersionResponse,
    summary="Read one version of an agent config",
    response_description="The full stored version, including its body",
)
async def get_agent_config_version(
    agent_name: str,
    version_num: int,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_READ)),
) -> GetAgentConfigVersionResponse:
    """Return one history row in full, so a client can diff it locally.

    Server-side diffing is deliberately absent: the bodies are small, the client
    already holds both sides, and an API that returns diffs has to pick an
    algorithm and keep it stable forever.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    await require_registered_agent(
        db, namespace_key=principal.namespace_key, agent_name=normalized
    )
    version = await AgentConfigService(db).get_version_or_404(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        version_num=version_num,
    )
    return GetAgentConfigVersionResponse(version=_to_version_detail(version))


@router.post(
    "/{agent_name}/config/versions/{version_num}:restore",
    response_model=SetAgentConfigResponse,
    summary="Restore an earlier version as a new one",
    response_description="The new version created by the restore",
)
async def restore_agent_config_version(
    agent_name: str,
    version_num: int,
    request: RestoreAgentConfigVersionRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_WRITE)),
) -> SetAgentConfigResponse:
    """Copy an old version's fields forward. Version numbers never rewind.

    Two refusals before anything is written, and the restore never partially
    applies: a stored ``body_format`` this server does not understand is a 409
    ``SCHEMA_INCOMPATIBLE``, and a stored ``model_id`` that has left the
    allowlist is a 409 ``MODEL_NOT_ALLOWED`` naming the model. A restore that
    quietly dropped the model half would be a rewind nobody could see in the
    history; the explicit alternative is an ordinary save carrying the old body
    and the current model id.

    ``prompt_enabled`` is not restored. Re-enabling is a separate call.
    """
    normalized = normalize_agent_name_or_422(agent_name)
    service = AgentConfigService(db)
    source = await service.get_version_or_404(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        version_num=version_num,
    )
    findings = scan_prompt_body(source.body)
    version, resolved = await service.restore_version(
        namespace_key=principal.namespace_key,
        agent_name=normalized,
        version_num=version_num,
        expected_version=request.expected_version,
        note=request.note,
        caller_hash=hash_caller_id(principal.caller_id),
        scan_findings=findings,
    )
    await db.commit()
    return SetAgentConfigResponse(
        version_num=version.version_num,
        current_version=resolved.current_version,
        etag=resolved.row.etag if resolved.row is not None else None,
        prompt_source=resolved.prompt_source,
        model_source=resolved.model_source,
        delivery_state=resolved.delivery_state,
        scan_findings=findings,
    )


@model_router.get(
    "",
    response_model=ListAgentModelsResponse,
    summary="List the models this server offers",
    response_description="The deployment's model allowlist",
)
async def list_agent_models(
    principal: Principal = Depends(require_operation(Operation.AGENT_CONFIGS_WRITE)),
) -> ListAgentModelsResponse:
    """Return the server's model allowlist.

    Takes the **write** operation, not the read one, and that is deliberate. It
    enumerates the deployment's whole vendor and cost-tier inventory, is
    deployment-wide and namespace-independent, and exists solely to populate an
    admin picker. At AUTHENTICATED one compromised agent process key in any
    namespace would read cross-tenant reconnaissance about which vendors the
    operator has relationships with. A read-only viewer still sees their own
    agent's model, provider and cost tier - those come back on the per-agent
    config response at read tier. They just cannot enumerate the rest.

    Reads server configuration, touches no database, takes no namespace filter.
    """
    del principal  # Authorization is the whole point of the dependency here.
    return ListAgentModelsResponse(models=list(model_settings.allowlist))
