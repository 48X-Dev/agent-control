"""Who may search the corpus, under both providers, and with which credential.

Three questions that look like one and are not.

*Which tier.* ``company_knowledge.search`` is ADMIN on the header path, which
is the fallback for a deployment with no runtime secret. It is not a judgement
that reading company documents is an admin act; it is that on that path there
is no session binding to key the ceiling on, and an ordinary key would let any
caller search as any session. ``company_knowledge.status`` is AUTHENTICATED
because it is the oversight path and nothing it returns is a document.

*Which credential.* The real grant is the session-bound runtime token. A token
minted for session A cannot search as session B, and that binding is what makes
the per-session ceiling a ceiling.

*Which provider.* With ``api_key_enabled`` false - the shipped default - every
operation succeeds and ``caller_id`` is None. What must survive there is
everything that is code rather than tier: the query bounds, the clamps and the
window. This file asserts that directly, because "correct under both providers"
is a claim two tests apart from the tier table.

No corpus is configured in this module, so an authorized search answers
``knowledge_disabled`` with HTTP 200. That is the point: the status code
carries the authorization decision and the body carries the corpus's state, so
neither can be mistaken for the other.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_control_models.knowledge import KnowledgeRefusalCode
from agent_control_models.knowledge_search import QUERY_MAX_CHARS
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RUNTIME_TOKEN_BOUND_OPERATIONS,
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import (
    LocalJwtVerifyProvider,
    NoAuthProvider,
)
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.config import knowledge_settings
from agent_control_server.services.agent_sessions import (
    SESSION_TOKEN_SCOPES,
    mint_session_runtime_token,
)
from agent_control_server.services.knowledge_quota import reset_knowledge_quota
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .test_agent_sessions_auth import DenyingAuthorizer

_SESSION = "sess-auth-a"
_OTHER_SESSION = "sess-auth-b"
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"


def _url(session_key: str = _SESSION, verb: str = "search") -> str:
    return f"/api/v1/agent-sessions/{session_key}/knowledge/{verb}"


@pytest.fixture(autouse=True)
def _fresh_window() -> Any:
    reset_knowledge_quota()
    yield
    reset_knowledge_quota()


# ---------------------------------------------------------------------------
# Which tier
# ---------------------------------------------------------------------------


def test_the_machine_read_sits_where_the_other_machine_operations_do() -> None:
    assert DEFAULT_OPERATION_ACCESS[Operation.COMPANY_KNOWLEDGE_SEARCH] is AccessLevel.ADMIN
    assert (
        DEFAULT_OPERATION_ACCESS[Operation.AGENT_NUDGES_CONSUME]
        is DEFAULT_OPERATION_ACCESS[Operation.COMPANY_KNOWLEDGE_SEARCH]
    )


def test_the_oversight_read_sits_where_the_other_oversight_reads_do() -> None:
    assert (
        DEFAULT_OPERATION_ACCESS[Operation.COMPANY_KNOWLEDGE_STATUS]
        is AccessLevel.AUTHENTICATED
    )
    assert (
        DEFAULT_OPERATION_ACCESS[Operation.AGENT_TASKS_READ]
        is DEFAULT_OPERATION_ACCESS[Operation.COMPANY_KNOWLEDGE_STATUS]
    )


def test_an_ordinary_key_cannot_search_on_the_header_path(
    non_admin_client: TestClient,
) -> None:
    """The tier binds. Without this the ADMIN entry is a comment.

    On this path there is no token to bind the search to a session, so an
    ordinary key would be a key that searches as anybody.
    """
    refused = non_admin_client.post(_url(), json={"query": "laptop policy"})

    assert refused.status_code == 403, refused.text


def test_the_agents_token_carries_the_search_and_the_consoles_operation_does_not() -> None:
    """One scope widened, one deliberately not.

    An executor's token may search. It may not read the corpus's status: that
    is the oversight surface, it belongs to a human's credential, and a token
    that carried both would make "what the fleet may read" and "what an
    operator may audit" the same grant.
    """
    assert Operation.COMPANY_KNOWLEDGE_SEARCH.value in SESSION_TOKEN_SCOPES
    assert Operation.COMPANY_KNOWLEDGE_STATUS.value not in SESSION_TOKEN_SCOPES
    assert Operation.COMPANY_KNOWLEDGE_SEARCH in RUNTIME_TOKEN_BOUND_OPERATIONS
    assert Operation.COMPANY_KNOWLEDGE_STATUS not in RUNTIME_TOKEN_BOUND_OPERATIONS


# ---------------------------------------------------------------------------
# Which operation
# ---------------------------------------------------------------------------


def test_both_verbs_consult_the_search_operation_and_nothing_else(
    client: TestClient,
) -> None:
    """Catches the failure a tier test cannot: a handler dragging in a second
    operation, which quietly narrows who can use it."""
    for verb, body in (("search", {"query": "laptop policy"}), ("recent", {"days": 7})):
        set_authorizer(DenyingAuthorizer(Operation.COMPANY_KNOWLEDGE_SEARCH))
        assert client.post(_url(verb=verb), json=body).status_code == 403

        authorizer = DenyingAuthorizer(Operation.AGENT_SESSIONS_READ)
        set_authorizer(authorizer)
        allowed = client.post(_url(verb=verb), json=body)
        assert allowed.status_code == 200, allowed.text
        assert authorizer.seen == [Operation.COMPANY_KNOWLEDGE_SEARCH]


# ---------------------------------------------------------------------------
# Which credential
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime_provider() -> Any:
    """Route the search operation through the token verifier, as a deployment does."""
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.COMPANY_KNOWLEDGE_SEARCH,
    )
    yield
    set_runtime_auth_config(None)


def _token(session_key: str) -> dict[str, str]:
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id="0123456789abcdef"
    )
    assert minted is not None
    return {"Authorization": f"Bearer {minted[0]}"}


def test_the_session_token_searches_its_own_session(
    app: FastAPI, runtime_provider: None
) -> None:
    machine = TestClient(app, raise_server_exceptions=True)

    answered = machine.post(
        _url(), json={"query": "laptop policy"}, headers=_token(_SESSION)
    )

    assert answered.status_code == 200, answered.text
    assert answered.json()["refusal_code"] == KnowledgeRefusalCode.KNOWLEDGE_DISABLED


def test_a_token_minted_for_another_session_cannot_search_this_one(
    app: FastAPI, runtime_provider: None
) -> None:
    """The whole authorization design in one assertion.

    The context builder plucks the session key out of the path and the verifier
    compares it against the token's own target. A search is metered per
    session; a token that could name a different session would make the meter
    key something the caller picks.
    """
    machine = TestClient(app, raise_server_exceptions=True)

    refused = machine.post(
        _url(), json={"query": "laptop policy"}, headers=_token(_OTHER_SESSION)
    )

    assert refused.status_code == 403, refused.text


def test_the_recency_verb_is_bound_to_the_session_the_same_way(
    app: FastAPI, runtime_provider: None
) -> None:
    machine = TestClient(app, raise_server_exceptions=True)

    assert (
        machine.post(_url(verb="recent"), json={}, headers=_token(_OTHER_SESSION)).status_code
        == 403
    )
    assert (
        machine.post(_url(verb="recent"), json={}, headers=_token(_SESSION)).status_code == 200
    )


# ---------------------------------------------------------------------------
# Which provider
# ---------------------------------------------------------------------------


def test_with_credential_checks_off_the_ceilings_are_still_the_ceilings(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped default, priced honestly.

    Every operation succeeds and the caller is nobody, exactly as it is for
    every other operation in that configuration. What does not change is
    anything written in code: the query bounds refuse typed, and the response
    carries both counters.
    """
    set_authorizer(NoAuthProvider())
    anonymous = TestClient(app, raise_server_exceptions=True)
    monkeypatch.setattr(knowledge_settings, "enabled", True)

    reachable = anonymous.post(_url(), json={"query": "laptop policy"})
    too_short = anonymous.post(_url(), json={"query": "ab"})
    too_long = anonymous.post(_url(), json={"query": "x" * (QUERY_MAX_CHARS + 1)})

    assert reachable.status_code == 200, reachable.text
    assert reachable.json()["refusal_code"] == KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE
    assert too_short.json()["refusal_code"] == KnowledgeRefusalCode.QUERY_TOO_SHORT
    assert too_long.json()["refusal_code"] == KnowledgeRefusalCode.QUERY_TOO_LONG
    for response in (reachable, too_short, too_long):
        payload = response.json()
        assert payload["result_count"] == 0
        assert payload["external_author_count"] == 0


