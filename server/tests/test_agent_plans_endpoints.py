"""Coverage for the declared plan: ``/agent-sessions/{key}/plan`` and its steps.

Everything this resource stores is a claim by the agent, so almost every
assertion below is about the difference between recording a claim and endorsing
it. The two failure modes worth guarding are opposite in shape: a refusal that
quietly wrote half of something, and a response that hands a console the
arithmetic for a percentage nobody measured.

What is pinned:

* a session with no plan answers 200 with ``plan: null`` - the fallback view's
  input, and not a 404, because never declaring a plan is the ordinary case;
* a re-declaration is a **new revision**, the old revision survives in the
  table, and ``revision_count`` is what lets a console say "revised";
* marking step 7 of a three-step plan is a 422 that writes *nothing*, asserted
  by reading every step back afterwards rather than by trusting the status code;
* naming a superseded revision is a 409 that also writes nothing, and the 409
  names the current revision so the agent can correct itself;
* a token minted for session A cannot write session B's plan, which is the
  whole authorization design and is exercised against the real routes with a
  real signed token;
* no namespace can see, declare or mark another namespace's plan;
* **no percentage exists anywhere in any response**, and none can be derived:
  asserted over the whole serialized payload, not over a field list somebody
  would have to remember to update;
* an abandoned plan keeps its steps ``pending`` and its timestamps still, so a
  console reads staleness rather than inferring progress.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from agent_control_models.plans import PLAN_MAX_REVISIONS, PLAN_MAX_STEPS
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import LocalJwtVerifyProvider
from agent_control_server.services.agent_sessions import mint_session_runtime_token
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from .test_agent_sessions_auth import DenyingAuthorizer
from .test_agent_sessions_endpoints import (
    HeaderNamespaceAuthorizer,
    _agent_name,
    _bind,
    _namespace_client,
    _open_session,
    _register_agent,
    executor_enabled,  # noqa: F401 - fixture
    fake_executor,  # noqa: F401 - fixture
)

_SESSIONS_URL = "/api/v1/agent-sessions"
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"

pytestmark = pytest.mark.usefixtures("executor_enabled", "fake_executor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(client: TestClient) -> str:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    return str(_open_session(client, agent_name)["session_key"])


class _Agent:
    """A client that writes plans the way an executor does: one session, one token.

    The token is minted per request for the session named in the path, so
    ``token_for`` is the only way a test can produce the cross-session attempt -
    and the refusal it gets back comes from the real verifier rather than from
    a check this file wrote.
    """

    def __init__(self, app: FastAPI) -> None:
        self._client = TestClient(app, raise_server_exceptions=True)

    def _headers(self, session_key: str, *, token_for: str | None = None) -> dict[str, str]:
        minted = mint_session_runtime_token(
            namespace_key="default",
            session_key=token_for or session_key,
            actor_id="0123456789abcdef",
        )
        assert minted is not None
        return {"Authorization": f"Bearer {minted[0]}"}

    def declare(
        self,
        session_key: str,
        steps: list[str],
        *,
        token_for: str | None = None,
    ) -> Any:
        return self._client.put(
            f"{_SESSIONS_URL}/{session_key}/plan",
            json={"steps": steps},
            headers=self._headers(session_key, token_for=token_for),
        )

    def mark(
        self,
        session_key: str,
        revision: int,
        index: int,
        status: str,
        *,
        note: str | None = None,
        token_for: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"status": status}
        if note is not None:
            body["note"] = note
        return self._client.patch(
            f"{_SESSIONS_URL}/{session_key}/plan/revisions/{revision}/steps/{index}",
            json=body,
            headers=self._headers(session_key, token_for=token_for),
        )


@pytest.fixture()
def agent(app: FastAPI) -> Any:
    """The machine half, wired to the real runtime-token verifier.

    Installed for ``agent_plans.write`` only, so a test that reaches for the
    human read route with an API key still goes through the ordinary
    authorizer. That separation is the thing under test, not scaffolding.
    """
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_PLANS_WRITE,
    )
    yield _Agent(app)
    set_runtime_auth_config(None)


def _read(client: TestClient, session_key: str) -> dict[str, Any]:
    resp = client.get(f"{_SESSIONS_URL}/{session_key}/plan")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _plan(client: TestClient, session_key: str) -> dict[str, Any] | None:
    plan = _read(client, session_key)["plan"]
    return dict(plan) if plan is not None else None


def _rows(db_engine: Any, session_key: str) -> list[tuple[Any, ...]]:
    """Every plan row for a session, straight from the table.

    Read past the API on purpose: the endpoint returns only the current
    revision, and "the superseded revision is still there" is a claim about the
    table.
    """
    with db_engine.begin() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT p.plan_revision, p.step_index, p.title, p.status, p.note "
                    "  FROM agent_session_plan_steps p "
                    "  JOIN agent_sessions s ON s.id = p.session_id "
                    "   AND s.namespace_key = p.namespace_key "
                    " WHERE s.session_key = :key "
                    " ORDER BY p.plan_revision, p.step_index"
                ),
                {"key": session_key},
            ).fetchall()
        )


# ---------------------------------------------------------------------------
# No plan is an answer, not an error
# ---------------------------------------------------------------------------


def test_a_session_whose_agent_never_declared_a_plan_answers_null(
    client: TestClient,
) -> None:
    """The input to the fallback view, and the reason it is not a 404.

    Most agents never call ``declare_plan``. Answering 404 would make the
    ordinary case indistinguishable from a session that does not exist, and a
    console cannot tell "nothing was reported" from "nothing is there" if the
    server will not.
    """
    session_key = _session(client)

    body = _read(client, session_key)

    assert body == {"session_key": session_key, "plan": None}


def test_reading_the_plan_of_an_unknown_session_is_a_404(client: TestClient) -> None:
    resp = client.get(f"{_SESSIONS_URL}/{uuid.uuid4().hex}/plan")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Declaring, and re-declaring
# ---------------------------------------------------------------------------


def test_a_declared_plan_comes_back_in_the_order_the_agent_wrote_it(
    client: TestClient, agent: _Agent
) -> None:
    """Order and index are the agent's, and the server does not reorder them.

    The index is how a later ``mark_step`` addresses a step, so a server that
    sorted these by anything of its own would move the target out from under an
    update already in flight.
    """
    session_key = _session(client)

    declared = agent.declare(
        session_key, ["Read the ticket", "Check the policy", "Draft the reply"]
    )
    assert declared.status_code == 200, declared.text

    plan = declared.json()["plan"]
    assert plan["revision"] == 1
    assert plan["revision_count"] == 1
    assert [(s["index"], s["title"]) for s in plan["steps"]] == [
        (0, "Read the ticket"),
        (1, "Check the policy"),
        (2, "Draft the reply"),
    ]
    assert {s["status"] for s in plan["steps"]} == {"pending"}
    assert all(s["note"] is None for s in plan["steps"])
    # And the human read agrees with what the write returned.
    assert _plan(client, session_key) == plan


def test_a_replan_writes_a_new_revision_and_keeps_the_old_one(
    client: TestClient, agent: _Agent, db_engine: Any
) -> None:
    """The claim the "plan revised" line in the UI rests on.

    Two separate facts: the read shows the *new* steps at revision 2, and the
    superseded revision is still in the table. Overwriting revision 1 would let
    a console show a person different steps than the ones they read a minute
    ago with nothing saying why, and would land any in-flight step update on a
    plan the agent had already replaced.
    """
    session_key = _session(client)
    assert agent.declare(session_key, ["Old one", "Old two"]).status_code == 200

    replanned = agent.declare(session_key, ["New one", "New two", "New three"])
    assert replanned.status_code == 200, replanned.text

    plan = replanned.json()["plan"]
    assert plan["revision"] == 2
    assert plan["revision_count"] == 2
    assert [s["title"] for s in plan["steps"]] == ["New one", "New two", "New three"]

    revisions = {row[0] for row in _rows(db_engine, session_key)}
    assert revisions == {1, 2}, "the superseded revision must survive the replan"
    assert [row[2] for row in _rows(db_engine, session_key) if row[0] == 1] == [
        "Old one",
        "Old two",
    ]


def test_a_replan_does_not_carry_the_previous_revisions_marks_forward(
    client: TestClient, agent: _Agent
) -> None:
    """A new plan starts unmarked, because nobody said the new steps were done.

    Copying statuses across by index would tick steps of a plan the agent has
    not yet touched, which is the same lie as a percentage: a mark with no
    author.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A", "B"])
    assert agent.mark(session_key, 1, 0, "done").status_code == 200

    agent.declare(session_key, ["A", "B"])

    plan = _plan(client, session_key)
    assert plan is not None
    assert [s["status"] for s in plan["steps"]] == ["pending", "pending"]


