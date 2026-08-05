"""Main server application entry point."""

import inspect
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from agent_control_engine import discover_evaluators, list_evaluators
from agent_control_models import HealthResponse
from agent_control_telemetry import DEFAULT_CONTROL_EVENT_SINK_NAME, ControlEventSinkSelection
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette_exporter import PrometheusMiddleware, handle_metrics

from . import __version__ as server_version
from .auth import get_api_key_from_header
from .config import (
    auth_settings,
    check_agent_config_startup_requirements,
    check_executor_startup_requirements,
    executor_settings,
    observability_settings,
    settings,
)
from .db import AsyncSessionLocal, async_engine
from .endpoints.agent_attachments import router as agent_attachment_router
from .endpoints.agent_configs import model_router as agent_model_router
from .endpoints.agent_configs import router as agent_config_router
from .endpoints.agent_dispatch import router as agent_dispatch_router
from .endpoints.agent_halts import router as agent_halt_router
from .endpoints.agent_nudges import router as agent_nudge_router
from .endpoints.agent_plans import router as agent_plan_router
from .endpoints.agent_runtimes import router as agent_runtime_router
from .endpoints.agent_sessions import router as agent_session_router
from .endpoints.agent_tasks import router as agent_task_router
from .endpoints.agent_workflows import router as agent_workflow_router
from .endpoints.agents import router as agent_router
from .endpoints.auth import router as auth_router
from .endpoints.control_bindings import router as control_binding_router
from .endpoints.controls import router as control_router
from .endpoints.controls import template_router as control_template_router
from .endpoints.evaluation import router as evaluation_router
from .endpoints.evaluators import router as evaluator_router
from .endpoints.observability import router as observability_router
from .endpoints.policies import router as policy_router
from .endpoints.system import router as system_router
from .endpoints.teams import router as team_router
from .errors import (
    APIError,
    api_error_handler,
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from .logging_utils import (
    access_log_enabled,
    configure_logging,
    get_uvicorn_log_level_name,
    should_configure_logging,
)
from .middleware import AttachmentUploadBodyLimit
from .observability.ingest import DirectEventIngestor
from .observability.sinks import (
    EventStoreControlEventSink,
    ResolvedControlEventBackend,
    resolve_control_event_backend,
)
from .observability.store import PostgresEventStore
from .ui_assets import configure_ui_routes

logger = logging.getLogger(__name__)
_logging_configured = False

METRICS_PATH = "/metrics"
PROMETHEUS_BUCKETS = [
    0.1,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    30.0,
    60.0,
    float("inf"),
]
CORS_ALLOW_CREDENTIALS = True
"""Cookie-based browser auth needs credentialed cross-origin requests.

Named rather than inlined because the executor startup check has to reason
about it: credentialed requests plus a wildcard origin means any page in any tab
can drive this API as the logged-in operator, which is tolerable for a
configuration console and is not tolerable once the same session can spend model
quota.
"""

PROMETHEUS_SKIP_PATHS = [
    METRICS_PATH,
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
]


def _default_log_level() -> str:
    return "DEBUG" if settings.debug else "INFO"


def _configure_logging_once() -> None:
    global _logging_configured

    if _logging_configured or not should_configure_logging():
        return

    configure_logging(default_level=_default_log_level())
    _logging_configured = True


def add_prometheus_metrics(app: FastAPI, metrics_prefix: str) -> None:
    """Configure Prometheus metrics for the FastAPI app."""
    app.add_middleware(
        PrometheusMiddleware,
        app_name="agent-control-server",
        prefix=metrics_prefix,
        group_paths=True,
        filter_unhandled_paths=True,
        buckets=PROMETHEUS_BUCKETS,
        skip_paths=PROMETHEUS_SKIP_PATHS,
    )
    app.add_route(METRICS_PATH, handle_metrics)


async def _shutdown_observability_sink(sink: object) -> None:
    """Flush and close a custom async sink when it exposes lifecycle hooks."""
    flush = getattr(sink, "flush", None)
    if callable(flush):
        try:
            result = flush()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("Observability sink flush failed during shutdown", exc_info=True)

    for method_name in ("close", "shutdown"):
        callback = getattr(sink, method_name, None)
        if not callable(callback):
            continue
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "Observability sink %s failed during shutdown",
                method_name,
                exc_info=True,
            )
        return


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager for FastAPI app startup and shutdown."""
    _configure_logging_once()

    # Refuse to start an executor-enabled server that cannot protect the
    # endpoints the executor adds. Before chat, an unauthenticated caller on a
    # published port could tamper with configuration; after it, the same caller
    # can spend the operator's model quota and put text in front of a running
    # agent. This runs before anything else in startup so the failure is a
    # refusal to boot rather than a window of exposure.
    check_executor_startup_requirements(
        executor=executor_settings,
        auth=auth_settings,
        cors_origins=settings.get_cors_origins(),
        allow_credentials=CORS_ALLOW_CREDENTIALS,
    )

    # Decide whether saved agent configuration may reach a running agent.
    # Unlike the executor check this never refuses to boot: a gated config store
    # is still a working config store, and editing on a laptop with no
    # credentials configured is how everybody first meets this feature. What it
    # suppresses is the one thing that changes a running agent.
    check_agent_config_startup_requirements(auth=auth_settings)

    # One feature, two switches, and the half-on state is silent by design
    # downstream: a dispatched step on a Linear task simply writes no files
    # summary, so the agent is told nothing about files sitting on its issue.
    # Say it once here, where an operator reading "why are files not arriving"
    # will actually look. Not a refusal to boot - uploads-without-tracker-
    # ingress is a legitimate deployment - just no longer a silent one.
    from .config import linear_settings

    if executor_settings.attachments_enabled and not linear_settings.attachments_enabled:
        logger.warning(
            "Attachments are enabled but Linear file ingress is not: dispatched "
            "steps will not fetch files from their issues. Set "
            "AGENT_CONTROL_LINEAR_ATTACHMENTS_ENABLED=true if they should."
        )

    # Install the request-auth provider selected by environment variables.
    from .auth_framework.config import configure_auth_from_env

    configure_auth_from_env()

    # Discover evaluators at startup
    discover_evaluators()
    available = list(list_evaluators().keys())
    logger.info(f"Evaluator discovery complete. Available evaluators: {available}")

    # Initialize observability components (stored on app.state)
    if observability_settings.enabled:
        logger.info("Initializing observability components...")

        # 1. Resolve the active sink from shared sink-selection config
        sink_selection = ControlEventSinkSelection(
            name=observability_settings.sink_name,
            config=observability_settings.sink_config,
        )
        default_backend: ResolvedControlEventBackend | None = None
        if sink_selection.name == DEFAULT_CONTROL_EVENT_SINK_NAME:
            store = PostgresEventStore(AsyncSessionLocal)
            default_backend = ResolvedControlEventBackend(
                sink=EventStoreControlEventSink(store),
                event_store=store,
            )
            logger.info("PostgresEventStore initialized")

        backend = resolve_control_event_backend(
            sink_selection,
            default_backend=default_backend,
        )
        app.state.event_store = backend.event_store
        app.state.event_sink = backend.sink

        # 2. Create event ingestor
        ingestor = DirectEventIngestor(
            store=backend.sink,
            log_to_stdout=observability_settings.stdout,
        )
        app.state.event_ingestor = ingestor
        logger.info(
            "DirectEventIngestor initialized (stdout=%s, sink=%s)",
            observability_settings.stdout,
            sink_selection.name,
        )

        logger.info("Observability initialization complete")

    yield

    # Shutdown: release auth provider resources (e.g., upstream HTTP clients)
    from .auth_framework.config import teardown_auth

    await teardown_auth()

    # Shutdown: release the Linear HTTP clients, if any were ever built. Three
    # singletons, one per module that talks to Linear: milestones, issues, and
    # the write path.
    from .services.linear_issues import shutdown_milestone_issues_service
    from .services.linear_milestones import shutdown_milestone_service
    from .services.linear_writeback_runtime import shutdown_writeback_runtime

    await shutdown_milestone_service()
    await shutdown_milestone_issues_service()
    await shutdown_writeback_runtime()

    # Shutdown: release the executor connection pool, if a session was ever
    # opened. Built on first use, so a server that never chats never opens it.
    from .services.executor_factory import shutdown_executor_clients

    await shutdown_executor_clients()

    # Shutdown: let any background attachment conversion land, briefly. What it
    # would have written is a cache entry, so losing one costs a repeat rather
    # than data, and a process that waited out an OCR run would look hung.
    from .services.attachment_conversions import shutdown_attachment_conversions

    await shutdown_attachment_conversions()

    # Shutdown: Clean up observability
    if observability_settings.enabled and hasattr(app.state, "event_store"):
        logger.info("Shutting down observability components...")
        sink = getattr(app.state, "event_sink", None)
        if sink is not None:
            await _shutdown_observability_sink(sink)
        if sink is None or sink is not app.state.event_store:
            await app.state.event_store.close()
        logger.info("EventStore closed")

    await async_engine.dispose()
    logger.info("Database engine disposed")


app = FastAPI(
    title="Agent Control Server",
    description="""Server component for Agent Control - policy-based control for AI agents.

