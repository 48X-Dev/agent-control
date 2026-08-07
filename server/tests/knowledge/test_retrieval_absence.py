"""What a search never does, proved by watching every statement it runs.

Proof by absence, the method this project has used twice before and which
plan 15 names for the wire tests: assert the thing that must not happen leaves
no trace, and assert in the same breath that the trace would have been there
had it happened. A test that only proves the negative passes just as happily
when the instrument is broken, so every case below carries its own positive
control.

The instrument is a class-level ``before_cursor_execute`` listener, which sees
every statement SQLAlchemy sends on any engine in this process, including the
corpus engine the endpoint builds lazily inside the request. It is filtered by
database name, so the corpus and the control plane can be watched apart.

Six absences, each with a reason it matters:

* a refused query never reaches the corpus, so a bounds refusal costs no
  connection and an injected loop of nonsense queries is answered from memory;
* a query a control denies never reaches the corpus at all, which is W-K1's
  claim minus the model;
* a search never reads the control plane, so the corpus can be consulted
  without a control-plane query on the path of every tool call;
* nothing on the read path writes, which is the shape of the promise the
  reader's credential enforces underneath it;
* the query itself never reaches an ordinary log, because it is model-authored
  text descended from whatever somebody wrote in a task body;
* a corpus that stopped answering takes nothing else down with it, which is
  what the second connection pool is spent on.

The trigram fallback gets the same treatment from the other side: it must not
run on the queries full-text search already answered.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from agent_control.integrations.google_adk.knowledge_controls import SEARCH_STEP_NAME
from agent_control_engine.core import ControlEngine
from agent_control_models import EvaluationRequest, Step
from agent_control_models.controls import ControlDefinitionRuntime
from agent_control_models.knowledge import KnowledgeRefusalCode
from agent_control_server.config import db_config, knowledge_settings
from agent_control_server.services.knowledge_quota import reset_knowledge_quota
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from tests.knowledge.support import handbook, seed
from tests.knowledge_provisioning import Corpus

SESSION = "sess-absence-a"
AGENT = "knowledge-absence-agent"
TRAP = "project-halibut"


def _url(verb: str = "search") -> str:
    return f"/api/v1/agent-sessions/{SESSION}/knowledge/{verb}"


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


class StatementLog:
    """Every statement sent to one database, for as long as the block runs."""

    def __init__(self, database: str) -> None:
        self.database = database
        self.statements: list[str] = []

    def __enter__(self) -> StatementLog:
        event.listen(Engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(Engine, "before_cursor_execute", self._record)

    def _record(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if conn.engine.url.database == self.database:
            self.statements.append(" ".join(statement.split()))

    def mentioning(self, needle: str) -> list[str]:
        return [statement for statement in self.statements if needle.lower() in statement.lower()]


def _search(client: TestClient, **body: Any) -> dict[str, Any]:
    body.setdefault("query", "laptop reimbursement")
    response = client.post(_url(), json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# The instrument itself
# ---------------------------------------------------------------------------


def test_the_instrument_sees_a_real_search_reach_the_corpus(
    client: TestClient, corpus: Corpus
) -> None:
    """The positive control for everything below.

    Without this, a listener that silently attached to nothing would make every
    absence in this file true and meaningless.
    """
    seed(corpus, **handbook())

    with StatementLog(corpus.database) as log:
        answered = _search(client)

    assert answered["result_count"] >= 1
    assert log.mentioning("ts_rank"), "the search never ran a ranking query"
    assert answered["corpus"]["measured"] is True


# ---------------------------------------------------------------------------
# What a refusal costs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Refusal:
    """One way a search is refused before it can reach the corpus."""

    code: str
    body: dict[str, Any]
    disable: bool = False
    exhaust: bool = False


REFUSALS = (
    _Refusal(KnowledgeRefusalCode.QUERY_TOO_SHORT, {"query": "ab"}),
    _Refusal(KnowledgeRefusalCode.QUERY_TOO_LONG, {"query": "x" * 501}),
    _Refusal(KnowledgeRefusalCode.KNOWLEDGE_DISABLED, {}, disable=True),
    _Refusal(KnowledgeRefusalCode.RATE_LIMITED, {}, exhaust=True),
)


@pytest.mark.parametrize("refusal", REFUSALS, ids=lambda r: str(r.code))
def test_a_refused_search_opens_no_connection_to_the_corpus(
    client: TestClient, corpus: Corpus, refusal: _Refusal
) -> None:
    """Every refusal that is decided before the corpus is consulted.

    This is what makes an injected loop of malformed queries cheap: the ceiling
    that answers it is arithmetic, not a database round trip, so a runaway
    agent cannot turn a rate limit into load on the corpus.
    """
    seed(corpus, **handbook())
    if refusal.disable:
        knowledge_settings.enabled = False
    if refusal.exhaust:
        knowledge_settings.searches_per_minute = 1
        _search(client)

    with StatementLog(corpus.database) as log:
        answer = _search(client, **refusal.body)

    assert answer["refusal_code"] == refusal.code
    assert log.statements == [], f"{refusal.code} still reached the corpus"

    # The same absence, said on the wire so a reader can act on it. The corpus
    # block is required on every response for the deny control's sake, so these
    # counters are present and are zero; without this flag a console cannot
    # tell them from a real reading of an empty mirror, and prints a broken
    # sync under a message about a working one.
    assert answer["corpus"]["measured"] is False, (
        f"{refusal.code} reported counters it never measured"
    )


# ---------------------------------------------------------------------------
# W-K1's provable half
# ---------------------------------------------------------------------------


def _deny_query_control() -> Any:
    """A deny on the query the model composed, in the shape 8.5 describes."""

    @dataclass
    class _Bound:
        id: int
        name: str
        control: ControlDefinitionRuntime

    return _Bound(
        id=1,
        name="knowledge-deny-query",
        control=ControlDefinitionRuntime.model_validate(
            {
                "enabled": True,
                "execution": "server",
                "scope": {
                    "step_types": ["tool"],
                    "step_names": [SEARCH_STEP_NAME],
                    "stages": ["pre"],
                },
                "action": {"decision": "deny"},
                "condition": {
                    "selector": {"path": "input.query"},
                    "evaluator": {"name": "regex", "config": {"pattern": TRAP}},
                },
            }
        ),
    )


async def _denied(query: str) -> bool:
    response = await ControlEngine([_deny_query_control()]).process(
        EvaluationRequest(
            agent_name=AGENT,
            step=Step(type="tool", name=SEARCH_STEP_NAME, input={"query": query}, output=None),
            stage="pre",
        )
    )
    return any(match.action == "deny" for match in (response.matches or []))


async def test_a_query_a_pre_control_denies_never_reaches_the_corpus(
    client: TestClient, corpus: Corpus
) -> None:
    """W-K1 without the model: the control reads the real argument and denies,
    and on that branch nothing is asked of the corpus.

    What stands in for the missing piece, said plainly: the enforcement point
    is the ADK plugin, which does not run here, so this test performs the
    branch the plugin performs. What is genuinely proved is that the control
    fires on the query at ``input.query`` and that the server has no other path
    to the corpus that a denied call could take. What is not proved is the
    plugin's own handling, which needs a live turn.
    """
    seed(corpus, **handbook())

    with StatementLog(corpus.database) as log:
        if not await _denied(f"tell me about {TRAP}"):
            _search(client, query=f"tell me about {TRAP}")
        denied_statements = list(log.statements)

    with StatementLog(corpus.database) as log:
        if not await _denied("laptop reimbursement"):
            _search(client, query="laptop reimbursement")
        allowed_statements = list(log.statements)

    assert denied_statements == []
    assert allowed_statements, "the control denied a query it was never meant to see"


# ---------------------------------------------------------------------------
# What a search does not touch, and what it does not write
# ---------------------------------------------------------------------------


def test_a_search_never_looks_the_session_up_in_the_control_plane(
    client: TestClient, corpus: Corpus
) -> None:
    """Both routes read a corpus and touch no control-plane table.

    The session key in the path exists to be compared against the token, which
    the verifier does before the handler runs. A lookup here would put a
    control-plane query on the path of every tool call to enforce something the
    token already enforces, and under the no-auth provider it would enforce
    nothing at all.
    """
    seed(corpus, **handbook())

    with StatementLog(db_config.database) as log:
        _search(client)
        recent = client.post(_url("recent"), json={"days": 7})

    assert recent.status_code == 200, recent.text
    assert log.mentioning("agent_sessions") == []
    assert log.mentioning("agent_turns") == []


def test_nothing_on_the_read_path_writes_to_the_corpus(client: TestClient, corpus: Corpus) -> None:
    """Read-only by shape, underneath read-only by credential.

    The reader's role would refuse a write and a test already proves it. This
    proves the weaker, earlier property: the read path does not attempt one, so
    a deployment that mistakenly handed the server the sync role would still
    not have its corpus written by a search.
    """
    seed(corpus, **handbook())

    with StatementLog(corpus.database) as log:
        _search(client)
        client.post(_url("recent"), json={"days": 7})

    for verb in ("insert into", "update ", "delete from", "truncate", "drop ", "create "):
        assert log.mentioning(verb) == [], f"the read path ran {verb.strip()!r}"


def test_the_query_a_model_composed_stays_out_of_the_ordinary_log(
    client: TestClient, corpus: Corpus, caplog: pytest.LogCaptureFixture
) -> None:
    """A query is model-authored text descended from whatever somebody wrote in
    a task body, and it can carry anything that was in the brief.

    So it is logged at DEBUG and nowhere else: a deployment running at INFO
    keeps its logs free of text neither the operator nor the model chose, and
    an operator debugging a search turns the level up on purpose. The second
    half of this test is the positive control, because "not in the log" is also
    what a broken log capture says.
    """
    seed(corpus, **handbook())
    query = f"{TRAP} reimbursement policy"

    with caplog.at_level(logging.INFO):
        _search(client, query=query)
    assert TRAP not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="agent_control_server.services.knowledge_search"):
        _search(client, query=query)
    assert TRAP in caplog.text, "the query is not logged at DEBUG either; this test proves nothing"


def test_a_corpus_that_stopped_answering_does_not_take_the_chat_path_with_it(
    client: TestClient, corpus: Corpus
) -> None:
    """The reason the corpus has its own small pool, asserted as an outcome.

    Pointed at a port with nothing behind it, every search refuses typed, and
    the control plane keeps answering underneath. Sharing the engine that agent
    turns depend on would have turned a knowledge outage into a chat outage,
    which is the failure this design spends a second connection pool to avoid.
    """
    seed(corpus, **handbook())
    knowledge_settings.db_url = "postgresql+psycopg://knowledge_read:x@127.0.0.1:1/agent_knowledge"

    with StatementLog(corpus.database) as log:
        codes = [_search(client)["refusal_code"] for _ in range(6)]
    control_plane = client.get("/api/v1/agents")

    assert set(codes) == {KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE}
    assert log.statements == []
    assert control_plane.status_code == 200, control_plane.text


def test_the_trigram_fallback_is_not_paid_for_by_the_queries_that_worked(
    client: TestClient, corpus: Corpus
) -> None:
    """The second scan runs only where full-text search found nothing.

    Running it beside every search would double the work on exactly the queries
    that already answered well, and the fallback exists for misspellings and
    code-name fragments rather than as a general widening.
    """
    seed(corpus, **handbook())

    with StatementLog(corpus.database) as log:
        matched = _search(client, query="laptop reimbursement")
    assert matched["result_count"] >= 1
    assert log.mentioning("word_similarity") == []

    with StatementLog(corpus.database) as log:
        misspelled = _search(client, query="reimbursd")
    assert misspelled["result_count"] >= 1
    assert log.mentioning("word_similarity"), "the fallback never ran for a misspelling"