def test_an_empty_plan_and_an_over_long_plan_are_both_refused(
    client: TestClient, agent: _Agent, db_engine: Any
) -> None:
    """The ceiling is refused whole rather than truncated.

    A silently shortened plan is a plan whose last step can never be marked,
    and the agent would have no way to find that out.
    """
    session_key = _session(client)

    assert agent.declare(session_key, []).status_code == 422
    over = agent.declare(session_key, [f"step {i}" for i in range(PLAN_MAX_STEPS + 1)])
    assert over.status_code == 422, over.text

    assert _rows(db_engine, session_key) == []
    assert _plan(client, session_key) is None


def test_the_plan_ceiling_refuses_the_next_revision_and_leaves_the_last_one_standing(
    client: TestClient, agent: _Agent
) -> None:
    """An agent replanning in a loop stops writing rows; it does not lose its plan.

    The refusal is a 429 rather than a 400 because it is about volume, and the
    plan already recorded stays readable - a ceiling that wiped the record
    would punish the operator for the agent's behaviour.
    """
    session_key = _session(client)
    for revision in range(1, PLAN_MAX_REVISIONS + 1):
        resp = agent.declare(session_key, [f"revision {revision}"])
        assert resp.status_code == 200, resp.text

    refused = agent.declare(session_key, ["one too many"])
    assert refused.status_code == 429, refused.text
    assert refused.json()["error_code"] == "QUOTA_EXCEEDED"

    plan = _plan(client, session_key)
    assert plan is not None
    assert plan["revision"] == PLAN_MAX_REVISIONS
    assert [s["title"] for s in plan["steps"]] == [f"revision {PLAN_MAX_REVISIONS}"]


