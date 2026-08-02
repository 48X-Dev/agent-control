"""Two tools an agent uses to say what it is doing.

``declare_plan`` records the steps the agent intends to take. ``mark_step``
records how one of them went. Together they are the only progress signal in this
stack with an author, which is the entire reason they exist: an executor emits
events, and a percentage synthesised from event counts is a number that moves
and means nothing.

What a console shows from these is labelled "Plan reported by the agent" and
sits beside a link to the turn's trace. That wording is not hedging. Nothing
here verifies the claim, and nothing can: an agent that marks step three done
without doing it has told a lie this SDK cannot detect. What it can do is refuse
to launder the claim into a measurement and put the independent evidence next to
it.

Three properties are load-bearing.

**Identity comes from the session, never from an argument.** Both tools read the
session key and the runtime token out of ``tool_context.state``, which Agent
Control seeded at session creation and refreshes with every turn. A model
cannot name a session it should not write to, because it never names one at all.

**Nothing here raises.** A tool that throws takes the turn down with it. Every
failure - no credential, an unreachable control plane, a refusal - comes back as
an ordinary result the model can read and act on, which is also how an agent
that marked a nonexistent step learns to mark a real one.

**The revision is part of a step's identity.** ``mark_step`` names the revision
it belongs to, and the server refuses a stale one. Agents replan; without the
revision, marking a step of the plan the agent has in mind would mark a step of
the plan it has since replaced.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent_control._state import state

from ._session_state import SessionIdentity

logger = logging.getLogger(__name__)

PLAN_REVISION_STATE_KEY = "agent_control_plan_revision"
"""Where the current revision is remembered between tool calls.

Written into ADK session state by ``declare_plan`` and read back by
``mark_step`` when the model does not supply one. A flat key of its own rather
than a field inside the seeded ``agent_control`` block, so a tool writing its
own bookkeeping can never shadow the identity the server seeded there."""

PLAN_STEP_STATUSES = ("pending", "active", "done", "skipped", "failed")
"""Mirrors the server's enum. Checked here first so a typo costs a sentence back
to the model rather than a round trip and a 422."""

REQUEST_TIMEOUT_SECONDS = 10.0
"""Ceiling on one progress write.

