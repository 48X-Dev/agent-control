"""One tool an agent uses to save something onto the ticket it is working on.

``save_to_tracker`` posts a comment on the tracker issue this session's task
came from. It is the only tool in this SDK whose effect leaves Agent Control,
and it is deliberately the narrowest one here.

Four properties are load-bearing, three shared with ``progress_tools.py`` and
``knowledge_tools.py`` for the same reasons.

**The issue comes from the session and cannot be named.** There is no issue
argument. The server reads the session, follows it to its task and takes that
task's tracker reference. A model never names a destination, so instructions
arriving inside a fetched page cannot point this at another ticket, and the
reach of the session's token is the one issue it was already working on.

**It comments and cannot close.** Moving an issue's state needs
``agent_tasks.approve``, which no session token carries and no tool here asks
for. An agent can add to the record; agreeing that the work is done stays a
person's press.

**Nothing here raises.** A tool that throws takes the turn down with it. A
missing credential, an unreachable control plane, a session with no ticket
behind it - all come back as an ordinary result carrying a sentence the model
can repeat to whoever asked, and the turn carries on.

**Saving twice saves twice.** There is no marker and no deduplication. Asking
an agent to save a correction is a normal thing to do, and swallowing the
second call because it resembled the first would lose the correction silently.

Operators: scope controls to the agent-qualified step name,
``root_agent.save_to_tracker``. The bare name matches nothing, warns about
nothing, and the tool runs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent_control._state import state

from ._session_state import SessionIdentity

__all__ = ["build_tracker_tools", "save_to_tracker"]

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15.0

_NO_CREDENTIAL_MESSAGE = (
    "This session has no credential for saving to the tracker, so nothing was "
    "posted. Tell the person who asked; they can copy the text across."
)
_UNREACHABLE_MESSAGE = (
    "Agent Control could not be reached, so nothing was posted to the tracker."
)


async def save_to_tracker(text: str, tool_context: Any = None) -> dict[str, Any]:
    """Post ``text`` as a comment on the ticket this session's task came from.

    Use when somebody asks for something to be saved, recorded or written to
    the ticket. It adds a comment and never closes, reassigns or edits the
    issue, and it cannot post to any other ticket.

    Returns ``{"saved": true, "issue_ref": ..., "issue_url": ...}`` when the
    comment was created, and ``{"saved": false, "message": ...}`` otherwise,
    where the message says why in a sentence worth repeating to the person who
    asked.
    """
    body = (text or "").strip()
    if not body:
        return {
            "saved": False,
            "message": "There was no text to save, so nothing was posted.",
        }

    identity = _identity(tool_context)
    if identity is None:
        return {"saved": False, "message": _NO_CREDENTIAL_MESSAGE}

    outcome = await _request(
        identity=identity,
        path=f"/api/v1/agent-sessions/{identity.session_key}/tracker-comment",
        body={"text": body},
    )
    if outcome.get("status") != "ok":
        return _failure(outcome)

    payload = outcome.get("payload") or {}
    return {
        "saved": True,
        "issue_ref": payload.get("issue_ref"),
        "issue_url": payload.get("issue_url"),
        "message": "Posted as a comment on the ticket. It was not closed.",
    }


def build_tracker_tools() -> list[Any]:
    """The tool, wrapped for an ADK agent's ``tools=[...]``.

    Named for what it builds rather than for this module, matching the other
    factories here. ADK is imported lazily so the module stays importable
    without ``google-adk``.
    """
    from google.adk.tools import (  # type: ignore[import-not-found,import-untyped]
        FunctionTool,
    )

    return [FunctionTool(save_to_tracker)]


# =============================================================================
# Internals
# =============================================================================


def _identity(tool_context: Any) -> SessionIdentity | None:
    """Which session this call belongs to, from ADK state.

    ``None`` when there is no session-bound token. Falling back to the process
    API key would let one agent comment on another session's ticket, which is
    the whole reason the token is bound to a session.
    """
    if tool_context is None:
        return None
    identity = SessionIdentity.read(getattr(tool_context, "state", None))
    if identity is None or identity.token is None:
        return None
    return identity


async def _request(
    *, identity: SessionIdentity, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Make the one call, never raising."""
    server_url = state.server_url
    if not server_url:
        return {"status": "unavailable"}

    headers = {"Authorization": f"Bearer {identity.token}"}
    try:
        async with httpx.AsyncClient(
            base_url=server_url.rstrip("/"),
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        ) as client:
            response = await client.post(path, json=body, headers=headers)
    except (TimeoutError, httpx.HTTPError):
        logger.debug("Agent Control tracker comment failed", exc_info=True)
        return {"status": "unavailable"}

    if response.status_code >= 400:
        return {
            "status": "refused",
            "http_status": response.status_code,
            "problem": _problem_of(response),
        }
    try:
        payload = response.json()
    except ValueError:
        return {"status": "unavailable"}
    return {"status": "ok", "payload": payload if isinstance(payload, dict) else {}}


def _problem_of(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _failure(outcome: dict[str, Any]) -> dict[str, Any]:
    """Turn a refusal into something the model can repeat to a person."""
    if outcome.get("status") == "unavailable":
        return {"saved": False, "message": _UNREACHABLE_MESSAGE}

    problem = outcome.get("problem") or {}
    detail = problem.get("detail")
    hint = problem.get("hint")
    message = detail if isinstance(detail, str) and detail else _UNREACHABLE_MESSAGE
    if isinstance(hint, str) and hint:
        message = f"{message} {hint}"
    return {
        "saved": False,
        "error_code": problem.get("error_code"),
        "message": message,
    }