# ---------------------------------------------------------------------------
# Marking steps
# ---------------------------------------------------------------------------


def test_marking_a_step_records_the_status_and_the_note_the_agent_wrote(
    client: TestClient, agent: _Agent
) -> None:
    session_key = _session(client)
    agent.declare(session_key, ["Read the ticket", "Draft the reply"])

    marked = agent.mark(session_key, 1, 0, "done", note="found it in the archive")
    assert marked.status_code == 200, marked.text

    steps = marked.json()["plan"]["steps"]
    assert steps[0]["status"] == "done"
    assert steps[0]["note"] == "found it in the archive"
    assert steps[1]["status"] == "pending"
    assert steps[1]["note"] is None


def test_a_failed_step_stays_failed_and_is_never_folded_into_done(
    client: TestClient, agent: _Agent
) -> None:
    """``failed`` and ``skipped`` are their own statuses on the way out too.

    Collapsing either into "not pending" would let a plan read as finished when
    a third of it was abandoned, which is a percentage arriving by a slower
    route.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A", "B", "C"])

    agent.mark(session_key, 1, 0, "done")
    agent.mark(session_key, 1, 1, "failed", note="the API refused")
    agent.mark(session_key, 1, 2, "skipped")

    plan = _plan(client, session_key)
    assert plan is not None
    assert [s["status"] for s in plan["steps"]] == ["done", "failed", "skipped"]


def test_marking_a_step_twice_replaces_the_status_and_keeps_the_last_note(
    client: TestClient, agent: _Agent
) -> None:
    """A step is a current claim, not an append-only log of claims."""
    session_key = _session(client)
    agent.declare(session_key, ["A"])

    agent.mark(session_key, 1, 0, "active", note="started")
    agent.mark(session_key, 1, 0, "done")

    plan = _plan(client, session_key)
    assert plan is not None
    # No note in the second call, so the note it carried is left alone rather
    # than blanked: an update that named only a status said nothing about it.
    assert plan["steps"][0] == {
        **plan["steps"][0],
        "status": "done",
        "note": "started",
    }


def test_an_unknown_status_is_refused_before_it_reaches_the_column(
    client: TestClient, agent: _Agent
) -> None:
    session_key = _session(client)
    agent.declare(session_key, ["A"])

    resp = agent.mark(session_key, 1, 0, "nearly-done")

    assert resp.status_code == 422, resp.text
    plan = _plan(client, session_key)
    assert plan is not None
    assert plan["steps"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# The two refusals, and the absence of a partial write behind each
# ---------------------------------------------------------------------------


def test_marking_step_seven_of_a_three_step_plan_is_a_422_that_writes_nothing(
    client: TestClient, agent: _Agent, db_engine: Any
) -> None:
    """The absence is the assertion, so it is read back from the table.

    A 422 is easy to return and easy to return *after* touching a neighbouring
    row. Every step is read afterwards, at the API and in the database, because
    "nothing was written" is the promise the refusal makes.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A", "B", "C"])
    before = _rows(db_engine, session_key)

    refused = agent.mark(session_key, 1, 7, "done", note="marking thin air")

    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert body["error_code"] == "PLAN_STEP_OUT_OF_RANGE"
    # The refusal has to teach the agent the shape of its own plan, or it will
    # simply try step 7 again.
    assert "3 steps" in body["detail"]
    assert _rows(db_engine, session_key) == before
    plan = _plan(client, session_key)
    assert plan is not None
    assert [s["status"] for s in plan["steps"]] == ["pending", "pending", "pending"]
    assert all(s["note"] is None for s in plan["steps"])


