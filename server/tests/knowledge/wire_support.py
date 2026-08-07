"""The rig the two wire files share: a session token, the tool, and a judge.

Plain functions and small classes, deliberately not fixtures, so a test that
wants two sessions or two controls calls them twice. The fixtures themselves
stay in the test modules, which is the pattern the corpus tests beside this
file already use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_control.integrations.google_adk import knowledge_tools
from agent_control.integrations.google_adk.knowledge_controls import KNOWLEDGE_CONTROLS
from agent_control_engine.core import ControlEngine
from agent_control_models import EvaluationRequest, Step
from agent_control_models.controls import ControlDefinitionRuntime
from agent_control_server.services.agent_sessions import mint_session_runtime_token

SESSION = "sess-wire-a"
FOREIGN_SESSION = "sess-wire-b"
RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"
ACTOR = "0123456789abcdef"
AGENT = "knowledge-wire-agent"


class ToolContext:
    """What ADK hands a tool: state, and nothing the model chose.

    The session key and the credential are read from here rather than from an
    argument, which is why a model cannot search as another session. The state
    block is the shape ``SessionIdentity.read`` expects.
    """

    def __init__(self, *, session_key: str, token: str) -> None:
        self.state: dict[str, Any] = {
            "agent_control": {
                "session_key": session_key,
                "namespace_key": "default",
                "runtime_token": token,
            }
        }


def token_for(session_key: str) -> str:
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id=ACTOR
    )
    assert minted is not None, "the runtime secret is not configured for this test"
    return str(minted[0])


def context(session_key: str = SESSION, bound_to: str | None = None) -> ToolContext:
    """A tool context for ``session_key``, holding a token for ``bound_to``.

    The two differ only when a test is asking what happens when they differ,
    which is the whole of the session-binding assertion.
    """
    return ToolContext(session_key=session_key, token=token_for(bound_to or session_key))


async def search(query: str = "laptop reimbursement", **kwargs: Any) -> dict[str, Any]:
    return await knowledge_tools.company_knowledge_search(query, tool_context=context(), **kwargs)


@dataclass
class Bound:
    """A control as the engine sees it: an id, a name and a definition."""

    id: int
    name: str
    control: ControlDefinitionRuntime


def bound(name: str, **overrides: Any) -> Bound:
    """One of the three shipped controls, as an operator would bind it."""
    return Bound(
        id=1,
        name=name,
        control=ControlDefinitionRuntime.model_validate({**KNOWLEDGE_CONTROLS[name], **overrides}),
    )


def content_control(pattern: str, *, step_names: list[str], stage: str, path: str) -> Bound:
    """The live ``block-ssn`` shape from this repo's README, pointed anywhere."""
    return Bound(
        id=2,
        name="block-ssn",
        control=ControlDefinitionRuntime.model_validate(
            {
                "enabled": True,
                "execution": "server",
                "scope": {"step_types": ["tool"], "step_names": step_names, "stages": [stage]},
                "action": {"decision": "deny"},
                "condition": {
                    "selector": {"path": path},
                    "evaluator": {"name": "regex", "config": {"pattern": pattern}},
                },
            }
        ),
    )


async def judge(
    control: Bound,
    *,
    step_name: str,
    stage: str,
    step_input: Any = None,
    step_output: Any = None,
) -> list[str]:
    """Run one control over one step and report which controls matched."""
    response = await ControlEngine([control]).process(
        EvaluationRequest(
            agent_name=AGENT,
            step=Step(
                type="tool",
                name=step_name,
                input=step_input if step_input is not None else {},
                output=step_output,
            ),
            stage=stage,
        )
    )
    return [match.control_name for match in (response.matches or [])]
