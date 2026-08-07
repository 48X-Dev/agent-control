"""The ceiling on how often a session may read the corpus, and what it costs.

Six searches a minute is the enforceable approximation of "per turn": the
server cannot see a turn boundary from a tool call, and the number bounds the
same runaway an injected search loop would produce. What makes it a ceiling
rather than a suggestion is the key, and the key is never the path. Under the
runtime-token provider the window spends the token's own verified binding, so
each session gets its own and no caller can pick which. Under the header key
and under ``NoAuthProvider`` nothing is verified, so there is one bucket per
namespace - narrower than it sounds is not the trade here, because a window
keyed on a segment the caller types is a window a loop refreshes for free.

Three things are asserted here that a test of the number alone would miss:

* **what does not spend it.** A query the bounds refuse costs nothing to
  answer, and spending a search from the window on it would spend the ceiling
  on the one call that was never going to reach the database. Order is the
  design, so order is tested.
* **that the number in the refusal is true.** ``retry_after_seconds`` says when
  the window reopens. A fixed guess would pass a test that only checked the
  field was present, so the clock is moved by exactly that many seconds.
* **that it is a second bucket.** A search must not spend the namespace's turn
  allowance, or an agent reading documents would stop another agent starting
  work and the ceiling that fired would name the wrong thing.

Both providers, everywhere authorization touches the answer, because "correct
under both providers" is the property this repo has been bitten by twice.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from agent_control_models.knowledge import KnowledgeRefusalCode
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import (
    LocalJwtVerifyProvider,
    NoAuthProvider,
)
from agent_control_server.config import knowledge_settings
from agent_control_server.services import turn_quota
from agent_control_server.services.agent_sessions import mint_session_runtime_token
from agent_control_server.services.knowledge_quota import reset_knowledge_quota
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_API_KEY
from tests.knowledge.support import handbook, seed
from tests.knowledge_provisioning import Corpus

SESSION = "sess-meter-a"
OTHER_SESSION = "sess-meter-b"
RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"


def _url(session_key: str = SESSION, verb: str = "search") -> str:
    return f"/api/v1/agent-sessions/{session_key}/knowledge/{verb}"


@pytest.fixture(autouse=True)
def corpus_wired(corpus: Corpus) -> Iterator[None]:
    fields = type(knowledge_settings).model_fields
    saved = {name: getattr(knowledge_settings, name) for name in fields}
    knowledge_settings.enabled = True
    knowledge_settings.db_url = corpus.read_url
    reset_knowledge_quota()
    yield
    for name, value in saved.items():
        setattr(knowledge_settings, name, value)
    reset_knowledge_quota()


@pytest.fixture(params=["header-key", "no-auth"])
def either_provider(request: pytest.FixtureRequest, app: FastAPI) -> TestClient:
    """The same client, once per authorization provider this product ships.

    ``api_key_enabled`` defaults to false, so the second parameter is the
    configuration most deployments actually run. Anything asserted through this
    fixture is a property of the code rather than of the tier table.
    """
    if request.param == "no-auth":
        set_authorizer(NoAuthProvider())
        return TestClient(app, raise_server_exceptions=True)
    return TestClient(app, raise_server_exceptions=True, headers={"X-API-Key": TEST_ADMIN_API_KEY})


def _search(client: TestClient, session_key: str = SESSION, **body: Any) -> dict[str, Any]:
    body.setdefault("query", "laptop reimbursement")
    response = client.post(_url(session_key), json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _codes(client: TestClient, count: int, **body: Any) -> list[str | None]:
    return [_search(client, **body)["refusal_code"] for _ in range(count)]


@pytest.fixture()
def token_provider() -> Iterator[None]:
    set_runtime_auth_config(RuntimeAuthConfig(secret=RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=RUNTIME_SECRET),
        operation=Operation.COMPANY_KNOWLEDGE_SEARCH,
    )
    yield
    set_runtime_auth_config(None)


def _bearer(session_key: str) -> dict[str, str]:
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id="0123456789abcdef"
    )
    assert minted is not None
    return {"Authorization": f"Bearer {minted[0]}"}


# ---------------------------------------------------------------------------
# What does not spend a search
# ---------------------------------------------------------------------------


def test_a_query_the_bounds_refuse_costs_nothing_from_the_window(
    either_provider: TestClient, corpus: Corpus
) -> None:
    """The order of the checks, read from the other end.

    Ten refused queries, then the allowance in full. If the window were metered
    before the bounds, a model correcting its own too-short query would find the
    ceiling already spent on the queries it was told to fix, which is the one
    outcome that turns a helpful refusal into a dead end.
    """
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 2

    refused = _codes(either_provider, 5, query="ab") + _codes(either_provider, 5, query="x" * 501)
    allowed = _codes(either_provider, 2)
    exhausted = _search(either_provider)["refusal_code"]

    assert set(refused) == {
        KnowledgeRefusalCode.QUERY_TOO_SHORT,
        KnowledgeRefusalCode.QUERY_TOO_LONG,
    }
    assert allowed == [None, None]
    assert exhausted == KnowledgeRefusalCode.RATE_LIMITED


def test_a_switched_off_corpus_does_not_spend_the_window_either(
    either_provider: TestClient, corpus: Corpus
) -> None:
    """An agent that searched a disabled deployment all turn has spent nothing,
    so the day it is switched on it has its full allowance. The refusal is
    answered before a connection is opened and before the meter is touched."""
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 2
    knowledge_settings.enabled = False

    off = _codes(either_provider, 5)
    knowledge_settings.enabled = True

    assert set(off) == {KnowledgeRefusalCode.KNOWLEDGE_DISABLED}
    assert _codes(either_provider, 2) == [None, None]


def test_only_the_rate_limited_refusal_says_when_to_try_again(
    either_provider: TestClient, corpus: Corpus
) -> None:
    """The field is on the response model for one refusal, and a number beside
    a refusal it does not describe is a number a model would act on."""
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 1

    short = _search(either_provider, query="ab")
    _search(either_provider)
    limited = _search(either_provider)
    knowledge_settings.enabled = False
    disabled = _search(either_provider)
    knowledge_settings.enabled = True

    assert short["retry_after_seconds"] is None
    assert disabled["retry_after_seconds"] is None
    assert limited["retry_after_seconds"] is not None


# ---------------------------------------------------------------------------
# The number the refusal names
# ---------------------------------------------------------------------------


class _Clock:
    """A stand-in for the ``time`` module the window reads, movable by a test."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