def test_a_negative_step_index_is_refused_by_the_route(
    client: TestClient, agent: _Agent
) -> None:
    """Step indexes are 0-based and dense, so ``-1`` is not "the last one"."""
    session_key = _session(client)
    agent.declare(session_key, ["A", "B"])

    assert agent.mark(session_key, 1, -1, "done").status_code == 422


def test_marking_a_step_of_a_superseded_revision_is_a_409_that_writes_nothing(
    client: TestClient, agent: _Agent, db_engine: Any
) -> None:
    """The race the revision exists for, and the refusal that resolves it.

    The agent replanned while it believed it was still working revision 1.
    Applying the update to "the latest plan" would mark a step of the *new*
    plan done because a step of the *old* one finished, which is a tick against
    work nobody did.
    """
    session_key = _session(client)
    agent.declare(session_key, ["Old A", "Old B"])
    agent.declare(session_key, ["New A", "New B"])
    before = _rows(db_engine, session_key)

    refused = agent.mark(session_key, 1, 0, "done")

    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["error_code"] == "PLAN_REVISION_STALE"
    # The current revision is named, so the agent can retry against the right
    # plan instead of guessing.
    assert "2" in body["detail"]
    assert _rows(db_engine, session_key) == before
    plan = _plan(client, session_key)
    assert plan is not None
    assert [s["status"] for s in plan["steps"]] == ["pending", "pending"]


