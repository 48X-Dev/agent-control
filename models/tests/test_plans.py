"""The wire contract for a declared plan.

These models are the shared boundary: the server validates against them, both
SDKs are generated from them, and a console renders whatever they carry. So the
assertions worth having are the ones that hold a boundary, not the ones that
restate a field list.

The one that matters most is an absence. There is no percentage field anywhere
in this module, and there is no pair of fields a client could divide to make
one. That is asserted over the JSON schema rather than over a list of names
somebody would have to remember to update, because the failure this guards
against is a `percent_complete` added in six months by someone who never read
the docstring explaining why it must not exist.

The rest:

* request models forbid unknown fields, so an agent cannot smuggle a status or
  a revision through a body that is only supposed to carry steps;
* ``skipped`` and ``failed`` are their own statuses and neither is a flavour of
  ``done`` - collapsing them would let a plan read as finished when a third of
  it was abandoned;
* the step ceiling is enforced by the model, so an over-long plan is refused
  whole rather than silently truncated to a plan whose last step can never be
  marked;
* ``plan`` is nullable on the response, because never declaring one is the
  ordinary case and not an error.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from agent_control_models import (
    PLAN_MAX_STEPS,
    PLAN_NOTE_MAX_LENGTH,
    PLAN_STEP_TITLE_MAX_LENGTH,
    DeclarePlanRequest,
    Plan,
    PlanResponse,
    PlanStep,
    PlanStepStatus,
    UpdatePlanStepRequest,
)
from pydantic import ValidationError

_NOW = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)


def _step(index: int = 0, status: PlanStepStatus = PlanStepStatus.PENDING) -> PlanStep:
    return PlanStep(index=index, title=f"step {index}", status=status, note=None, updated_at=_NOW)


# ---------------------------------------------------------------------------
# The number that does not exist
# ---------------------------------------------------------------------------


_COMPLETION_WORDS = re.compile(
    r"percent|pct|progress|completion|ratio|fraction|done_count|steps_done|"
    r"remaining|elapsed_fraction",
    re.IGNORECASE,
)


def _property_names(schema: dict) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                found |= set(value)
            if isinstance(value, dict | list):
                found |= _property_names(value)  # type: ignore[arg-type]
    elif isinstance(schema, list):
        for item in schema:
            found |= _property_names(item)
    return found


@pytest.mark.parametrize(
    "model", [Plan, PlanStep, PlanResponse, DeclarePlanRequest, UpdatePlanStepRequest]
)
def test_no_plan_model_carries_a_completion_figure(model: type) -> None:
    """Over the generated schema, so a new field is caught wherever it is added.

    A percentage here would be laundering: every number in this module has an
    author, and a completion figure would be the one number nobody measured
    wearing the same label as the ones somebody claimed.
    """
    schema = model.model_json_schema()

    offenders = {n for n in _property_names(schema) if _COMPLETION_WORDS.search(n)}

    assert offenders == set(), f"{model.__name__} must carry no completion figure"


def test_a_plan_carries_no_total_beside_its_marks() -> None:
    """Not even the raw materials: no ``total``, no ``done``, just the steps.

    The moment a payload ships two counts, the quotient is one line of client
    code away and the rail's label ends up attached to arithmetic.
    """
    plan = Plan(
        session_key="sess-1",
        revision=1,
        revision_count=1,
        steps=[_step(0, PlanStepStatus.DONE), _step(1)],
        declared_at=_NOW,
        last_updated_at=_NOW,
    )

    payload = json.loads(plan.model_dump_json())

    assert set(payload) == {
        "session_key",
        "revision",
        "revision_count",
        "steps",
        "declared_at",
        "last_updated_at",
    }
    assert not any(isinstance(value, float) for value in payload.values())


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


def test_skipped_and_failed_are_their_own_statuses() -> None:
    """Neither collapses into ``done``, at the type level or on the wire."""
    assert set(PlanStepStatus) == {
        PlanStepStatus.PENDING,
        PlanStepStatus.ACTIVE,
        PlanStepStatus.DONE,
        PlanStepStatus.SKIPPED,
        PlanStepStatus.FAILED,
    }
    assert PlanStepStatus.SKIPPED != PlanStepStatus.DONE
    assert PlanStepStatus.FAILED != PlanStepStatus.DONE
    assert json.loads(_step(0, PlanStepStatus.FAILED).model_dump_json())["status"] == ("failed")


def test_an_invented_status_is_refused() -> None:
    with pytest.raises(ValidationError):
        UpdatePlanStepRequest(status="nearly-done")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Request boundaries
# ---------------------------------------------------------------------------


def test_declaring_a_plan_forbids_unknown_fields() -> None:
    """An agent must not be able to name its own revision in the body.

    The server allocates revisions; a body field would let a replan overwrite
    the revision a person is currently reading.
    """
    with pytest.raises(ValidationError):
        DeclarePlanRequest(steps=["a"], plan_revision=7)  # type: ignore[call-arg]


def test_updating_a_step_forbids_unknown_fields() -> None:
    """Including the two that live in the path.

    Revision and index address the resource. Accepting them in the body too
    would give one request two ways to name a step and one way to disagree with
    itself.
    """
    with pytest.raises(ValidationError):
        UpdatePlanStepRequest(status="done", step_index=3)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        UpdatePlanStepRequest(status="done", plan_revision=1)  # type: ignore[call-arg]


def test_a_plan_needs_at_least_one_step_and_no_more_than_the_ceiling() -> None:
    """Refused whole at both ends rather than padded or truncated."""
    with pytest.raises(ValidationError):
        DeclarePlanRequest(steps=[])

    ok = DeclarePlanRequest(steps=[f"step {i}" for i in range(PLAN_MAX_STEPS)])
    assert len(ok.steps) == PLAN_MAX_STEPS

    with pytest.raises(ValidationError):
        DeclarePlanRequest(steps=[f"step {i}" for i in range(PLAN_MAX_STEPS + 1)])


def test_an_empty_step_title_and_an_over_long_one_are_both_refused() -> None:
    with pytest.raises(ValidationError):
        DeclarePlanRequest(steps=[""])
    with pytest.raises(ValidationError):
        DeclarePlanRequest(steps=["x" * (PLAN_STEP_TITLE_MAX_LENGTH + 1)])

    assert DeclarePlanRequest(steps=["x" * PLAN_STEP_TITLE_MAX_LENGTH]).steps


def test_an_over_long_note_is_refused_and_an_absent_one_is_fine() -> None:
    """Absent is not the same as empty: one leaves the note alone, one is noise."""
    assert UpdatePlanStepRequest(status="done").note is None
    with pytest.raises(ValidationError):
        UpdatePlanStepRequest(status="done", note="x" * (PLAN_NOTE_MAX_LENGTH + 1))
    with pytest.raises(ValidationError):
        UpdatePlanStepRequest(status="done", note="")


def test_a_step_index_is_zero_based_and_never_negative() -> None:
    with pytest.raises(ValidationError):
        PlanStep(index=-1, title="t", status=PlanStepStatus.PENDING, updated_at=_NOW)


def test_a_revision_starts_at_one() -> None:
    with pytest.raises(ValidationError):
        Plan(
            session_key="s",
            revision=0,
            revision_count=1,
            steps=[],
            declared_at=_NOW,
            last_updated_at=_NOW,
        )


# ---------------------------------------------------------------------------
# No plan is an answer
# ---------------------------------------------------------------------------


def test_the_response_carries_a_null_plan_rather_than_omitting_it() -> None:
    """The fallback view's input.

    Null says "the agent reported nothing", which a console can render. An
    absent key says nothing at all, and a client would have to guess whether
    the read succeeded.
    """
    payload = json.loads(PlanResponse(session_key="sess-1").model_dump_json())

    assert payload == {"session_key": "sess-1", "plan": None}
    assert PlanResponse.model_json_schema()["properties"]["plan"] is not None
