"""The three shipped knowledge controls: do they compile, and are they right.

Both halves, because review proved the first is not enough. A control can be
valid schema and still be a wrong control, and the two wrong ones this design
already met are pinned here as tests rather than as warnings in a comment:

* the external-author deny selecting a bare integer into the ``json``
  evaluator, which denies **every** search including a count of zero;
* the qualified-name trap, where a control scoped to the bare tool name
  matches nothing, warns about nothing, and the tool runs.

Everything below drives the real ``ControlEngine`` with the real evaluators
over real ``Step`` payloads, which is the same path a bound control takes in a
live turn. Nothing here stubs an evaluator, because a stub cannot disagree with
the thing it stands in for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from agent_control.integrations.google_adk.knowledge_controls import (
    DENY_EXTERNAL_AUTHOR,
    DENY_FENCE_IN_WEB_ARGS,
    KNOWLEDGE_CONTROLS,
    OBSERVE_REFUSAL,
    SEARCH_STEP_NAME,
)
from agent_control_engine.core import ControlEngine
from agent_control_models import EvaluationRequest, Step
from agent_control_models.controls import ControlDefinitionRuntime
from agent_control_models.knowledge import KnowledgeRefusalCode

AGENT = "knowledge-controls"


@dataclass
class _Bound:
    """A control as the engine sees it: an id, a name and a definition."""

    id: int
    name: str
    control: ControlDefinitionRuntime


def _bound(name: str, **overrides: Any) -> _Bound:
    data = {**KNOWLEDGE_CONTROLS[name], **overrides}
    return _Bound(id=1, name=name, control=ControlDefinitionRuntime.model_validate(data))


def _tool_result(**fields: Any) -> dict[str, Any]:
    """A tool result in the shape ``knowledge_tools`` actually returns."""
    return {
        "text": "…",
        "result_count": 1,
        "external_author_count": 0,
        "stale_seconds": None,
        "refusal_code": None,
        **fields,
    }


async def _evaluate(
    bound: _Bound,
    *,
    step_name: str,
    stage: str,
    step_input: Any = None,
    step_output: Any = None,
) -> Any:
    request = EvaluationRequest(
        agent_name=AGENT,
        step=Step(
            type="tool",
            name=step_name,
            input=step_input if step_input is not None else {},
            output=step_output,
        ),
        stage=stage,
    )
    return await ControlEngine([bound]).process(request)


def _matched(response: Any) -> list[str]:
    return [match.control_name for match in (response.matches or [])]


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(KNOWLEDGE_CONTROLS))
def test_every_shipped_control_is_authorable(name: str) -> None:
    """An unauthorable control has to fail CI, not Phase 3."""
    definition = ControlDefinitionRuntime.model_validate(KNOWLEDGE_CONTROLS[name])

    assert definition.execution == "server"
    assert definition.enabled is True


def test_every_knowledge_step_name_is_agent_qualified() -> None:
    """The bare name is the silent failure; nothing shipped may carry one."""
    for name, data in KNOWLEDGE_CONTROLS.items():
        for step_name in data["scope"].get("step_names", []):
            assert "." in step_name, f"{name} scopes an unqualified step name"


# ---------------------------------------------------------------------------
# The external-author deny, and the shape it was corrected from
# ---------------------------------------------------------------------------


async def test_a_result_set_with_no_external_authors_passes() -> None:
    response = await _evaluate(
        _bound(DENY_EXTERNAL_AUTHOR),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(external_author_count=0),
    )

    assert response.is_safe is True
    assert _matched(response) == []


async def test_one_external_author_denies() -> None:
    response = await _evaluate(
        _bound(DENY_EXTERNAL_AUTHOR),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(external_author_count=1),
    )

    assert response.is_safe is False
    assert _matched(response) == [DENY_EXTERNAL_AUTHOR]


async def test_a_result_missing_the_field_denies_rather_than_passing() -> None:
    """Fails closed, which is the property the whole shape was chosen for.

    With the object selected, a missing key is "field not found", which the
    evaluator reports as a match. So the day the tool stops emitting the field,
    searches are denied instead of quietly being let through with an
    unenforceable control attached to them.
    """
    output = _tool_result()
    del output["external_author_count"]

    response = await _evaluate(
        _bound(DENY_EXTERNAL_AUTHOR),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=output,
    )

    assert response.is_safe is False
    assert _matched(response) == [DENY_EXTERNAL_AUTHOR]


async def test_the_scalar_selector_would_have_denied_every_single_search() -> None:
    """The rejected draft, kept as a test so nobody re-writes it from taste.

    ``output.external_author_count`` is an ``int``. ``_parse_json`` accepts
    dict, list or JSON string and answers "Unsupported data type: int" for
    anything else; ``allow_invalid_json`` defaults False, which turns that
    parse error into ``matched=True``. The result is a deny that fires on a
    perfectly clean search - the exact profile of a control the first
    inconvenienced operator switches off for good.
    """
    wrong = _bound(
        DENY_EXTERNAL_AUTHOR,
        condition={
            "selector": {"path": "output.external_author_count"},
            "evaluator": {
                "name": "json",
                "config": {"field_constraints": {"external_author_count": {"max": 0}}},
            },
        },
    )

    response = await _evaluate(
        wrong,
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(external_author_count=0),
    )

    assert response.is_safe is False, "the scalar idiom denies a clean search"


# ---------------------------------------------------------------------------
# The qualified-name trap, at unit level
# ---------------------------------------------------------------------------


async def test_the_bare_tool_name_matches_nothing_and_says_nothing() -> None:
    """W-K2's first half without a live model: reproduce, then fix.

    A control scoped to ``company_knowledge_search`` never becomes applicable,
    so there is no match, no error and no warning - the search simply runs. The
    fix is one string, and it is why every shipped definition carries the
    agent prefix.
    """
    bare = _bound(DENY_EXTERNAL_AUTHOR, scope={
        "step_types": ["tool"],
        "step_names": ["company_knowledge_search"],
        "stages": ["post"],
    })

    response = await _evaluate(
        bare,
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(external_author_count=9),
    )

    assert response.is_safe is True
    assert _matched(response) == []
    assert response.errors is None

    fixed = await _evaluate(
        _bound(DENY_EXTERNAL_AUTHOR),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(external_author_count=9),
    )
    assert fixed.is_safe is False


# ---------------------------------------------------------------------------
# The observe control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [code.value for code in KnowledgeRefusalCode])
async def test_every_refusal_code_is_observed(code: str) -> None:
    response = await _evaluate(
        _bound(OBSERVE_REFUSAL),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(refusal_code=code, result_count=0),
    )

    assert _matched(response) == [OBSERVE_REFUSAL]
    assert response.is_safe is True, "observing is not denying"


async def test_an_ordinary_answer_is_not_observed() -> None:
    """``refusal_code: null`` is the common case and must stay quiet.

    An observe control that fires on every successful search is a log nobody
    reads, which is the same as no signal at all.
    """
    response = await _evaluate(
        _bound(OBSERVE_REFUSAL),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(refusal_code=None),
    )

    assert _matched(response) == []


async def test_the_observe_control_ignores_an_unrelated_word() -> None:
    """Anchored, so a tool answering "rate_limited by upstream" is not a
    knowledge refusal."""
    response = await _evaluate(
        _bound(OBSERVE_REFUSAL),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=_tool_result(refusal_code="rate_limited by upstream"),
    )

    assert _matched(response) == []


# ---------------------------------------------------------------------------
# The egress tripwire
# ---------------------------------------------------------------------------


async def test_a_web_argument_carrying_a_fenced_block_is_denied() -> None:
    quoted = (
        '<<<KNOWLEDGE_BEGIN 1: "Ops Handbook/pricing.md" modified 2026-07-30 '
        "synced 2026-08-06 author workspace>>>\nthe enterprise floor is 40k\n"
        "<<<KNOWLEDGE_END 1>>>"
    )

    response = await _evaluate(
        _bound(DENY_FENCE_IN_WEB_ARGS),
        step_name="root_agent.web_search_exa",
        stage="pre",
        step_input={"query": quoted},
    )

    assert response.is_safe is False
    assert _matched(response) == [DENY_FENCE_IN_WEB_ARGS]


async def test_an_innocent_web_argument_is_untouched() -> None:
    response = await _evaluate(
        _bound(DENY_FENCE_IN_WEB_ARGS),
        step_name="root_agent.web_fetch_exa",
        stage="pre",
        step_input={"url": "https://example.com/pricing"},
    )

    assert response.is_safe is True
    assert _matched(response) == []


async def test_the_tripwire_catches_only_the_whole_block_form() -> None:
    """Its limit, asserted rather than described.

    A model that paraphrases a snippet into a search query steps straight past
    this. That is why the tripwire is the third line of defence in the plan and
    not the first: the decision that bounds this pairing is co-provisioning
    itself, made in the tool allowlist on purpose.
    """
    paraphrased = {"query": "what is the enterprise floor price at this company"}

    response = await _evaluate(
        _bound(DENY_FENCE_IN_WEB_ARGS),
        step_name="root_agent.web_search_exa",
        stage="pre",
        step_input=paraphrased,
    )

    assert response.is_safe is True, "paraphrase is not caught, and the plan says so"
