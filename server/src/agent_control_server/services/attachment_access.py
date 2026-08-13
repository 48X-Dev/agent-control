"""Who may put bytes into somebody's conversation.

Separated from the service next door because the caller is different in kind.
The service stores files and counts them; this decides one question, it is the
question a reviewer will want to read on its own, and it is the part that has to
be correct under two auth providers that disagree about whether callers exist.
"""

from __future__ import annotations

from agent_control_models.errors import ErrorCode

from ..errors import ForbiddenError
from ..models import AgentSession
from .agent_sessions import require_content_access


def authorize_attachment_write(
    row: AgentSession,
    *,
    caller_hash: str | None,
    is_admin: bool,
    session_bound_token: bool = False,
) -> None:
    """Decide whether this caller may put bytes into this conversation.

    **A session-bound runtime token is the session**, so it returns before any
    of the rules below. Every one of them asks which human this conversation
    belongs to, and the agent running inside it is not one: a task's session
    would refuse its own agent under the first, and an unattributed one under
    the second. The verifier has already refused a token naming a different
    session, so the caller here is this conversation writing to itself.

    The shared predicate first, with ``for_turn=True``, because an upload is
    driving the conversation rather than observing it. Then two conditions that
    the predicate cannot express, both checked directly rather than inferred,
    and neither dependent on ``created_by_hash`` being populated.

    **A dispatch task's session refuses anyone but its creator or an admin.**
    ``require_content_access`` returns early on a NULL creator, which is every
    session under the default provider, so relying on it here would leave a
    task's conversation open to any authenticated bystander. Files reach a task
    session through the ingress path that opened it, never through a passer-by.
    Where there is no caller identity at all this refuses *every* upload into a
    task's session, which is the fail-closed reading and costs nothing: a
    tracker's files arrive through a server-side fetch, not through this route.

    **An unattributed session refuses only when attribution was possible.**
    ``created_by_hash IS NULL`` *and* a caller identity exists means a provider
    that resolves callers did not attribute this session, which is worth
    refusing. Under the default provider there is no caller identity at all, so
    this condition is inert by construction - which is correct, because that
    deployment is one trust domain with no boundary to enforce, and a rule that
    fired there would 403 every upload in it.
    """
    if session_bound_token:
        return

    require_content_access(
        row, caller_hash=caller_hash, is_admin=is_admin, for_turn=True
    )

    if is_admin:
        return

    caller_is_the_holder = caller_hash is not None and row.created_by_hash == caller_hash
    if row.agent_task_id is not None and not caller_is_the_holder:
        raise ForbiddenError(
            error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
            detail="This session belongs to a dispatch task and is driven by its dispatcher.",
            resource="AgentSession",
            resource_id=row.session_key,
            hint=(
                "Attach the file to the tracker issue the task came from, or "
                "open your own session with this agent."
            ),
        )

    if row.created_by_hash is None and caller_hash is not None:
        raise ForbiddenError(
            error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
            detail="This session records no owner, so an upload into it cannot be attributed.",
            resource="AgentSession",
            resource_id=row.session_key,
            hint="Open a new session with this credential and attach the file there.",
        )
