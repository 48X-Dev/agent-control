"""Generic primitives for the pluggable request-auth framework.

This module is intentionally provider-neutral: no upstream-specific
nouns, no transport assumptions, no policy engine references.
Providers wire those concerns in
:mod:`agent_control_server.auth_framework.providers`.

Concepts:

- :class:`Operation` is the vocabulary endpoints declare. Adding a new
  endpoint that needs a new permission means adding a member here, not
  changing every provider.
- :class:`Principal` is the resolved-identity result of authorization:
  the namespace the request runs in plus optional caller metadata.
- :class:`RequestAuthorizer` is the seam. Implementations decide whether
  a request may perform an operation; failure raises an HTTP-typed
  error and short-circuits the request.
- :func:`require_operation` is the FastAPI dependency factory endpoints
  attach to. It looks up the active authorizer, builds an optional
  per-request context, and returns the :class:`Principal`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from fastapi import Request


class Operation(StrEnum):
    """Authorization vocabulary spoken on the endpoint side.

    Members are stable wire identifiers; providers map them to whatever
    permission system a deployment uses.
    """

    # Control bindings (target attachments).
    CONTROL_BINDINGS_READ = "control_bindings.read"
    CONTROL_BINDINGS_WRITE = "control_bindings.write"

    # Runtime token exchange - wired on the exchange endpoint.
    RUNTIME_TOKEN_EXCHANGE = "runtime.token_exchange"

    CONTROLS_READ = "controls.read"
    CONTROLS_CREATE = "controls.create"
    CONTROLS_UPDATE = "controls.update"
    CONTROLS_DELETE = "controls.delete"
    POLICIES_READ = "policies.read"
    POLICIES_CREATE = "policies.create"
    POLICIES_UPDATE = "policies.update"
    AGENTS_READ = "agents.read"
    AGENTS_CREATE = "agents.create"
    AGENTS_UPDATE = "agents.update"
    EVALUATORS_READ = "evaluators.read"
    TEAMS_READ = "teams.read"
    TEAMS_WRITE = "teams.write"
    OBSERVABILITY_READ = "observability.read"
    OBSERVABILITY_WRITE = "observability.write"
    RUNTIME_USE = "runtime.use"

    # Chat sessions with an agent. Session metadata and message content are
    # split because they are different sensitivity classes: a title and a
    # timestamp are configuration-shaped, while a transcript carries raw human
    # prompts, model output, and tool results that can hold third-party data
    # pulled with a server-held key.
    AGENT_SESSIONS_READ = "agent_sessions.read"
    AGENT_SESSION_CONTENT_READ = "agent_sessions.content_read"
    AGENT_SESSIONS_WRITE = "agent_sessions.write"
    # Split from write because running a turn spends money and calls a model.
    # Splitting later would be a wire-contract change; splitting now is a line.
    AGENT_SESSIONS_RUN = "agent_sessions.run"
    AGENT_NUDGES_WRITE = "agent_nudges.write"
    # Stopping a turn sits at the same tier as starting one. Run at
    # AUTHENTICATED and stop at ADMIN is the one combination that cannot be
    # defended: whoever can start a turn that spends money must be able to stop
    # it. The scoping that keeps this from being a denial-of-service primitive
    # is creator scoping in the service, not the tier.
    AGENT_HALTS_WRITE = "agent_halts.write"
    # Uploading a file is per-caller working state on the caller's own session,
    # the same class as starting a turn, and it is scoped in the service by the
    # same predicate that gates a turn because it is the same act: it puts
    # caller-chosen bytes into somebody's conversation and in front of a model.
    #
    # One member, not three. Reading an attachment's name and downloading its
    # bytes is the same sensitivity class as reading the transcript it appears
    # in, and ``agent_sessions.content_read`` already exists at that tier for
    # exactly that. Minting a second read operation beside it would document a
    # boundary that does not exist.
    AGENT_ATTACHMENTS_WRITE = "agent_attachments.write"
    # Binding an agent to an executor URL is deployment configuration.
    AGENT_RUNTIMES_WRITE = "agent_runtimes.write"

    # One row carries the agent's system prompt and its model. The prompt lands
    # verbatim in ADK's ``config.system_instruction``, which
    # ``extract_request_text`` never reads, so nothing written there is
    # evaluated by any control in the deployment. The model decides which vendor
    # is called and whose quota is spent on every turn. Reads are
    # configuration-shaped; writes are not.
    AGENT_CONFIGS_READ = "agent_configs.read"
    AGENT_CONFIGS_WRITE = "agent_configs.write"

    # The dispatch ledger. Four operations rather than two, because the three
    # things a caller can do to a task are different authorities that happen to
    # share a table: importing work, taking responsibility for a row and
    # writing what an agent produced, and accepting an agent's claim to have
    # finished so it changes a tracker a human team plans against.
    #
    # ``agent_tasks.approve`` has no route in the ledger phase. It is declared
    # here anyway because it is the one operation whose *separation* is the
    # design: the accept path compares the approver's caller hash against the
    # claiming dispatcher's, and naming the operation now is what stops it
    # being folded into ``write`` by whoever writes that route.
    AGENT_TASKS_READ = "agent_tasks.read"
    AGENT_TASKS_WRITE = "agent_tasks.write"
    AGENT_TASKS_CLAIM = "agent_tasks.claim"
    AGENT_TASKS_APPROVE = "agent_tasks.approve"
    # The ordered list of agents a task is handed between. Split from the
    # ledger's four because a workflow is *configuration*, not work: it names
    # agents and it shapes the prompt each one receives, which is the authority
    # that decides what an autonomous chain can reach. Reading one is
    # configuration-shaped; writing one is not, and the tiers below say so.
    AGENT_WORKFLOWS_READ = "agent_workflows.read"
    AGENT_WORKFLOWS_WRITE = "agent_workflows.write"
    # Stopping the fleet. Separate from ``agent_tasks.write`` because a stop
    # has to be usable by somebody who is not allowed to start anything, and
    # because the thing being paused is the namespace rather than a task.
    AGENT_DISPATCH_PAUSE = "agent_dispatch.pause"
    # Binding a stop to every turn running in a namespace at once. Separate
    # from ``agent_halts.write``, which stops one session a caller can already
    # see, because this one reaches every session in the namespace including
    # chats belonging to other people. One operation per blast radius.
    AGENT_HALTS_WRITE_ALL = "agent_halts.write_all"

    # Machine-side operations. These are performed by the executor, not by a
    # human, and are authorized by a runtime token bound to one session rather
    # than by an API key that would be valid for every session in the
    # namespace. See ``auth_framework.config`` for the wiring.
    AGENT_NUDGES_CONSUME = "agent_nudges.consume"
    AGENT_PLANS_WRITE = "agent_plans.write"
    # Reading the company-knowledge mirror. Machine-side like the two above:
    # an agent asks through a session-bound runtime token, never with a key
    # that would be valid for every session in the namespace.
    COMPANY_KNOWLEDGE_SEARCH = "company_knowledge.search"

    # The oversight half, and the only one a human uses. Separate from the
    # operation above rather than folded into it, because a console reaching
    # the corpus under ``company_knowledge.search`` would be a browser holding
    # the machine-side credential and every ceiling attached to it. It carries
    # the console's Knowledge panel today; the per-source status endpoint the
    # name suggests is a later phase and joins this same operation.
    COMPANY_KNOWLEDGE_STATUS = "company_knowledge.status"


@dataclass(frozen=True)
class Principal:
    """Resolved identity for an authorized request.

    Attributes:
        namespace_key: The namespace the request runs in. Endpoints use
            this to scope every read and write.
        is_admin: Whether the caller has admin privileges in the
            current namespace.
        caller_id: Opaque, provider-supplied identifier for the caller
            (e.g., a key fingerprint or user id). Useful for audit
            logging; never echo back to clients.
        target_type: Set when the authorization grants access to a
            specific target. Endpoints can use it to verify the request
            target matches.
        target_id: Companion to ``target_type``; opaque identifier of
            the bound target.
        scopes: Granted capabilities (e.g., ``("runtime.use",)``).
            Populated by providers that surface a normalized grant.
        grant_expires_at: When the upstream grant expires. Used by the
            runtime-token exchange endpoint to bound the local token's
            lifetime.
    """

    namespace_key: str
    is_admin: bool = False
    caller_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    scopes: tuple[str, ...] = ()
    grant_expires_at: datetime | None = None


ContextBuilder = Callable[[Request], dict[str, Any] | Awaitable[dict[str, Any]]]
"""Optional per-request context, e.g. path-parameter pluck for ABAC."""


class RequestAuthorizer(Protocol):
    """Decides whether a request may perform an :class:`Operation`.

    Implementations raise ``AuthenticationError`` (401),
    ``ForbiddenError`` (403), or ``NotFoundError`` (404) on denial; the
    framework does not catch these. On success they return the resolved
    :class:`Principal`.
    """

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal: ...


_default_authorizer: RequestAuthorizer | None = None
_operation_authorizers: dict[Operation, RequestAuthorizer] = {}


def set_authorizer(
    authorizer: RequestAuthorizer | None,
    *,
    operation: Operation | None = None,
) -> None:
    """Install an authorizer.

    Without ``operation``, this becomes the default authorizer used by
    every operation that does not have a specific override. With
    ``operation``, it overrides the default for that operation only -
    used to route a different family (e.g., runtime) through a
    different provider.

    Passing ``None`` clears the default (or the override). Tests use
    this via fixtures to install deterministic providers.
    """
    global _default_authorizer
    if operation is None:
        _default_authorizer = authorizer
        return
    if authorizer is None:
        _operation_authorizers.pop(operation, None)
        return
    _operation_authorizers[operation] = authorizer


def get_authorizer(operation: Operation | None = None) -> RequestAuthorizer:
    """Return the authorizer that should handle ``operation``.

    Looks up the operation-specific override first; falls back to the
    default. Raises if nothing is installed.
    """
    if operation is not None:
        specific = _operation_authorizers.get(operation)
        if specific is not None:
            return specific
    if _default_authorizer is None:
        raise RuntimeError(
            "No RequestAuthorizer installed. Call set_authorizer() at "
            "application startup, or use configure_auth_from_env()."
        )
    return _default_authorizer


def clear_authorizers() -> None:
    """Remove every installed authorizer (default + overrides). Test helper."""
    global _default_authorizer
    _default_authorizer = None
    _operation_authorizers.clear()


def require_operation(
    operation: Operation,
    *,
    context_builder: ContextBuilder | None = None,
) -> Callable[..., Awaitable[Principal]]:
    """Build a FastAPI dependency that authorizes ``operation``.

    The dependency consults the installed authorizer for this specific
    operation (falling back to the default) and returns the resulting
    :class:`Principal` so endpoints can read the resolved
    ``namespace_key`` and target binding without re-deriving them.

    A ``context_builder`` may extract additional context (e.g., path
    parameters) the provider needs to make a decision; the result is
    forwarded to :meth:`RequestAuthorizer.authorize` as ``context``.
    """
    import inspect

    async def dependency(request: Request) -> Principal:
        context: dict[str, Any] | None = None
        if context_builder is not None:
            built = context_builder(request)
            if inspect.isawaitable(built):
                built = await built
            context = built
        authorizer = get_authorizer(operation)
        return await authorizer.authorize(request, operation, context)

    return dependency