def test_with_credential_checks_off_the_window_is_one_bucket_for_everybody(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim the docstring above used to make and never checked.

    Nothing here ties ``{session_key}`` to a caller, so metering on it would
    hand a loop a fresh allowance every time it typed a new number. Six
    different session keys, one allowance: the first search spends it and the
    other five are refused. That is the fallback plan 8.1 names, and it is the
    only shape that bounds anything under a provider that identifies nobody.
    """
    set_authorizer(NoAuthProvider())
    anonymous = TestClient(app, raise_server_exceptions=True)
    monkeypatch.setattr(knowledge_settings, "enabled", True)
    monkeypatch.setattr(knowledge_settings, "searches_per_minute", 1)

    codes = [
        anonymous.post(_url(f"sess-invented-{index}"), json={"query": "laptop policy"}).json()[
            "refusal_code"
        ]
        for index in range(6)
    ]

    assert codes[0] == KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE
    assert codes[1:] == [KnowledgeRefusalCode.RATE_LIMITED] * 5


def test_a_switched_off_corpus_says_so_before_it_grades_the_query(
    client: TestClient,
) -> None:
    """Refusal order is an answer to "what does the model most need to know".

    With the feature off, "your query was too short" would send a model away to
    rewrite a query that was never going to be run. The fact that matters is
    that there is no knowledge base here.
    """
    refused = client.post(_url(), json={"query": "ab"})

    assert refused.json()["refusal_code"] == KnowledgeRefusalCode.KNOWLEDGE_DISABLED


# ---------------------------------------------------------------------------
# What the routes refuse to be
# ---------------------------------------------------------------------------


def test_the_corpus_has_no_route_that_lists_it(app: FastAPI) -> None:
    """Proof by absence, checked against the app rather than the prose.

    A list endpoint plus a loop is the whole corpus in a transcript, so the
    refusal has to be structural. Two things are asserted, and the second is
    the one that survives a new surface being added: every route into the
    corpus ends in one of the two verbs, and the full inventory is spelled out
    so a third verb has to be typed here on purpose rather than appearing.

    The console pair is a second door onto the same two verbs at a different
    tier, not a third verb. Surfaces may multiply; the verbs may not.
    """
    paths = set(app.openapi()["paths"])

    knowledge_paths = {path for path in paths if "knowledge" in path}
    assert all(
        path.endswith(("/search", "/recent")) for path in knowledge_paths
    ), sorted(knowledge_paths)
    assert knowledge_paths == {
        "/api/v1/agent-sessions/{session_key}/knowledge/search",
        "/api/v1/agent-sessions/{session_key}/knowledge/recent",
        "/api/v1/company-knowledge/search",
        "/api/v1/company-knowledge/recent",
    }


def test_neither_verb_accepts_a_parameter_that_would_page_it(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    paging = {"cursor", "offset", "page", "page_token", "after", "skip", "start", "all"}

    for name in ("KnowledgeSearchRequest", "KnowledgeRecentRequest"):
        assert not paging & set(schemas[name]["properties"]), name
