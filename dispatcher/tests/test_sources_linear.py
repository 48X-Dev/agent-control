"""The milestone source, and the one module that knows both source schemes.

Three properties are worth a test each because losing any of them changes what
the tool does rather than how it reads:

* an unreadable scope raises rather than returning an empty list, so a failed
  read and an empty milestone never look the same to whoever ran this;
* creator identity is dropped at :func:`_to_item`, so it reaches the terminal
  and never the envelope; and
* ``write_back`` refuses instead of returning a success-shaped outcome, because
  slice 2 has no write of any kind.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from pathlib import Path

import pytest
from agent_control_dispatcher.sources.base import ScopedTaskSource
from agent_control_dispatcher.sources.file import FileTaskSource, SourceParseError
from agent_control_dispatcher.sources.linear import (
    SOURCE_PREFIX,
    LinearMilestoneSource,
    LinearScopeError,
)
from agent_control_dispatcher.sources.resolve import build_source
from agent_control_models.linear import (
    ListMilestoneIssuesResponse,
    MilestoneIssue,
    MilestoneIssueCounts,
    MilestoneIssueSkipCounts,
    MilestonesStatus,
)

MILESTONE = "3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6"


class FakeReader:
    """Stands in for :class:`DispatchClient`, one method wide."""

    def __init__(self, response: ListMilestoneIssuesResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    async def fetch_milestone_issues(
        self, *, team_slug: str, milestone_id: str
    ) -> ListMilestoneIssuesResponse:
        self.calls.append((team_slug, milestone_id))
        return self._response


def _issue(ref: str, *, minutes: int = 0, identifier: str = "OPS-1") -> MilestoneIssue:
    return MilestoneIssue(
        ref=ref,
        identifier=identifier,
        title="Review the deck",
        description="Owner noted in request: Clive.",
        url=f"https://linear.app/acme/issue/{identifier}",
        created_at=dt.datetime(2026, 8, 1, 14, 56, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 8, 1, 15, 0, tzinfo=dt.UTC) + dt.timedelta(minutes=minutes),
        creator_id="c087560f",
        creator_display_name="paul",
    )


def _response(
    *issues: MilestoneIssue,
    status: MilestonesStatus = MilestonesStatus.OK,
    error: str | None = None,
    started: int = 0,
    assigned: int = 0,
    other_team: int = 0,
    beyond_page_cap: bool = False,
) -> ListMilestoneIssuesResponse:
    return ListMilestoneIssuesResponse(
        status=status,
        slug="operations",
        linear_team_key="OPS",
        milestone_id=MILESTONE,
        issues=list(issues),
        counts=MilestoneIssueCounts(
            fetched=len(issues) + started + assigned,
            eligible=len(issues),
            skipped=MilestoneIssueSkipCounts(
                started=started, assigned=assigned, other_team=other_team
            ),
            beyond_page_cap=beyond_page_cap,
        ),
        error=error,
        cached=False,
        fetched_at=dt.datetime(2026, 8, 3, 8, 19, tzinfo=dt.UTC),
    )


def _digest(refs: list[str]) -> str:
    """Section 5.2's ``refs_digest``, computed here rather than shipped.

    Nothing in slice 2 sends one - ``expected_refs_digest`` is Phase 4 - but the
    property it depends on is a property of *this* code, so it is worth pinning
    now: the same milestone, unchanged, has to hash the same twice.
    """

    return hashlib.sha256("\n".join(refs).encode()).hexdigest()


def _source(response: ListMilestoneIssuesResponse) -> tuple[LinearMilestoneSource, FakeReader]:
    reader = FakeReader(response)
    return (
        LinearMilestoneSource(reader=reader, milestone_id=MILESTONE, team_slug="operations"),
        reader,
    )


# =============================================================================
# Resolving --source and --team
# =============================================================================


def test_a_milestone_spec_needs_a_team() -> None:
    with pytest.raises(SourceParseError) as excinfo:
        build_source(f"{SOURCE_PREFIX}{MILESTONE}", team_slug=None, reader=FakeReader(_response()))

    assert "--team" in str(excinfo.value)


def test_an_empty_milestone_id_is_refused() -> None:
    with pytest.raises(SourceParseError):
        build_source(SOURCE_PREFIX, team_slug="operations", reader=FakeReader(_response()))


@pytest.mark.parametrize(
    "spec",
    [
        f"{SOURCE_PREFIX}   ",
        f"{SOURCE_PREFIX}//{MILESTONE}",
        f"{SOURCE_PREFIX}../../../agent-sessions",
        f"{SOURCE_PREFIX}{MILESTONE}/issues",
        f"{SOURCE_PREFIX}a b",
        f"{SOURCE_PREFIX}?first=1000",
    ],
)
def test_a_malformed_milestone_ref_is_refused_before_any_request(spec: str) -> None:
    """The id becomes a path segment, and httpx resolves dot segments itself.

    ``linear-milestone:../../../agent-sessions`` would otherwise be a GET
    against a route nobody named on the command line. Refused here instead, and
    the reader is never called.
    """

    reader = FakeReader(_response())

    with pytest.raises(SourceParseError):
        build_source(spec, team_slug="operations", reader=reader)

    assert reader.calls == []


@pytest.mark.parametrize(
    "spec",
    [f"linear://{MILESTONE}", f"linear-milestones://{MILESTONE}"],
)
def test_a_guessed_linear_scheme_is_told_the_right_spelling(spec: str) -> None:
    with pytest.raises(SourceParseError) as excinfo:
        build_source(spec, team_slug=None, reader=FakeReader(_response()))

    assert SOURCE_PREFIX in str(excinfo.value)


def test_a_team_is_refused_for_a_file_rather_than_quietly_ignored() -> None:
    with pytest.raises(SourceParseError) as excinfo:
        build_source("file://tasks.yaml", team_slug="operations", reader=FakeReader(_response()))

    assert "--team" in str(excinfo.value)


def test_a_file_still_resolves_to_the_file_source() -> None:
    source = build_source("file://tasks.yaml", team_slug=None, reader=FakeReader(_response()))

    assert isinstance(source, FileTaskSource)
    assert source.describe() == f"file://{Path('tasks.yaml')}"


def test_a_milestone_spec_resolves_to_the_linear_source() -> None:
    source = build_source(
        f"{SOURCE_PREFIX}{MILESTONE}", team_slug="operations", reader=FakeReader(_response())
    )

    assert isinstance(source, LinearMilestoneSource)
    assert source.kind == "linear"
    assert MILESTONE in source.describe()
    assert "operations" in source.describe()


def test_only_a_scoped_source_reports_a_scope() -> None:
    file_source = build_source("file://tasks.yaml", team_slug=None, reader=FakeReader(_response()))
    linear_source, _ = _source(_response())

    assert not isinstance(file_source, ScopedTaskSource)
    assert isinstance(linear_source, ScopedTaskSource)


# =============================================================================
# A read that failed is not a read that found nothing
# =============================================================================


@pytest.mark.parametrize(
    "status",
    [
        MilestonesStatus.ERROR,
        MilestonesStatus.NOT_CONFIGURED,
        # Unreachable through the endpoint, which refuses an unlinked team with
        # a 409. Refused here anyway: a status this side does not understand
        # must not be read as "nothing to do".
        MilestonesStatus.NOT_LINKED,
    ],
)
def test_an_unreadable_scope_raises_rather_than_looking_empty(
    status: MilestonesStatus,
) -> None:
    source, _ = _source(_response(status=status, error="Linear could not be reached."))

    with pytest.raises(LinearScopeError):
        asyncio.run(source.poll(cursor=None))


def test_an_empty_milestone_is_an_ordinary_empty_list() -> None:
    source, _ = _source(_response(status=MilestonesStatus.EMPTY, started=2))

    assert asyncio.run(source.poll(cursor=None)) == []
    assert source.scope_report is not None
    assert source.scope_report.skipped_started == 2


# =============================================================================
# What the operator is shown, and what the agent is shown
# =============================================================================


def test_the_scope_report_names_every_skip_reason() -> None:
    source, _ = _source(
        _response(_issue("a"), started=2, assigned=3, other_team=6, beyond_page_cap=True)
    )

    asyncio.run(source.poll(cursor=None))
    report = source.scope_report
    assert report is not None
    rendered = "\n".join(report.lines())

    assert "2 already started by a person" in rendered
    assert "3 assigned to a person" in rendered
    assert "6 belonging to another team" in rendered
    assert "page cap" in rendered


def test_creator_identity_stops_at_the_confirm() -> None:
    """It reaches the terminal. It must not reach the envelope."""

    source, _ = _source(_response(_issue("a")))

    item = asyncio.run(source.poll(cursor=None))[0]

    assert not hasattr(item, "creator_id")
    assert not hasattr(item, "creator_display_name")
    assert "paul" not in item.title
    assert "paul" not in item.body


def test_the_ref_is_linears_id_and_the_key_rides_in_the_title() -> None:
    source, _ = _source(_response(_issue("uuid-1", identifier="OPS-2")))

    item = asyncio.run(source.poll(cursor=None))[0]

    assert item.ref == "uuid-1"
    assert item.title.startswith("OPS-2: ")


def test_items_come_back_oldest_change_first() -> None:
    source, _ = _source(
        _response(
            _issue("newest", minutes=10),
            _issue("oldest", minutes=0),
            _issue("middle", minutes=5),
        )
    )

    items = asyncio.run(source.poll(cursor=None))

    assert [item.ref for item in items] == ["oldest", "middle", "newest"]


def test_a_cursor_resumes_after_the_item_it_names() -> None:
    source, _ = _source(
        _response(_issue("a", minutes=0), _issue("b", minutes=1), _issue("c", minutes=2))
    )

    items = asyncio.run(source.poll(cursor="a"))

    assert [item.ref for item in items] == ["b", "c"]


def test_a_cursor_naming_a_departed_item_restarts_from_the_top() -> None:
    source, _ = _source(_response(_issue("a"), _issue("b", minutes=1)))

    items = asyncio.run(source.poll(cursor="gone"))

    assert [item.ref for item in items] == ["a", "b"]


# =============================================================================
# No write of any kind
# =============================================================================


def test_repeated_reads_of_unchanged_data_produce_the_same_order_and_digest() -> None:
    """``orderBy: updatedAt`` plus a total tiebreak is what makes this hold.

    Section 5.2 wants a digest over the refs to identify a set, so that a later
    phase can refuse a press against a set that has moved underneath it. That
    digest is only worth anything if two reads of an unchanged milestone agree,
    which means the ordering has to be total: ``updated_at`` alone is not, and
    two issues touched in the same millisecond would otherwise swap places and
    change which three ``--max-tasks 3`` picks.
    """

    same_moment = [
        _issue("b-second", minutes=5),
        _issue("a-first", minutes=5),
        _issue("c-later", minutes=9),
    ]
    source, _ = _source(_response(*same_moment))
    other, _ = _source(_response(*reversed(same_moment)))

    first_refs = [item.ref for item in asyncio.run(source.poll(cursor=None))]
    second_refs = [item.ref for item in asyncio.run(source.poll(cursor=None))]
    reordered_refs = [item.ref for item in asyncio.run(other.poll(cursor=None))]

    assert first_refs == second_refs == reordered_refs == ["a-first", "b-second", "c-later"]
    assert _digest(first_refs) == _digest(second_refs) == _digest(reordered_refs)


def test_an_issue_with_no_updated_at_still_sorts_rather_than_crashing() -> None:
    dated = _issue("dated", minutes=5)
    undated = _issue("undated").model_copy(update={"updated_at": None})
    source, _ = _source(_response(dated, undated))

    items = asyncio.run(source.poll(cursor=None))

    assert [item.ref for item in items] == ["undated", "dated"]


def test_a_naive_timestamp_does_not_crash_the_sort() -> None:
    """Linear sends UTC. A mixed page must not raise on the comparison anyway."""

    naive = _issue("naive").model_copy(update={"updated_at": dt.datetime(2026, 8, 1, 14, 0)})
    source, _ = _source(_response(_issue("aware", minutes=5), naive))

    assert [item.ref for item in asyncio.run(source.poll(cursor=None))] == [
        "naive",
        "aware",
    ]


def test_a_cached_set_is_reported_as_cached_with_the_original_read_time() -> None:
    """How old the set is, on screen, because the operator is authorising it."""

    response = _response(_issue("a"))
    source, _ = _source(response.model_copy(update={"cached": True}))

    asyncio.run(source.poll(cursor=None))
    report = source.scope_report
    assert report is not None

    assert report.cached is True
    assert "(cached)" in "\n".join(report.lines())
    assert report.fetched_at == response.fetched_at


def test_the_source_asks_only_for_the_scope_it_was_built_with() -> None:
    source, reader = _source(_response(_issue("a")))

    asyncio.run(source.poll(cursor=None))

    assert reader.calls == [("operations", MILESTONE)]


def test_the_linear_source_refuses_to_write_back() -> None:
    source, reader = _source(_response(_issue("a")))

    with pytest.raises(NotImplementedError):
        asyncio.run(source.write_back(item_ref="a", body="report", idempotency_marker="m"))

    assert reader.calls == []