Longer than the nudge claim's, because this is not on the path of every model
call - it runs when the agent chooses to report - and shorter than any turn, so
a control plane that is slow costs the agent a sentence rather than the turn."""

_MAX_STEPS = 40
"""Mirrors ``PLAN_MAX_STEPS``. A plan longer than this is refused whole by the
server; refusing it here too means the agent is told why in one hop."""

# Hand-written result text. These are what the model reads, so they say what
# happened and what to do about it, and they never carry a transport detail an
# agent could mistake for a fact about its own work.
_NO_SESSION_MESSAGE = (
    "Progress reporting is not available in this session, so the plan was not "
    "recorded. Carry on with the work; this does not affect anything else."
)
_UNREACHABLE_MESSAGE = (
    "Agent Control could not be reached, so the plan was not recorded. Carry on "
    "with the work and report progress again later if you want to."
)


async def declare_plan(steps: list[str], tool_context: Any = None) -> dict[str, Any]:
    """Record the steps you intend to take, in order.

    Call this once when you have decided how to approach the work, and again if
    you change approach: a re-declared plan is recorded as a new revision rather
    than as an edit, so a person watching sees that you replanned instead of
    seeing the steps quietly change under them.

    Args:
        steps: The steps you intend to take, in order, one short line each.

    Returns:
        The revision your plan was recorded as, and how many steps it holds.
        Use that revision when you mark a step.
    """
    cleaned = [str(step).strip() for step in steps or []]
    cleaned = [step for step in cleaned if step]
    if not cleaned:
        return {
            "status": "rejected",
            "message": "A plan needs at least one step. Nothing was recorded.",
        }
    if len(cleaned) > _MAX_STEPS:
        return {
            "status": "rejected",
            "message": (
                f"A plan may hold at most {_MAX_STEPS} steps and this one has "
                f"{len(cleaned)}. Nothing was recorded. Declare the outline and "
                f"break a step down later if you need to."
            ),
        }

    identity = _identity(tool_context)
    if identity is None:
        return {"status": "unavailable", "message": _NO_SESSION_MESSAGE}

    outcome = await _request(
        identity=identity,
        method="PUT",
        path=f"/api/v1/agent-sessions/{identity.session_key}/plan",
        body={"steps": cleaned},
    )
    if outcome.get("status") != "ok":
        return _failure(outcome)

    plan = (outcome.get("payload") or {}).get("plan") or {}
    revision = plan.get("revision")
    if isinstance(revision, int):
        _remember_revision(tool_context, revision)
    return {
        "status": "recorded",
        "plan_revision": revision,
        "step_count": len(plan.get("steps") or cleaned),
        "message": (
            "Your plan was recorded and is shown to the operator as reported by "
            "you. Mark steps as you go, using this revision."
        ),
    }


async def mark_step(
    plan_revision: int,
    step_index: int,
    status: str,
    note: str = "",
    tool_context: Any = None,
) -> dict[str, Any]:
    """Record how one step of your declared plan went.

    Args:
        plan_revision: The revision your plan was recorded as. Pass 0 to use the
            most recent revision you declared in this session.
        step_index: Which step, counting from 0 in the order you declared them.
        status: One of "pending", "active", "done", "skipped" or "failed".
        note: Optional short note about how the step went.

    Returns:
        Whether the step was recorded, or why it was not.
    """
    normalized = str(status or "").strip().lower()
    if normalized not in PLAN_STEP_STATUSES:
        return {
            "status": "rejected",
            "message": (
                f"'{status}' is not a step status. Use one of: "
                f"{', '.join(PLAN_STEP_STATUSES)}. Nothing was recorded."
            ),
        }
    if step_index < 0:
        return {
            "status": "rejected",
            "message": "Step indexes start at 0. Nothing was recorded.",
        }

    identity = _identity(tool_context)
    if identity is None:
        return {"status": "unavailable", "message": _NO_SESSION_MESSAGE}

    revision = plan_revision if plan_revision and plan_revision > 0 else None
    if revision is None:
        revision = _recall_revision(tool_context)
    if revision is None:
        return {
            "status": "rejected",
            "message": (
                "No plan has been declared in this session, so there is no step "
                "to mark. Declare a plan first."
            ),
        }

    body: dict[str, Any] = {"status": normalized}
    trimmed = (note or "").strip()
    if trimmed:
        body["note"] = trimmed

    outcome = await _request(
        identity=identity,
        method="PATCH",
        path=(
            f"/api/v1/agent-sessions/{identity.session_key}"
            f"/plan/revisions/{revision}/steps/{step_index}"
        ),
        body=body,
    )
    if outcome.get("status") != "ok":
        return _failure(outcome)

    plan = (outcome.get("payload") or {}).get("plan") or {}
    if isinstance(plan.get("revision"), int):
        _remember_revision(tool_context, int(plan["revision"]))
    return {
        "status": "recorded",
        "plan_revision": plan.get("revision", revision),
        "step_index": step_index,
        "step_status": normalized,
    }


# =============================================================================
# ADK wiring
# =============================================================================


def build_progress_tools() -> list[Any]:
    """The two tools, wrapped for an ADK agent's ``tools=[...]``.

    Named for what it builds rather than for this module, because a factory
    sharing its module's name shadows one with the other depending on which the
    import machinery resolved first.

    ADK is imported lazily so this module stays importable without
    ``google-adk`` installed, matching the rest of this integration package. The
    two functions above are the contract; this is convenience.
    """
    from google.adk.tools import (  # type: ignore[import-not-found,import-untyped]
        FunctionTool,
    )

    return [FunctionTool(declare_plan), FunctionTool(mark_step)]


# =============================================================================
# Internals
# =============================================================================


def _identity(tool_context: Any) -> SessionIdentity | None:
    """Which session this tool call belongs to, from ADK state.

    Returns ``None`` rather than raising when the state is missing or
    unreadable. Progress reporting is an enhancement to work that is happening
    anyway; failing a tool call over it would cost the turn.
    """
    if tool_context is None:
        return None
    identity = SessionIdentity.read(getattr(tool_context, "state", None))
    if identity is None or identity.token is None:
        # No token means no credential this session may write with. Reporting
        # progress under the process's own API key would let one agent rewrite
        # another session's plan, which is the whole reason the token is
        # session-bound.
        return None
    return identity


def _remember_revision(tool_context: Any, revision: int) -> None:
    """Record the revision so a later ``mark_step`` need not be told it."""
    if tool_context is None:
        return
    try:
        tool_context.state[PLAN_REVISION_STATE_KEY] = revision
    except Exception:
        logger.debug("Could not record the plan revision in session state", exc_info=True)


def _recall_revision(tool_context: Any) -> int | None:
    if tool_context is None:
        return None
    try:
        value = tool_context.state.get(PLAN_REVISION_STATE_KEY)
    except Exception:
        logger.debug("Could not read the plan revision from session state", exc_info=True)
        return None
    return int(value) if isinstance(value, int) and value > 0 else None


async def _request(
    *,
    identity: SessionIdentity,
    method: str,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Make one progress write, never raising.

    Returns ``{"status": "ok", "payload": ...}`` or a description of what went
    wrong. A refusal from Agent Control carries its own hand-written detail
    through, because that text is what tells the agent how to correct itself -
    which revision is current, how many steps its plan actually has.
    """
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
            response = await client.request(method, path, json=body, headers=headers)
    except (TimeoutError, httpx.HTTPError):
        logger.debug("Agent Control progress write failed", exc_info=True)
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
    """Turn a failed write into something the model can act on."""
    if outcome.get("status") == "unavailable":
        return {"status": "unavailable", "message": _UNREACHABLE_MESSAGE}

    problem = outcome.get("problem") or {}
    detail = problem.get("detail")
    hint = problem.get("hint")
    message = detail if isinstance(detail, str) and detail else _UNREACHABLE_MESSAGE
    if isinstance(hint, str) and hint:
        message = f"{message} {hint}"
    return {
        "status": "refused",
        "error_code": problem.get("error_code"),
        "message": message,
    }
