"""The store under both auth providers, and the gate it deliberately does not hold.

Authorization does not live in this slice. There is no knowledge route yet, so
there is nothing here for a provider to admit or refuse, and saying that
plainly is worth more than a test that pretends otherwise. What can be proved
now is the property section 8.1 will depend on later: the corpus answers the
same rows and the same refusals whether the caller was resolved or not.

That matters because ``api_key_enabled`` defaults false. A default deployment
runs ``NoAuthProvider``, every operation succeeds and ``caller_id`` is None. A
store that quietly behaved differently in that configuration would be tested on
one machine and deployed on another, which is the shape this repo has been
bitten by before.

The last two tests are tripwires for the phase that adds the endpoint. They
pass vacuously today and turn into real checks the moment a knowledge Operation
exists, which is the only way to write a check for code somebody else has not
written yet.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.providers import HeaderAuthProvider
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider
from agent_control_server.config import auth_settings
from agent_control_server.knowledge import (
    KnowledgeUnavailableError,
    knowledge_session,
    recent_documents,
    search_chunks,
    search_chunks_trigram,
)
from sqlalchemy.pool import NullPool

from tests.knowledge.support import handbook, seed, settings_for
from tests.knowledge_provisioning import Corpus

QUERY_VERBS = (search_chunks, search_chunks_trigram, recent_documents)

# Names the control plane uses for "who is asking". None of them appear in this
# slice, by decision rather than by omission: section 9 gives the whole
# namespace one corpus.
IDENTITY_PARAMETERS = frozenset(
    {"principal", "caller", "caller_id", "namespace_key", "namespace", "team", "team_slug"}
)

KNOWLEDGE_OPERATION_TIERS = {
    "company_knowledge.search": AccessLevel.ADMIN,
    "company_knowledge.status": AccessLevel.AUTHENTICATED,
}


@pytest.fixture(params=["header_api_key", "no_auth"])
def provider(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Both halves: the provider that resolves callers, and the default one."""
    if request.param == "header_api_key":
        monkeypatch.setattr(auth_settings, "api_key_enabled", True)
        set_authorizer(HeaderAuthProvider())
    else:
        monkeypatch.setattr(auth_settings, "api_key_enabled", False)
        set_authorizer(NoAuthProvider())
    # Teardown is the suite's own autouse authorizer fixture, which clears
    # whatever is installed after every test.
    yield request.param


async def test_a_search_returns_the_same_rows_under_either_provider(
    corpus: Corpus, provider: str
) -> None:
    """One corpus, namespace-wide. The caller changes nothing about the answer."""
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=400
        )

    assert [row.path for row in results] == ["Ops Handbook/Onboarding/laptops.md"], provider
    assert results[0].heading_path == "Onboarding > Laptops"


async def test_a_refusal_names_the_same_code_under_either_provider(
    corpus: Corpus, provider: str
) -> None:
    """The refusal vocabulary is a property of the corpus, not of the caller."""
    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(settings_for(corpus, enabled=False)):
            pass

    assert caught.value.code == "knowledge_disabled", provider


@pytest.mark.parametrize("verb", QUERY_VERBS, ids=lambda verb: verb.__name__)
def test_no_query_verb_takes_a_caller_identity(verb: object) -> None:
    """Slice one has no per-team, per-agent or per-user visibility, on purpose.

    Section 9 states it and the mitigation that goes with it: do not index a
    folder whose content should not reach every agent in the fleet. The
    consequence for anything built on this store is concrete. Filtering by
    identity is not available here, so the operation tier and the tool
    allowlist are the whole of the access control, and an endpoint that assumed
    the store would narrow its results would be assuming a parameter that does
    not exist.
    """
    parameters = set(inspect.signature(verb).parameters)  # type: ignore[arg-type]

    assert not parameters & IDENTITY_PARAMETERS, sorted(parameters & IDENTITY_PARAMETERS)


def test_the_corpus_carries_no_column_that_could_scope_a_reader(corpus: Corpus) -> None:
    """The same statement one layer down, where a filter would have to key on something."""
    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        inspector = sa.inspect(engine)
        columns = {
            f"{table}.{column['name']}"
            for table in ("sources", "documents", "chunks")
            for column in inspector.get_columns(table)
        }
    finally:
        engine.dispose()

    scoping = {name for name in columns if name.split(".")[1] in IDENTITY_PARAMETERS}
    assert not scoping, sorted(scoping)


def test_any_knowledge_operation_that_exists_is_registered() -> None:
    """The tripwire for the commit that adds the endpoint.

    An Operation missing from ``DEFAULT_OPERATION_ACCESS`` makes the server
    refuse to start, which is loud but late: it fails on the machine that
    deploys it rather than on the machine that wrote it. Registered is half of
    it; the tiers below are the other half.
    """
    knowledge_operations = [op for op in Operation if "knowledge" in op.value]

    for operation in knowledge_operations:
        assert operation in DEFAULT_OPERATION_ACCESS, operation.value


def test_any_knowledge_operation_that_exists_is_gated_at_the_tier_the_plan_names() -> None:
    """Search is admin on the header path; status sits with the oversight reads.

    Search is a machine-side operation normally carried by a session-bound
    runtime token. The header entry is the fallback for a deployment with no
    runtime secret, and there the per-session window has no session to key on,
    so the tier fails closed.
    """
    for operation in Operation:
        expected = KNOWLEDGE_OPERATION_TIERS.get(operation.value)
        if expected is None:
            continue
        assert DEFAULT_OPERATION_ACCESS.get(operation) is expected, operation.value