def test_naming_a_revision_that_never_existed_is_also_refused_without_writing(
    client: TestClient, agent: _Agent, db_engine: Any
) -> None:
    """A revision from the future is as stale as one from the past.

    Both mean "this update is not about the plan that is recorded", and both
    have to be refused rather than clamped to the nearest real revision.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A"])
    before = _rows(db_engine, session_key)

    refused = agent.mark(session_key, 9, 0, "done")

    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "PLAN_REVISION_STALE"
    assert _rows(db_engine, session_key) == before


def test_marking_a_step_of_a_session_with_no_plan_is_a_typed_404(
    client: TestClient, agent: _Agent
) -> None:
    """Not "stale revision", because nothing was superseded.

    An agent told its plan moved would go and re-read a plan that does not
    exist. Told there is no plan, it declares one.
    """
    session_key = _session(client)

    refused = agent.mark(session_key, 1, 0, "done")

    assert refused.status_code == 404, refused.text
    assert refused.json()["error_code"] == "PLAN_NOT_FOUND"


# ---------------------------------------------------------------------------
# Whose plan this is: the session-bound token
# ---------------------------------------------------------------------------


def test_a_token_minted_for_one_session_cannot_write_another_sessions_plan(
    client: TestClient, agent: _Agent, db_engine: Any
) -> None:
    """The entire authorization design, exercised against the real verifier.

    An executor holds a token for the session it is running. If that token
    could address another session, one agent could rewrite another agent's
    account of its own work - and the rail's label would be naming the wrong
    author.
    """
    victim = _session(client)
    attacker = _session(client)
    agent.declare(victim, ["Victim's own step"])

    refused_declare = agent.declare(victim, ["Injected plan"], token_for=attacker)
    assert refused_declare.status_code == 403, refused_declare.text

    refused_mark = agent.mark(victim, 1, 0, "done", token_for=attacker)
    assert refused_mark.status_code == 403, refused_mark.text

    assert [row[0:4] for row in _rows(db_engine, victim)] == [
        (1, 0, "Victim's own step", "pending")
    ]


def test_writing_a_plan_with_no_credential_at_all_is_refused(
    client: TestClient, app: FastAPI, agent: _Agent
) -> None:
    session_key = _session(client)
    anonymous = TestClient(app, raise_server_exceptions=True)

    resp = anonymous.put(f"{_SESSIONS_URL}/{session_key}/plan", json={"steps": ["A"]})

    assert resp.status_code in (401, 403), resp.text


def test_an_ordinary_api_key_cannot_write_a_plan_when_no_token_path_is_configured(
    client: TestClient, non_admin_client: TestClient
) -> None:
    """The fail-closed fallback, with no runtime provider installed.

    A deployment with no runtime token secret falls back to the header
    provider, where ``agent_plans.write`` is ADMIN rather than AUTHENTICATED.
    Any-authenticated-key would let one tenant's agent rewrite another
    session's plan, which is the hole the session binding exists to close.
    """
    session_key = _session(client)

    resp = non_admin_client.put(
        f"{_SESSIONS_URL}/{session_key}/plan", json={"steps": ["A"]}
    )

    assert resp.status_code == 403, resp.text


def test_reading_the_plan_needs_content_access_and_not_merely_session_read(
    client: TestClient, agent: _Agent
) -> None:
    """Step titles are model-authored text about the conversation.

    Handing them to a caller refused the transcript would be the inventory
    disclosure the operation split exists to prevent, one step title at a time.
    """
    session_key = _session(client)
    agent.declare(session_key, ["Email the customer about the refund"])

    authorizer = DenyingAuthorizer(Operation.AGENT_SESSION_CONTENT_READ)
    set_authorizer(authorizer)

    refused = client.get(f"{_SESSIONS_URL}/{session_key}/plan")

    assert refused.status_code == 403, refused.text
    assert Operation.AGENT_SESSION_CONTENT_READ in authorizer.seen
    assert "Email the customer" not in refused.text


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


def test_no_namespace_can_read_declare_or_mark_another_namespaces_plan(
    app: FastAPI, db_engine: Any
) -> None:
    """Every plan path, not just the read.

    A 404 rather than a 403 on the cross-namespace read, because the existence
    of a session in another tenant is itself not this caller's business.
    """
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")
    try:
        agent_name = _agent_name()
        _register_agent(alpha, agent_name)
        _bind(alpha, agent_name)
        session_key = str(_open_session(alpha, agent_name)["session_key"])

        # Declared inside alpha, through the same header authorizer.
        declared = alpha.put(
            f"{_SESSIONS_URL}/{session_key}/plan", json={"steps": ["Alpha's step"]}
        )
        assert declared.status_code == 200, declared.text

        assert beta.get(f"{_SESSIONS_URL}/{session_key}/plan").status_code == 404
        assert (
            beta.put(
                f"{_SESSIONS_URL}/{session_key}/plan", json={"steps": ["Beta's step"]}
            ).status_code
            == 404
        )
        assert (
            beta.patch(
                f"{_SESSIONS_URL}/{session_key}/plan/revisions/1/steps/0",
                json={"status": "done"},
            ).status_code
            == 404
        )

        # And alpha's row is exactly as alpha left it.
        assert [row[0:4] for row in _rows(db_engine, session_key)] == [
            (1, 0, "Alpha's step", "pending")
        ]
    finally:
        set_runtime_auth_config(None)


def test_the_same_session_key_in_two_namespaces_holds_two_separate_plans(
    app: FastAPI
) -> None:
    """Session keys are unique per namespace, so the plan read must scope too.

    A read that matched on ``session_key`` alone would hand one tenant the
    other's steps whenever the keys collided.
    """
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")

    agent_name = _agent_name()
    for client in (alpha, beta):
        _register_agent(client, agent_name)
        _bind(client, agent_name)

    alpha_key = str(_open_session(alpha, agent_name)["session_key"])
    beta_key = str(_open_session(beta, agent_name)["session_key"])
    alpha.put(f"{_SESSIONS_URL}/{alpha_key}/plan", json={"steps": ["Alpha only"]})
    beta.put(f"{_SESSIONS_URL}/{beta_key}/plan", json={"steps": ["Beta only"]})

    assert [
        s["title"] for s in alpha.get(f"{_SESSIONS_URL}/{alpha_key}/plan").json()["plan"]["steps"]
    ] == ["Alpha only"]
    assert [
        s["title"] for s in beta.get(f"{_SESSIONS_URL}/{beta_key}/plan").json()["plan"]["steps"]
    ] == ["Beta only"]


# ---------------------------------------------------------------------------
# The number that must not exist
# ---------------------------------------------------------------------------


_PERCENTAGE_WORDS = re.compile(
    r"percent|pct|progress|completion|complete_?ratio|ratio|fraction|"
    r"steps_done|done_count|remaining",
    re.IGNORECASE,
)


def _keys_of(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        found = set(payload)
        for value in payload.values():
            found |= _keys_of(value)
        return found
    if isinstance(payload, list):
        found: set[str] = set()
        for item in payload:
            found |= _keys_of(item)
        return found
    return set()


@pytest.mark.parametrize("marked", [0, 1, 2, 3])
def test_no_response_carries_a_completion_number_at_any_stage(
    client: TestClient, agent: _Agent, marked: int
) -> None:
    """Asserted over the whole payload, at four different amounts of progress.

    Over the serialized keys rather than an allow-list of fields, because the
    failure this guards against is somebody adding ``percent_complete`` in six
    months and nobody remembering there was a list to update. Parameterised
    across a plan with nothing, some and everything marked, since a summary
    field could plausibly appear only once something is done.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A", "B", "C"])
    for index in range(marked):
        assert agent.mark(session_key, 1, index, "done").status_code == 200

    body = _read(client, session_key)
    keys = _keys_of(body)

    offenders = {key for key in keys if _PERCENTAGE_WORDS.search(key)}
    assert offenders == set(), (
        "a plan response must carry no completion figure and nothing a console "
        f"could render as one; found {sorted(offenders)}"
    )
    # No bare number in the payload either, beyond the indexes and revisions
    # that address a step. A float would have to be a ratio.
    assert not any(
        isinstance(value, float)
        for value in json.loads(json.dumps(body)).get("plan", {}).values()
    )


