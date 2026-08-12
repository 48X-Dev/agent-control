"""Default :class:`RequestAuthorizer` that uses local credentials only.

Returns ``DEFAULT_NAMESPACE_KEY`` and enforces a per-operation access
level using the local API-key + session-cookie credential check from
:mod:`agent_control_server.auth`:

- ``ADMIN`` operations require an admin key (or admin session).
- ``AUTHENTICATED`` operations require any valid credential.
- ``PUBLIC`` operations are open.
- When the underlying local credential layer is disabled, every
  operation succeeds with a non-admin :class:`Principal`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import Request

from ...auth import _validate_api_key
from ...models import DEFAULT_NAMESPACE_KEY
from ..core import Operation, Principal, RequestAuthorizer


class AccessLevel(Enum):
    """Access level required for an operation under the local-credential path."""

    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"


# Single source of truth for the local-credential access policy. Adding
# a new :class:`Operation` here makes its required access level
# explicit and auditable; missing entries are rejected at startup so
# wiring drift is loud, not silent.
DEFAULT_OPERATION_ACCESS: dict[Operation, AccessLevel] = {
    Operation.CONTROL_BINDINGS_READ: AccessLevel.AUTHENTICATED,
    Operation.CONTROL_BINDINGS_WRITE: AccessLevel.ADMIN,
    Operation.CONTROLS_READ: AccessLevel.AUTHENTICATED,
    Operation.CONTROLS_CREATE: AccessLevel.ADMIN,
    Operation.CONTROLS_UPDATE: AccessLevel.ADMIN,
    Operation.CONTROLS_DELETE: AccessLevel.ADMIN,
    Operation.POLICIES_READ: AccessLevel.AUTHENTICATED,
    Operation.POLICIES_CREATE: AccessLevel.ADMIN,
    Operation.POLICIES_UPDATE: AccessLevel.ADMIN,
    Operation.AGENTS_READ: AccessLevel.AUTHENTICATED,
    Operation.AGENTS_CREATE: AccessLevel.AUTHENTICATED,
    Operation.AGENTS_UPDATE: AccessLevel.ADMIN,
    Operation.EVALUATORS_READ: AccessLevel.AUTHENTICATED,
    Operation.TEAMS_READ: AccessLevel.AUTHENTICATED,
    Operation.TEAMS_WRITE: AccessLevel.ADMIN,
    Operation.OBSERVABILITY_READ: AccessLevel.AUTHENTICATED,
    Operation.OBSERVABILITY_WRITE: AccessLevel.AUTHENTICATED,
    Operation.RUNTIME_TOKEN_EXCHANGE: AccessLevel.AUTHENTICATED,
    Operation.RUNTIME_USE: AccessLevel.AUTHENTICATED,
    # Opening a chat and reading it back is per-caller working state, not org
    # configuration. ADMIN here would mean only admin keys can hold a
    # conversation, which removes the feature; the precedent is AGENTS_CREATE.
    Operation.AGENT_SESSIONS_READ: AccessLevel.AUTHENTICATED,
    Operation.AGENT_SESSION_CONTENT_READ: AccessLevel.AUTHENTICATED,
    Operation.AGENT_SESSIONS_WRITE: AccessLevel.AUTHENTICATED,
    Operation.AGENT_SESSIONS_RUN: AccessLevel.AUTHENTICATED,
    Operation.AGENT_NUDGES_WRITE: AccessLevel.AUTHENTICATED,
    # Same tier as AGENT_SESSIONS_RUN, and for a reason that only reads one
    # way round: a credential that can start a turn which spends money must be
    # able to stop it. The service scopes halt creation to the caller who
    # opened the session, which is what keeps an AUTHENTICATED stop from being
    # a way to end everybody else's turns for free.
    Operation.AGENT_HALTS_WRITE: AccessLevel.AUTHENTICATED,
    # AUTHENTICATED on the AGENT_SESSIONS_RUN precedent: whoever may start a
    # turn may attach a file to it, and an admin-only attach is a feature
    # nobody can use. The tier is defensible because the accepted set is
    # decided by a magic-byte sniff rather than by what the upload claimed, and
    # because the service scopes the write to the caller who opened the session
    # - a session belonging to a dispatch task refuses a bystander outright.
    Operation.AGENT_ATTACHMENTS_WRITE: AccessLevel.AUTHENTICATED,
    # Deployment configuration, same tier as CONTROL_BINDINGS_WRITE.
    Operation.AGENT_RUNTIMES_WRITE: AccessLevel.ADMIN,
    # Readable by any key in the namespace, including agent process keys,
    # because delivery is the agent fetching its own config on the refresh loop.
    # ADMIN here would put an admin key in every agent process, which is a worse
    # posture than the exposure it prevents. The exposure it accepts, stated
    # rather than glossed: every key in a namespace can read every other agent's
    # prompt and its full version history, and because clearing preserves
    # history, that outlives the decision to remove a prompt.
    Operation.AGENT_CONFIGS_READ: AccessLevel.AUTHENTICATED,
    # ADMIN on two independent grounds. The prompt body lands in
    # system_instruction, which no control can see, so a lower-privileged write
    # would override ADMIN-authored control policy in a field no guardrail
    # evaluates. And the model choice spends the operator's quota on every turn
    # of every session and changes how reliably the agent follows that policy.
    # Same tier as CONTROLS_CREATE and AGENT_RUNTIMES_WRITE.
    #
    # This tier only binds when credential enforcement is on. With
    # api_key_enabled false the default authorizer is NoAuthProvider and every
    # operation succeeds unauthenticated, which is why delivery has its own
    # startup gate; see check_agent_config_startup_requirements.
    Operation.AGENT_CONFIGS_WRITE: AccessLevel.ADMIN,
    # ``agent_nudges.consume`` guards halt delivery as well as nudge delivery,
    # and there is deliberately no separate halt operation. Halts ride the same
    # claim call as nudges at the model boundary, so a deployment that
    # restricted a separate operation would still have halts delivered under
    # this one, and revoking this one would silently disable half of halt
    # delivery - which reads to an operator as "stop sometimes doesn't work".
    #
    # Machine-side operations are normally routed to the runtime-JWT provider,
    # which binds them to a single session. These entries are the fallback for
    # a deployment with no runtime token secret configured, and they are ADMIN
    # rather than AUTHENTICATED on purpose: under the local-credential path any
    # authenticated key would otherwise be able to claim and swallow another
    # caller's nudges, which is the exact hole the token binding exists to
    # close. Failing closed is the right default for a path with no session
    # binding available.
    Operation.AGENT_NUDGES_CONSUME: AccessLevel.ADMIN,
    Operation.AGENT_PLANS_WRITE: AccessLevel.ADMIN,
    # Same tier and the same reason: with no session binding to key on, an
    # ordinary key here would let any caller comment on any session's issue.
    Operation.AGENT_TRACKER_COMMENT: AccessLevel.ADMIN,
    # The dispatch ledger sits at AUTHENTICATED, all four of it, and the
    # reasoning is the same one AGENTS_CREATE already settled: a play button
    # only an admin can press is a play button an admin presses on somebody
    # else's behalf, which is worse oversight than the person who wants the
    # work doing pressing it themselves. What bounds the money is the confirmed
    # set plus the namespace budget, not the credential tier.
    #
    # Read is AUTHENTICATED for a second, independent reason: it is the
    # oversight path. A session belonging to a task has no human owner, so the
    # content-access predicate lets any caller holding this operation read,
    # halt and nudge it. Putting that at ADMIN would mean overseeing the fleet
    # required a key that also carries ``controls.create`` and
    # ``agent_runtimes.write``.
    #
    # And note what this tier cannot express, because it is why the accept path
    # is written the way it is: one ordinary key holds write, claim and approve
    # at once. "May run agents, may not accept their work" is not a tier here.
    # It is a server-side comparison of caller hashes on the accept route.
    Operation.AGENT_TASKS_READ: AccessLevel.AUTHENTICATED,
    Operation.AGENT_TASKS_WRITE: AccessLevel.AUTHENTICATED,
    Operation.AGENT_TASKS_CLAIM: AccessLevel.AUTHENTICATED,
    Operation.AGENT_TASKS_APPROVE: AccessLevel.AUTHENTICATED,
    # Read is AUTHENTICATED because the dispatcher reads the plan for every
    # task it claims, and because an operator watching a chain needs to see
    # which agent is supposed to run next.
    Operation.AGENT_WORKFLOWS_READ: AccessLevel.AUTHENTICATED,
    # Write is ADMIN, the same tier as CONTROLS_CREATE and
    # AGENT_RUNTIMES_WRITE, on two grounds. It names the agents an autonomous
    # chain runs, and agents differ in system prompt, in bound controls and in
    # tools - so whoever writes this row chooses the blast radius. And the
    # step's ``brief`` is the one part of the turn message that is *not* framed
    # as untrusted data, which makes it the only operator-authored instruction
    # channel into a dispatch turn. A lower tier here would be a way to steer
    # somebody else's fleet without touching a control.
    Operation.AGENT_WORKFLOWS_WRITE: AccessLevel.ADMIN,
    # The pause is ADMIN, which is the plan's level for it and is the opposite
    # tier from the ledger above. The asymmetry is deliberate. Pausing is not
    # only a stop: the same flag is what un-pausing clears, so a tier that can
    # set it is a tier that can hold every namespace's dispatch down, and under
    # the local-credential provider "authenticated" is every valid key in the
    # deployment. A stop anybody can press is a stop anybody can press twice.
    # Reaching an operator with an admin key is the level-4 runbook's problem
    # and it is a solved one; an unprivileged caller freezing the fleet is not.
    Operation.AGENT_DISPATCH_PAUSE: AccessLevel.ADMIN,
    # A namespace-wide stop is ADMIN for a different reason than the pause:
    # ``agent_halts.write`` is AUTHENTICATED because whoever can start a turn
    # must be able to stop it, and the scoping that keeps that from being a
    # denial-of-service primitive is creator scoping in the service. This
    # operation has no such scoping by construction - reaching every session in
    # the namespace is the whole point - so the tier is what bounds it.
    Operation.AGENT_HALTS_WRITE_ALL: AccessLevel.ADMIN,
    # Knowledge search is ADMIN here on the AGENT_NUDGES_CONSUME precedent and
    # for its exact reason. The real grant is SESSION_TOKEN_SCOPES: an executor
    # searches with a token bound to one session, short-lived and target-bound.
    # This entry is only the fallback for a deployment with no runtime secret,
    # and there the window has no verified binding to key on, so it falls back
    # to one bucket for the namespace and this tier fails closed beside it.
    Operation.COMPANY_KNOWLEDGE_SEARCH: AccessLevel.ADMIN,
    # The oversight path, same tier as AGENT_TASKS_READ and for its reason: it
    # is how a human sees whether the mirror is current and what is in it, and
    # putting that behind ADMIN would mean watching the corpus required a key
    # that also carries ``controls.create``. It does reach snippet text through
    # the console panel, which is a wider grant than counters alone and is the
    # same reach section 9 of the plan already gives every agent in the
    # namespace - the allowlist is the security boundary, not the read path.
    Operation.COMPANY_KNOWLEDGE_STATUS: AccessLevel.AUTHENTICATED,
}


class HeaderAuthProvider(RequestAuthorizer):
    """Default authorizer.

    For each operation's configured access level, validates the
    request's credentials via the local credential check; on success,
    returns a :class:`Principal` scoped to the resolved namespace.
    """

    def __init__(
        self,
        *,
        operation_access: dict[Operation, AccessLevel] | None = None,
        default_namespace_key: str = DEFAULT_NAMESPACE_KEY,
    ) -> None:
        self._operation_access = (
            DEFAULT_OPERATION_ACCESS if operation_access is None else operation_access
        )
        self._default_namespace_key = default_namespace_key

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del context  # The local-credential path does not use context.

        access = self._operation_access.get(operation)
        if access is None:
            raise RuntimeError(f"No access level configured for operation {operation.value!r}")

        namespace_key = self._resolve_namespace_key(request)

        if access is AccessLevel.PUBLIC:
            return Principal(namespace_key=namespace_key)

        api_key = request.headers.get("X-API-Key")
        client = await _validate_api_key(
            api_key,
            request,
            require_admin=access is AccessLevel.ADMIN,
        )
        # Runtime token exchange returns a normalized scope grant so the
        # exchange endpoint can require ``runtime.use`` uniformly across
        # providers.
        scopes: tuple[str, ...] = (
            (Operation.RUNTIME_USE.value,) if operation is Operation.RUNTIME_TOKEN_EXCHANGE else ()
        )
        return Principal(
            namespace_key=namespace_key,
            is_admin=client.is_admin,
            caller_id=client.key_id,
            scopes=scopes,
        )

    def _resolve_namespace_key(self, request: Request) -> str:
        # Local credentials do not carry namespace metadata. Providers
        # that resolve a namespace can return a different principal.
        del request
        return self._default_namespace_key