## Architecture

The system uses a simple hierarchical model:
- **Agents**: AI systems that need control
- **Policies**: Collections of controls assigned to agents
- **Controls**: Individual control configurations

## Hierarchy

```
Agent → Policy → Control(s)
```

## Quick Start

1. Register your agent with `/api/v1/agents/initAgent`
2. Create configured controls with `/api/v1/controls`
3. Create a policy and add controls to it
4. Assign the policy to your agent
5. Query agent controls with `/api/v1/agents/{agent_name}/controls`
   (use state filters on the same endpoint for disabled or unrendered associated controls)
    """,
    version=server_version,
    lifespan=lifespan,
)

add_prometheus_metrics(app, settings.prometheus_metrics_prefix)

# Bound the attachment upload body on the receive channel. Added before CORS so
# that CORS ends up outside it and decorates the 413: a browser that cannot read
# the refusal reports a network error instead of "your file is too big".
app.add_middleware(AttachmentUploadBodyLimit)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.get_allow_methods(),
    allow_headers=settings.get_allow_headers(),
)

@app.middleware("http")
async def attach_version_header(request, call_next):  # type: ignore[no-untyped-def]
    """Attach server version metadata to every response."""
    response = await call_next(request)
    response.headers["X-Agent-Control-Server-Version"] = server_version
    return response


# =============================================================================
# Exception Handlers (RFC 7807 / Kubernetes / GitHub-style)
# =============================================================================

# Register custom API error handler (highest priority for our errors)
app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]

# Register handler for FastAPI's RequestValidationError (Pydantic validation)
app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]

# Register handler for standard HTTPException (older routes, FastAPI internals)
app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]

# Register catch-all handler for unexpected exceptions
app.add_exception_handler(Exception, generic_exception_handler)

# API v1 prefix for all routes
api_v1_prefix = f"{settings.api_prefix}/{settings.api_version}"

# API routers. Routers migrated to the auth framework mount the
# non-validating header extractor only so OpenAPI advertises X-API-Key;
# each endpoint's ``require_operation`` dependency owns authn + authz.
app.include_router(
    agent_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)
app.include_router(
    policy_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)
app.include_router(
    # Endpoint dependencies handle auth; this advertises X-API-Key.
    control_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)
app.include_router(
    # The auth framework on each endpoint owns authentication and
    # authorization for control bindings, so this router is mounted
    # without the router-level auth gate. See ``auth_framework`` for
    # the provider contract. ``get_api_key_from_header`` is a non-
    # validating extractor (``auto_error=False``); it is attached purely
    # so the generated OpenAPI spec advertises the X-API-Key requirement
    # on these routes - without it, downstream SDK generators would treat
    # the routes as unauthenticated.
    control_binding_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# Auth endpoints (runtime token exchange). Same rationale as control
# bindings: framework on each endpoint owns authn + authz, and the
# header extractor is attached for OpenAPI advertisement only.
app.include_router(
    auth_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)
app.include_router(
    # Endpoint dependencies handle auth; this advertises X-API-Key.
    control_template_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)
app.include_router(
    evaluation_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    evaluator_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    observability_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    # Same rationale as control bindings: each endpoint's ``require_operation``
    # owns authn + authz, and the header extractor is attached so the generated
    # OpenAPI spec still advertises X-API-Key on these routes.
    team_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    # Executor bindings and chat sessions. Same rationale as control bindings:
    # each endpoint's ``require_operation`` owns authn + authz, and the header
    # extractor is attached so the generated OpenAPI spec advertises X-API-Key.
    agent_runtime_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    agent_session_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# Files attached to a session. Mounted next to the session router and after it,
# so the literal ``/attachments`` sub-paths are matched by this router rather
# than swallowed by anything the session router declares.
#
# Registered unconditionally and gated per route instead of at mount time. The
# plan asks for registration only when the executor is enabled; a mount-time
# condition reads the settings singleton once at import, which makes the feature
# untestable and would let a restart-time settings change silently remove routes
# a client is already calling. Every handler calls the same executor gate the
# session routes use, so a disabled deployment answers a written 503 rather than
# a 404, and the startup refusal that covers an enabled executor on an
# unauthenticated server covers this router with it.
app.include_router(
    agent_attachment_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    # Per-agent system prompt and model. Reads are AUTHENTICATED because the
    # agent process fetches its own config here on the refresh loop; writes are
    # ADMIN because the body lands in a field no control evaluates and the model
    # spends the operator's quota on every turn.
    agent_config_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    # The model allowlist. Deployment-wide, namespace-independent, and gated on
    # the write operation: at read tier it would be cross-tenant vendor
    # reconnaissance readable by any agent process key.
    agent_model_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# Nudges and halts hang off the session path. Their machine-side routes are
# authorized by a session-bound runtime token rather than by the header
# extractor mounted here, which is advertisement for OpenAPI only.
app.include_router(
    agent_nudge_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

app.include_router(
    agent_halt_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# The agent's own account of what it is doing. Reads sit with the transcript's
# operation; writes are the agent's, under the same session-bound runtime token
# the nudge and halt claims use.
app.include_router(
    agent_plan_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# The dispatch ledger. Rows only: no route here starts a turn, opens a session
# or contacts an executor, and the process that does those things is separate.
app.include_router(
    agent_task_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# The ordered list of agents a task is handed between. Separate from the ledger
# router because the authority is different again: reading a plan is what every
# dispatcher does, and writing one names the agents an autonomous chain runs.
app.include_router(
    agent_workflow_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# The ceilings on that ledger and the levels that stop it. Separate from the
# ledger router because the authority is different: a stop has to be usable by
# somebody who is not allowed to start anything.
app.include_router(
    agent_dispatch_router,
    prefix=api_v1_prefix,
    dependencies=[Depends(get_api_key_from_header)],
)

# System routes (config, login, logout) - no auth required
app.include_router(
    system_router,
    prefix=settings.api_prefix,
)


JSON_VALUE_SCHEMA_NAMES = (
    "JSONValue",
    "JSONValue-Input",
    "JSONValue-Output",
    "JsonValue",
    "JsonValue-Input",
    "JsonValue-Output",
)


# Override OpenAPI to avoid recursive JSONValue schema issues in TS generators.
def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    schemas = openapi_schema.get("components", {}).get("schemas", {})
    for schema_name in JSON_VALUE_SCHEMA_NAMES:
        if schema_name in schemas:
            schemas[schema_name] = {"description": "Any JSON value"}

    # This route is intentionally public metadata. FastAPI still emits inherited
    # API-key security for it, so patch only this operation in the generated spec.
    controls_schema_path = f"{api_v1_prefix}/controls/schema"
    controls_schema_operation = (
        openapi_schema.get("paths", {})
        .get(controls_schema_path, {})
        .get("get")
    )
    if isinstance(controls_schema_operation, dict):
        controls_schema_operation["security"] = []

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[assignment]


# Health check at root level (common convention)
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Health check",
    response_description="Server health status",
)
async def health_check() -> HealthResponse:
    """
    Check if the server is running and responsive.

    This endpoint does not check database connectivity.

    Returns:
        HealthResponse with status and version
    """
    return HealthResponse(status="healthy", version=server_version)


configure_ui_routes(app)


def run() -> None:
    """Run the server application."""
    _configure_logging_once()
    uvicorn_kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "log_level": get_uvicorn_log_level_name(_default_log_level()).lower(),
        "access_log": access_log_enabled(),
    }
    if not should_configure_logging():
        uvicorn_kwargs["log_config"] = None

    uvicorn.run(app, **uvicorn_kwargs)


if __name__ == "__main__":
    run()