def test_the_response_carries_no_step_count_a_console_could_divide_by(
    client: TestClient, agent: _Agent
) -> None:
    """``steps`` is a list, and a list is not an arithmetic invitation.

    The moment the server ships ``done`` and ``total`` beside each other, the
    quotient is one line of client code away, and the label "reported by the
    agent" is attached to a measurement.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A", "B"])
    agent.mark(session_key, 1, 0, "done")

    plan = _plan(client, session_key)

    assert plan is not None
    assert set(plan) == {
        "session_key",
        "revision",
        "revision_count",
        "steps",
        "declared_at",
        "last_updated_at",
    }


# ---------------------------------------------------------------------------
# Staleness, which is a time and never an inference
# ---------------------------------------------------------------------------


def test_an_abandoned_plan_keeps_its_steps_pending_and_its_clock_still(
    client: TestClient, agent: _Agent
) -> None:
    """Nothing here decays, completes itself, or ages towards done.

    An agent that walked away leaves ``last_updated_at`` where it was, which is
    the one fact a console can honestly render: this plan is old. Not "stalled
    at 40%", which asserts both a position and a stall.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A", "B", "C"])

    first = _plan(client, session_key)
    assert first is not None
    assert first["last_updated_at"] == first["declared_at"]

    later = _plan(client, session_key)
    assert later is not None
    assert later["last_updated_at"] == first["last_updated_at"]
    assert [s["status"] for s in later["steps"]] == ["pending"] * 3
    assert [s["updated_at"] for s in later["steps"]] == [
        s["updated_at"] for s in first["steps"]
    ]


