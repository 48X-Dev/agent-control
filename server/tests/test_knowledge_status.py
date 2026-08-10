"""The oversight read: is the mirror current, and which source stopped working.

Three claims are pinned here that no other test in this suite makes. The
staleness clock keys on verification rather than on cursor advancement. The two
source states that present as success - an enabled source holding nothing, and
a root that would not resolve - are reported as failures. And the endpoint
answers before Phase 2 has ever run, because the panel that shows a broken sync
is worth the most on the day there is one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.pool import NullPool

from agent_control_server.auth_framework import Operation
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.config import knowledge_settings
from agent_control_server.knowledge import dispose_knowledge_engine
from agent_control_server.knowledge.seed import SeedDocument
from tests.knowledge.support import LAPTOPS, PHONES, RELEASES, seed
from tests.knowledge_provisioning import Corpus, provision, teardown, unavailable_reason

STATUS = "/api/v1/company-knowledge/status"


@pytest.fixture(scope="module")
def corpus() -> Iterator[Corpus]:
    """One provisioned corpus per module, or a skip naming what is missing."""
    reason = unavailable_reason()
    if reason:
        pytest.skip(reason)
    provisioned = provision()
    try:
        yield provisioned
    finally:
        teardown(provisioned)


@pytest.fixture(autouse=True)
async def corpus_enabled(corpus: Corpus) -> AsyncIterator[None]:
    """An empty corpus, a fresh engine, and settings pointed at both."""
    await dispose_knowledge_engine()
    execute(corpus, "TRUNCATE chunks, documents, sources RESTART IDENTITY CASCADE")
    fields = type(knowledge_settings).model_fields
    saved = {name: getattr(knowledge_settings, name) for name in fields}
    knowledge_settings.enabled = True
    knowledge_settings.db_url = corpus.read_url
    yield
    for name, value in saved.items():
        setattr(knowledge_settings, name, value)
    await dispose_knowledge_engine()


def execute(corpus: Corpus, statement: str, **params: Any) -> None:
    """Write to the corpus as the sync would, for the columns the seed omits."""
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(statement), params)
    finally:
        engine.dispose()


def ask(client: TestClient) -> dict[str, Any]:
    response = client.get(STATUS)
    assert response.status_code == 200, response.text
    return dict(response.json())


def only_source(payload: dict[str, Any]) -> dict[str, Any]:
    assert len(payload["sources"]) == 1, payload["sources"]
    return dict(payload["sources"][0])


def handbook_doc() -> SeedDocument:
    return SeedDocument(path="Ops Handbook/Onboarding/laptops.md", body=LAPTOPS)


# ---------------------------------------------------------------------------
# Before anything has synced
# ---------------------------------------------------------------------------


def test_it_answers_before_the_sync_has_ever_run(client: TestClient) -> None:
    """No sources, no rows, and a valid body rather than a 500."""
    payload = ask(client)

    assert payload["schema_supported"] is True
    assert payload["schema_version"] is not None
    assert payload["sources"] == []
    assert payload["document_count"] == 0
    assert payload["chunk_count"] == 0
    assert payload["sources_failing"] == 0
    assert payload["stale_seconds"] is None
    assert payload["staleness_warn_seconds"] == knowledge_settings.staleness_warn_seconds


def test_a_switched_off_corpus_is_reported_rather_than_raised(
    client: TestClient, corpus: Corpus
) -> None:
    """The panel exists to show a broken mirror, so it cannot break with one.

    False and None together say the corpus was not read at all, which is a
    different fact from a corpus that was read and is empty.
    """
    seed(corpus, source_ref="ops-handbook", docs=[handbook_doc()])
    knowledge_settings.enabled = False

    payload = ask(client)

    assert payload["schema_supported"] is False
    assert payload["schema_version"] is None
    assert payload["sources"] == []
    assert payload["document_count"] == 0
    assert payload["staleness_warn_seconds"] == knowledge_settings.staleness_warn_seconds


# ---------------------------------------------------------------------------
# What each source says about itself
# ---------------------------------------------------------------------------


def test_it_reports_each_source_under_the_wires_own_word_for_its_kind(
    client: TestClient, corpus: Corpus
) -> None:
    seed(corpus, source_ref="ops-handbook", docs=[handbook_doc()])
    seed(
        corpus,
        source_kind="github_repo",
        source_ref="acme/agent-control",
        source_name="agent-control",
        docs=[SeedDocument(path="agent-control:docs/releases.md", body=RELEASES)],
    )

    payload = ask(client)

    assert {s["source_id"]: s["kind"] for s in payload["sources"]} == {
        "ops-handbook": "drive",
        "acme/agent-control": "github",
    }
    assert payload["document_count"] == 2
    assert payload["chunk_count"] >= 2


def test_the_totals_count_what_a_search_could_actually_reach(
    client: TestClient, corpus: Corpus
) -> None:
    """Enabled-scoped at the top, so this panel and the search strip agree.

    A switched-off source still reports its own documents: "off, still holding
    four hundred" is what somebody needs before turning it back on.
    """
    seed(corpus, source_ref="live", docs=[SeedDocument(path="Live/laptops.md", body=LAPTOPS)])
    seed(
        corpus,
        source_ref="retired",
        enabled=False,
        docs=[SeedDocument(path="Retired/phones.md", body=PHONES)],
    )

    payload = ask(client)
    by_id = {s["source_id"]: s for s in payload["sources"]}

    assert payload["document_count"] == 1
    assert by_id["live"]["document_count"] == 1
    assert by_id["retired"]["document_count"] == 1
    assert by_id["retired"]["enabled"] is False


# ---------------------------------------------------------------------------
# Verification, not advancement
# ---------------------------------------------------------------------------


def test_a_quiet_source_that_still_answers_a_check_is_not_stale(
    client: TestClient, corpus: Corpus
) -> None:
    """The whole reason the clock moved off the cursor.

    Keyed on the cursor, this source reads a month behind while being checked
    every fifteen minutes, and a warning that is always on is one nobody reads.
    """
    seed(
        corpus,
        source_kind="github_repo",
        source_ref="acme/quiet",
        last_verified_at=datetime.now(UTC),
        docs=[SeedDocument(path="quiet:docs/releases.md", body=RELEASES)],
    )
    execute(
        corpus,
        "UPDATE sources SET cursor_advanced_at = :moved",
        moved=datetime.now(UTC) - timedelta(days=30),
    )

    source = only_source(ask(client))

    assert source["cursor_advanced_at"] is not None
    assert source["stale_seconds"] < 3600
    assert ask(client)["stale_seconds"] < 3600


def test_a_source_that_has_not_answered_a_check_is_stale_by_exactly_that(
    client: TestClient, corpus: Corpus
) -> None:
    seed(
        corpus,
        source_ref="ops-handbook",
        last_verified_at=datetime.now(UTC) - timedelta(days=3),
        docs=[handbook_doc()],
    )

    payload = ask(client)

    assert payload["stale_seconds"] == pytest.approx(3 * 86_400, abs=300)
    assert only_source(payload)["stale_seconds"] == pytest.approx(3 * 86_400, abs=300)


def test_a_source_that_never_verified_reports_no_age_rather_than_zero(
    client: TestClient, corpus: Corpus
) -> None:
    """Zero would read as "checked just now", which is the opposite of true."""
    seed(corpus, source_ref="ops-handbook", docs=[handbook_doc()])
    execute(corpus, "UPDATE sources SET last_verified_at = NULL")

    payload = ask(client)

    assert only_source(payload)["stale_seconds"] is None
    assert payload["stale_seconds"] is None


def test_the_corpus_age_is_the_oldest_enabled_sources(
    client: TestClient, corpus: Corpus
) -> None:
    seed(
        corpus,
        source_ref="fresh",
        last_verified_at=datetime.now(UTC),
        docs=[SeedDocument(path="Fresh/laptops.md", body=LAPTOPS)],
    )
    seed(
        corpus,
        source_ref="behind",
        last_verified_at=datetime.now(UTC) - timedelta(days=5),
        docs=[SeedDocument(path="Behind/phones.md", body=PHONES)],
    )

    payload = ask(client)

    assert payload["stale_seconds"] == pytest.approx(5 * 86_400, abs=300)


# ---------------------------------------------------------------------------
# The two states that present as success
# ---------------------------------------------------------------------------


def test_an_enabled_source_holding_nothing_is_a_failure_not_a_zero(
    client: TestClient, corpus: Corpus
) -> None:
    """The shape a missing env passthrough makes, and an empty folder's too.

    Indistinguishable from each other and from success if all that is said
    about them is ``0``, so it is ``failing`` and readable off the two fields
    that produced it. No code, because the sync recorded none.
    """
    seed(corpus, source_ref="company-knowledge", source_name="Company Knowledge", docs=[])

    payload = ask(client)
    source = only_source(payload)

    assert source["enabled"] is True
    assert source["document_count"] == 0
    assert source["failing"] is True
    assert source["last_failure_code"] is None
    assert payload["sources_failing"] == 1


def test_a_root_that_would_not_resolve_says_so_instead_of_reading_as_empty(
    client: TestClient, corpus: Corpus
) -> None:
    """Plan 5.7: a shared-drive root reached without ``supportsAllDrives``
    answers 404 and looks exactly like a folder nobody shared."""
    seed(corpus, source_ref="company-knowledge", last_run_status="failed", docs=[])
    execute(corpus, "UPDATE sources SET last_run_error_code = 'root_not_found'")

    source = only_source(ask(client))

    assert source["failing"] is True
    assert source["last_failure_code"] == "root_not_found"


def test_a_run_that_finished_carrying_a_code_is_not_a_source_that_stopped(
    client: TestClient, corpus: Corpus
) -> None:
    """The mirror of the empty case, and why the two fields are not one field.

    A first walk that hit the per-run ceiling recorded a code and indexed a
    working corpus. ``failing`` is the conclusion, the code is the sync's
    record, and neither one answers for the other.
    """
    seed(corpus, source_ref="ops-handbook", last_run_status="partial", docs=[handbook_doc()])
    execute(corpus, "UPDATE sources SET last_run_error_code = 'source_ceiling'")

    payload = ask(client)
    source = only_source(payload)

    assert source["failing"] is False
    assert source["last_failure_code"] == "source_ceiling"
    assert payload["sources_failing"] == 0


def test_a_run_that_failed_without_a_code_reports_no_code_rather_than_one(
    client: TestClient, corpus: Corpus
) -> None:
    """Silence from the sync is reported as silence.

    Naming it here would put this server's guess in the field an operator
    greps the sync's log for, which is the one thing that field must not hold.
    """
    seed(
        corpus,
        source_ref="ops-handbook",
        last_run_status="failed",
        docs=[handbook_doc()],
    )

    source = only_source(ask(client))

    assert source["failing"] is True
    assert source["last_failure_code"] is None


def test_a_source_holding_documents_is_not_reported_failing(
    client: TestClient, corpus: Corpus
) -> None:
    """The other direction, or ``failing`` means nothing."""
    seed(corpus, source_ref="ops-handbook", docs=[handbook_doc()])

    payload = ask(client)
    source = only_source(payload)

    assert source["failing"] is False
    assert source["last_failure_code"] is None
    assert payload["sources_failing"] == 0


def test_a_switched_off_source_is_not_failing_for_being_empty(
    client: TestClient, corpus: Corpus
) -> None:
    """Nothing is reading it, so nothing about it is behind."""
    seed(corpus, source_ref="retired", enabled=False, docs=[])

    payload = ask(client)
    source = only_source(payload)

    assert source["enabled"] is False
    assert source["failing"] is False
    assert payload["sources_failing"] == 0


def test_the_failing_count_is_the_number_of_failing_rows(
    client: TestClient, corpus: Corpus
) -> None:
    """A header that disagreed with the table under it is a panel nobody trusts."""
    seed(corpus, source_ref="healthy", docs=[handbook_doc()])
    seed(corpus, source_ref="empty", docs=[])
    seed(corpus, source_kind="github_repo", source_ref="acme/broken", docs=[])

    payload = ask(client)

    assert payload["sources_failing"] == sum(1 for s in payload["sources"] if s["failing"])
    assert payload["sources_failing"] == 2


def test_per_item_refusals_are_counted_by_reason_and_a_deletion_is_not_one(
    client: TestClient, corpus: Corpus
) -> None:
    """A file that went away upstream is lifecycle; a file the sync declined is not."""
    tombstoned = datetime.now(UTC)
    seed(
        corpus,
        source_ref="ops-handbook",
        docs=[
            handbook_doc(),
            SeedDocument(
                path="Ops Handbook/huge.md",
                body=PHONES,
                tombstoned_at=tombstoned,
                tombstone_reason="oversize",
            ),
            SeedDocument(
                path="Ops Handbook/private.md",
                body=PHONES,
                tombstoned_at=tombstoned,
                tombstone_reason="unshared",
            ),
            SeedDocument(
                path="Ops Handbook/gone.md",
                body=PHONES,
                tombstoned_at=tombstoned,
                tombstone_reason="deleted",
            ),
        ],
    )

    source = only_source(ask(client))

    assert source["refusals_by_code"] == {"oversize": 1, "unshared": 1}
    assert source["document_count"] == 1


# ---------------------------------------------------------------------------
# Failing closed on a shape this server does not read
# ---------------------------------------------------------------------------


def test_a_corpus_written_in_an_unknown_shape_is_refused_not_guessed_at(
    client: TestClient, corpus: Corpus
) -> None:
    """A sync that migrated under a live pool: what the engine's check misses.

    It checks once per pool, this checks per read. The version travels because
    "at 9999, reads 3" is a five-minute fix and "unreadable" is an afternoon.
    """
    seed(corpus, source_ref="ops-handbook", docs=[handbook_doc()])
    healthy = ask(client)

    execute(corpus, "UPDATE schema_meta SET version = 9999 WHERE id = 1")
    try:
        degraded = ask(client)
    finally:
        execute(
            corpus,
            "UPDATE schema_meta SET version = :version WHERE id = 1",
            version=healthy["schema_version"],
        )

    assert healthy["schema_supported"] is True
    assert healthy["document_count"] == 1
    assert degraded["schema_supported"] is False
    assert degraded["schema_version"] == 9999
    assert degraded["sources"] == []
    assert degraded["document_count"] == 0
    assert degraded["chunk_count"] == 0
    assert degraded["stale_seconds"] is None
    assert degraded["sources_failing"] == 0


# ---------------------------------------------------------------------------
# The door it sits behind
# ---------------------------------------------------------------------------


def test_the_status_operation_is_registered_or_no_request_reaches_the_handler() -> None:
    """An unregistered operation is a RuntimeError on every call to this route."""
    assert (
        DEFAULT_OPERATION_ACCESS[Operation.COMPANY_KNOWLEDGE_STATUS]
        is AccessLevel.AUTHENTICATED
    )


def test_an_ordinary_key_may_watch_the_mirror_and_an_anonymous_caller_may_not(
    non_admin_client: TestClient, unauthenticated_client: TestClient
) -> None:
    """Oversight must not require a key that also carries ``controls.create``."""
    assert non_admin_client.get(STATUS).status_code == 200
    assert unauthenticated_client.get(STATUS).status_code in (401, 403)


def test_the_status_read_is_a_get_because_it_carries_no_query() -> None:
    """Its two neighbours are POSTs; they take a body, and this takes nothing."""
    from agent_control_server.endpoints.company_knowledge import router

    methods = {route.path: route.methods for route in router.routes}  # type: ignore[attr-defined]

    assert methods["/company-knowledge/status"] == {"GET"}
    assert methods["/company-knowledge/search"] == {"POST"}