def test_the_seconds_a_refusal_names_are_the_seconds_that_reopen_the_window(
    client: TestClient, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixed guess would satisfy any test that only checked the field exists.

    So the clock moves by exactly what the refusal said, and the search that
    was refused is made again. One second short of it, the refusal still
    stands: the number is an answer, not a decoration.
    """
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 1
    clock = _Clock()
    monkeypatch.setattr(turn_quota, "time", clock)

    assert _search(client)["refusal_code"] is None
    refused = _search(client)
    wait = refused["retry_after_seconds"]

    clock.now += wait - 2
    still_refused = _search(client)
    clock.now += 2
    reopened = _search(client)

    assert refused["refusal_code"] == KnowledgeRefusalCode.RATE_LIMITED
    assert still_refused["refusal_code"] == KnowledgeRefusalCode.RATE_LIMITED
    assert reopened["refusal_code"] is None


# ---------------------------------------------------------------------------
# Whose bucket
# ---------------------------------------------------------------------------


def test_an_unverified_caller_shares_one_bucket_however_it_names_the_path(
    either_provider: TestClient, corpus: Corpus
) -> None:
    """Both providers that verify no binding, and the behaviour must not differ.

    Neither the header key nor ``NoAuthProvider`` ties the ``{session_key}``
    segment to anything, so it is a string the caller typed. Metering on it
    would read as per-session and behave as no ceiling at all: the loop this
    window exists to bound would rename itself between calls and never spend
    the same bucket twice. One namespace-wide bucket is the fallback plan 8.1
    prescribes, and the cost of it - a fleet sharing one allowance under a
    configuration that identifies nobody - is the smaller of the two.
    """
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 1

    _search(either_provider)
    exhausted = _search(either_provider)
    renamed = _search(either_provider, OTHER_SESSION)

    assert exhausted["refusal_code"] == KnowledgeRefusalCode.RATE_LIMITED
    assert renamed["refusal_code"] == KnowledgeRefusalCode.RATE_LIMITED


def test_one_sessions_runaway_loop_does_not_stop_the_session_beside_it(
    app: FastAPI, corpus: Corpus, token_provider: None
) -> None:
    """Per-session separation, on the path where a session is a verified fact.

    This is the half the test above cannot have. Each token carries its own
    ``target_id``, the verifier has already refused it against the path, and
    the window spends that rather than the URL - so an agent that looped does
    not silence the agent working beside it.
    """
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 1
    machine = TestClient(app, raise_server_exceptions=True)

    def post(session_key: str) -> dict[str, Any]:
        response = machine.post(
            _url(session_key),
            json={"query": "laptop reimbursement"},
            headers=_bearer(session_key),
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    first = post(SESSION)
    exhausted = post(SESSION)
    neighbour = post(OTHER_SESSION)

    assert first["refusal_code"] is None
    assert exhausted["refusal_code"] == KnowledgeRefusalCode.RATE_LIMITED
    assert neighbour["refusal_code"] is None


def test_reading_documents_does_not_spend_the_allowance_for_starting_work(
    either_provider: TestClient, corpus: Corpus
) -> None:
    """A second bucket, not a share of the first.

    Sharing ``turn_quota``'s singleton would look like reuse and would mean an
    agent that read six documents could not start a turn, with a ceiling
    message naming turns. The two windows have the same shape and are
    deliberately not the same object.

    Two things give this teeth, and both are easy to lose. The ceiling below is
    asked for at the *same* size the searches ran under, because
    ``get_turn_quota`` rebuilds its window when the configured number changes
    and a different size would empty the bucket being examined. And the key is
    the one the searches actually spent: no binding is verified under either of
    these providers, so the knowledge window keyed on ``None``, and probing
    ``SESSION`` here would miss a shared singleton entirely.
    """
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 2
    turn_quota.reset_turn_quota()

    _search(either_provider)
    _search(either_provider)

    assert (
        turn_quota.get_turn_quota(max_per_minute=2).try_acquire(
            namespace_key="default", caller_hash=None
        )
        is None
    ), "a knowledge search spent the namespace's turn allowance"


# ---------------------------------------------------------------------------
# The key a caller cannot pick
# ---------------------------------------------------------------------------


def test_a_session_cannot_search_its_way_out_of_its_own_bucket(
    app: FastAPI, corpus: Corpus, token_provider: None
) -> None:
    """The compound the ceiling rests on, asserted in one place.

    An exhausted session cannot reach for a fresh bucket by naming a different
    session in the path, because the verifier compares the path against the
    token's own target before the handler runs. The bucket key is therefore
    something the caller is issued, not something it chooses, which is the
    difference between a ceiling and an honour system.
    """
    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 1
    machine = TestClient(app, raise_server_exceptions=True)

    first = machine.post(_url(), json={"query": "laptop reimbursement"}, headers=_bearer(SESSION))
    exhausted = machine.post(
        _url(), json={"query": "laptop reimbursement"}, headers=_bearer(SESSION)
    )
    escape = machine.post(
        _url(OTHER_SESSION), json={"query": "laptop reimbursement"}, headers=_bearer(SESSION)
    )
    neighbour = machine.post(
        _url(OTHER_SESSION),
        json={"query": "laptop reimbursement"},
        headers=_bearer(OTHER_SESSION),
    )

    assert first.json()["refusal_code"] is None
    assert exhausted.json()["refusal_code"] == KnowledgeRefusalCode.RATE_LIMITED
    assert escape.status_code == 403, escape.text
    assert neighbour.json()["refusal_code"] is None
