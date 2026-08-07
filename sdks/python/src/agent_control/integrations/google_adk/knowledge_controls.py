"""The three controls that ship with the knowledge tools, in valid schema.

Definitions rather than prose, for the lesson that a plan named and this repo
has now paid for twice: a control an operator cannot author is a control that
fails in CI's absence rather than in CI. These compile through
``ControlDefinitionRuntime.model_validate`` and are behaviour-tested against
the real evaluators, because a valid config can still be a wrong control.

Only the first is meant to be bound by default. The other two are examples an
operator turns on knowingly, and each says what it does and does not catch.

**Every step name here is agent-qualified.** ``root_agent.company_knowledge_search``,
never the bare ``company_knowledge_search``. ``get_applicable_controls``
filters on the name inside its ``step_names`` branch and warns about nothing
when the filter matches nothing, so a bare name is a control that silently does
not exist. Rename the prefix to match your own agent.
"""

from __future__ import annotations

from typing import Any

SEARCH_STEP_NAME = "root_agent.company_knowledge_search"
RECENT_STEP_NAME = "root_agent.company_knowledge_recent"
WEB_STEP_NAMES = ["root_agent.web_search_exa", "root_agent.web_fetch_exa"]

KNOWLEDGE_STEP_NAMES = [SEARCH_STEP_NAME, RECENT_STEP_NAME]

OBSERVE_REFUSAL = "knowledge-observe-refusal"
DENY_EXTERNAL_AUTHOR = "knowledge-deny-external-author-snippets"
DENY_FENCE_IN_WEB_ARGS = "knowledge-deny-fence-in-web-args"

_REFUSAL_CODES = (
    "query_too_short|query_too_long|rate_limited|"
    "knowledge_unavailable|knowledge_disabled|corpus_empty"
)

KNOWLEDGE_CONTROLS: dict[str, dict[str, Any]] = {
    OBSERVE_REFUSAL: {
        "description": (
            "Record every knowledge search that did not run, with the reason. "
            "Bound by default: it denies nothing and it is the difference "
            "between 'search finds nothing' and a corpus nobody noticed was "
            "unreachable."
        ),
        "enabled": True,
        "execution": "server",
        # Deliberately unscoped by step name. The codes only appear on this
        # tool's output, and an operator who renames the agent should not lose
        # their only signal that the corpus stopped answering.
        "scope": {"step_types": ["tool"], "stages": ["post"]},
        "action": {"decision": "observe"},
        "condition": {
            "selector": {"path": "output.refusal_code"},
            "evaluator": {
                "name": "regex",
                "config": {"pattern": f"^({_REFUSAL_CODES})$"},
            },
        },
    },
    DENY_EXTERNAL_AUTHOR: {
        "description": (
            "Example, unbound by default. Refuse a result set containing a "
            "snippet nobody in the workspace wrote."
        ),
        "enabled": True,
        "execution": "server",
        "scope": {
            "step_types": ["tool"],
            "step_names": KNOWLEDGE_STEP_NAMES,
            "stages": ["post"],
        },
        "action": {"decision": "deny"},
        # The selector is the whole output object, and that is the correction
        # this control exists to carry. Selecting the bare integer
        # ``output.external_author_count`` into the json evaluator denies
        # *every* search including a count of zero: _parse_json answers
        # "Unsupported data type: int" for a scalar, and allow_invalid_json
        # defaults False, which turns that parse error into matched=True. A
        # deny that fires on zero is a deny the first inconvenienced operator
        # switches off.
        #
        # With the object selected, a useful property falls out: a missing key
        # is "field not found", which is also matched=True, so this fails
        # closed if the tool ever stops emitting the field.
        "condition": {
            "selector": {"path": "output"},
            "evaluator": {
                "name": "json",
                "config": {"field_constraints": {"external_author_count": {"max": 0}}},
            },
        },
    },
    DENY_FENCE_IN_WEB_ARGS: {
        "description": (
            "Example, unbound by default. Refuse a web-tool argument carrying "
            "a knowledge fence marker. Catches whole-block copy-paste out of "
            "the corpus and only that: a model that paraphrases steps around "
            "it, which is why co-provisioning retrieval with a free-form "
            "outbound tool is the decision that actually matters."
        ),
        "enabled": True,
        "execution": "server",
        "scope": {
            "step_types": ["tool"],
            "step_names": WEB_STEP_NAMES,
            "stages": ["pre"],
        },
        "action": {"decision": "deny"},
        "condition": {
            "selector": {"path": "input"},
            "evaluator": {"name": "regex", "config": {"pattern": "<<<KNOWLEDGE_"}},
        },
    },
}