def test_marking_a_step_moves_that_steps_clock_and_the_plans_but_not_the_declaration(
    client: TestClient, agent: _Agent
) -> None:
    """``declared_at`` is a fact about the past and must not drift forward.

    Derived from the earliest ``updated_at`` it would be right until the last
    step was marked and then quietly report a declaration later than it was.
    """
    session_key = _session(client)
    agent.declare(session_key, ["A", "B"])
    before = _plan(client, session_key)
    assert before is not None

    assert agent.mark(session_key, 1, 1, "active").status_code == 200

    after = _plan(client, session_key)
    assert after is not None
    assert after["declared_at"] == before["declared_at"]
    assert after["last_updated_at"] >= before["last_updated_at"]
    assert after["steps"][0]["updated_at"] == before["steps"][0]["updated_at"]
    assert after["steps"][1]["updated_at"] >= before["steps"][1]["updated_at"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_deleting_the_session_takes_its_plan_with_it(
    client: TestClient, agent: _Agent, db_engine: Any
) -> None:
    """An orphaned plan is a plan for a conversation nobody can read."""
    session_key = _session(client)
    agent.declare(session_key, ["A", "B"])

    deleted = client.delete(f"{_SESSIONS_URL}/{session_key}")
    assert deleted.status_code == 200, deleted.text

    assert _rows(db_engine, session_key) == []


def test_a_plan_body_never_appears_in_log_output_above_debug(
    client: TestClient, agent: _Agent, caplog: pytest.LogCaptureFixture
) -> None:
    """Step titles are content, and content is not logged.

    A plan says what an agent is doing about somebody's ticket. It belongs in
    the transcript view and nowhere near an aggregated log stream.
    """
    session_key = _session(client)
    secret = "cancel the invoice for Acme Holdings"

    with caplog.at_level("INFO"):
        assert agent.declare(session_key, [secret]).status_code == 200
        assert agent.mark(session_key, 1, 0, "done", note=secret).status_code == 200

    assert secret not in caplog.text
