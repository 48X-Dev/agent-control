"""The GitHub half of a run, driven directly rather than through ``run_once``.

The adapter is not wired into the run loop yet, so these drive it against a fake writer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync.allowlist import RepoConfig, RepoRef
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials
from agent_control_knowledge_sync.github_client import GitHubClient
from agent_control_knowledge_sync.github_source import (
    TOMBSTONE_DELETED,
    TOMBSTONE_EXCLUDED,
    GitHubDocument,
    GitHubSource,
    RepoSweep,
    WriteOutcome,
    source_mime_for,
)

from tests.fakes.github import FakeGitHub, FakeRepo

REPO = RepoRef(owner="earlycore", name="agent-control")
SECOND = RepoRef(owner="earlycore", name="handbook")
HEAD = "a" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

CREDS = DriveCredentials(
    client_id="123456789012-abcdefg.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)


class FakeWriter:
    """A corpus that records what it was asked to do, and nothing more."""

    def __init__(self, *, live: set[str] | None = None) -> None:
        self.documents: dict[str, GitHubDocument] = {}
        self.tombstones: list[tuple[str, str]] = []
        self.live: set[str] = set(live or set())
        self.unchanged: set[str] = set()
        self.refusals: dict[str, str] = {}

    async def write(self, document: GitHubDocument) -> WriteOutcome:
        refusal = self.refusals.get(document.external_id)
        if refusal is not None:
            return WriteOutcome(refusal_code=refusal)
        if document.external_id in self.unchanged:
            return WriteOutcome(unchanged=True)
        self.documents[document.external_id] = document
        self.live.add(document.external_id)
        return WriteOutcome(indexed=True)

    async def tombstone(self, external_id: str, *, reason: str) -> bool:
        if external_id not in self.live:
            return False
        self.live.discard(external_id)
        self.tombstones.append((external_id, reason))
        return True

    async def live_external_ids(self) -> set[str]:
        return set(self.live)


def _config(**overrides: Any) -> SyncConfig:
    base: dict[str, Any] = {
        "credentials": CREDS,
        "root_folder_id": "0ABsharedDriveRoot",
        "database_url": "postgresql+psycopg://knowledge_sync@localhost/agent_knowledge",
    }
    base.update(overrides)
    return SyncConfig(**base)


def _source(
    hub: FakeGitHub,
    *,
    repos: tuple[RepoConfig, ...] = (RepoConfig(repo=REPO),),
    **overrides: Any,
) -> GitHubSource:
    config = _config(**overrides)
    http = httpx.AsyncClient(transport=hub.transport())
    client = GitHubClient("ghp_not_a_real_token", http, config, repos=repos)
    return GitHubSource(client, repos, config)


def _hub(*paths: str, **repo_kwargs: Any) -> tuple[FakeGitHub, FakeRepo]:
    hub = FakeGitHub()
    repo = hub.repo(REPO.full_name, head=HEAD, **repo_kwargs)
    for path in paths:
        repo.add(path, f"# {path}\n")
    return hub, repo


async def _sweep(source: GitHubSource, writer: FakeWriter, **kwargs: Any) -> RepoSweep:
    return await source.sweep(source.repos[0], writer, **kwargs)


@pytest.mark.asyncio
class TestTheFirstWalk:
    async def test_it_indexes_the_set_and_earns_the_head_as_its_cursor(self) -> None:
        hub, _ = _hub("README.md", "docs/plans/task-dispatcher.md", "src/main.py")
        writer = FakeWriter()
        sweep = await _sweep(_source(hub), writer)
        assert sweep.status == "ok"
        assert sweep.cursor == HEAD
        assert sweep.indexed == 2
        assert sorted(writer.documents) == [
            "earlycore/agent-control:README.md",
            "earlycore/agent-control:docs/plans/task-dispatcher.md",
        ]

    async def test_the_document_carries_the_citation_form_from_the_plan(self) -> None:
        hub, _ = _hub("docs/plans/task-dispatcher.md")
        writer = FakeWriter()
        await _sweep(_source(hub), writer)
        document = writer.documents["earlycore/agent-control:docs/plans/task-dispatcher.md"]
        assert document.path == "agent-control:docs/plans/task-dispatcher.md"
        assert document.title == "task-dispatcher.md"
        assert document.source_mime == "text/markdown"
        assert document.data == b"# docs/plans/task-dispatcher.md\n"

    async def test_the_head_commit_date_is_what_a_file_is_dated_by(self) -> None:
        hub, _ = _hub("README.md")
        writer = FakeWriter()
        await _sweep(_source(hub), writer)
        stamped = writer.documents["earlycore/agent-control:README.md"].modified_at
        assert stamped is not None
        assert stamped.year == 2026

    async def test_an_unchanged_document_is_counted_apart_from_an_indexed_one(self) -> None:
        hub, _ = _hub("README.md")
        writer = FakeWriter()
        writer.unchanged.add("earlycore/agent-control:README.md")
        sweep = await _sweep(_source(hub), writer)
        assert (sweep.indexed, sweep.unchanged, sweep.seen) == (0, 1, 1)

    async def test_a_writer_refusal_is_counted_by_its_own_code(self) -> None:
        hub, _ = _hub("README.md")
        writer = FakeWriter()
        writer.refusals["earlycore/agent-control:README.md"] = "all_chunks_scrubbed"
        sweep = await _sweep(_source(hub), writer)
        assert sweep.refusals["all_chunks_scrubbed"] == 1
        assert sweep.indexed == 0


@pytest.mark.asyncio
class TestWhatIsRefusedDuringASweep:
    async def test_a_binary_under_docs_is_skipped_with_a_count_not_converted(self) -> None:
        hub, repo = _hub("docs/notes.md")
        repo.add("docs/diagram.png", PNG)
        writer = FakeWriter()
        sweep = await _sweep(_source(hub), writer)
        assert sweep.refusals["binary"] == 1
        assert list(writer.documents) == ["earlycore/agent-control:docs/notes.md"]

    async def test_the_path_filters_are_attributed_to_the_repo_that_tripped_them(self) -> None:
        hub, _ = _hub("README.md", "node_modules/x/README.md", "docs/.env")
        sweep = await _sweep(_source(hub), FakeWriter())
        assert sweep.refusals["denied_path"] == 1
        assert sweep.refusals["secret_file"] == 1

    async def test_an_oversize_file_is_refused_and_the_rest_of_the_repo_still_indexes(self) -> None:
        hub, repo = _hub("README.md")
        repo.add("docs/huge.md", "x" * 5_000)
        writer = FakeWriter()
        sweep = await _sweep(_source(hub, max_file_bytes=1_000), writer)
        assert sweep.refusals["oversize"] == 1
        assert sweep.indexed == 1


@pytest.mark.asyncio
class TestNothingIsDeletedWithoutEvidence:
    """Each of these is a failure to learn something, not a fact about a document."""

    async def test_an_unreachable_repo_tombstones_nothing(self) -> None:
        hub = FakeGitHub()
        writer = FakeWriter(live={"earlycore/agent-control:README.md"})
        sweep = await _sweep(_source(hub), writer)
        assert (sweep.status, sweep.error_code) == ("failed", "repo_unreachable")
        assert sweep.cursor is None
        assert writer.tombstones == []

    async def test_a_transport_failure_tombstones_nothing(self) -> None:
        hub, _ = _hub("README.md")
        hub.fail_next("/repos/earlycore/agent-control", 503, 503, 503, 503, 503)
        writer = FakeWriter(live={"earlycore/agent-control:README.md"})
        sweep = await _sweep(_source(hub), writer)
        assert (sweep.status, sweep.error_code) == ("failed", "github_unreachable")
        assert writer.tombstones == []

    async def test_a_truncated_tree_tombstones_nothing(self) -> None:
        hub, repo = _hub("README.md")
        repo.truncated = True
        writer = FakeWriter(live={"earlycore/agent-control:gone.md"})
        sweep = await _sweep(_source(hub), writer)
        assert (sweep.status, sweep.error_code) == ("failed", "tree_truncated")
        assert writer.tombstones == []

    async def test_a_walk_stopped_by_the_ceiling_tombstones_nothing_and_keeps_its_cursor(
        self,
    ) -> None:
        hub, _ = _hub("README.md", "docs/a.md", "docs/b.md")
        writer = FakeWriter(live={"earlycore/agent-control:gone.md"})
        sweep = await _sweep(_source(hub, max_documents_per_run=2), writer)
        assert (sweep.status, sweep.error_code) == ("partial", "source_ceiling")
        assert sweep.cursor is None
        assert writer.tombstones == []


@pytest.mark.asyncio
class TestRemovalOnEvidence:
    async def test_a_complete_tree_that_lost_a_file_tombstones_it(self) -> None:
        hub, _ = _hub("README.md")
        writer = FakeWriter(live={"earlycore/agent-control:docs/old.md"})
        sweep = await _sweep(_source(hub), writer)
        assert writer.tombstones == [("earlycore/agent-control:docs/old.md", TOMBSTONE_EXCLUDED)]
        assert sweep.tombstoned == 1

    async def test_another_repos_documents_are_left_alone(self) -> None:
        hub, _ = _hub("README.md")
        writer = FakeWriter(live={"earlycore/handbook:docs/old.md"})
        await _sweep(_source(hub), writer)
        assert writer.tombstones == []

    async def test_a_compare_removal_tombstones_as_deleted(self) -> None:
        hub, repo = _hub("docs/a.md")
        repo.set_compare("b" * 40, modified=("docs/a.md",), removed=("docs/gone.md",))
        writer = FakeWriter(live={"earlycore/agent-control:docs/gone.md"})
        sweep = await _sweep(_source(hub), writer, cursor="b" * 40)
        assert writer.tombstones == [("earlycore/agent-control:docs/gone.md", TOMBSTONE_DELETED)]
        assert sweep.indexed == 1
        assert sweep.cursor == HEAD


@pytest.mark.asyncio
class TestTheIncrementalPath:
    async def test_an_unmoved_head_reads_nothing_and_keeps_the_cursor(self) -> None:
        hub, _ = _hub("README.md")
        writer = FakeWriter()
        sweep = await _sweep(_source(hub), writer, cursor=HEAD)
        assert (sweep.seen, sweep.indexed) == (0, 0)
        assert sweep.cursor == HEAD
        assert writer.documents == {}

    async def test_a_force_push_relists_the_whole_repo_and_says_so(self) -> None:
        hub, _ = _hub("README.md", "docs/a.md")
        writer = FakeWriter()
        sweep = await _sweep(_source(hub), writer, cursor="b" * 40)
        assert (sweep.status, sweep.error_code) == ("partial", "force_push_relist")
        assert sweep.cursor == HEAD
        assert sweep.indexed == 2

    async def test_a_force_push_relist_that_hits_the_ceiling_keeps_the_ceilings_code(self) -> None:
        hub, _ = _hub("README.md", "docs/a.md", "docs/b.md")
        sweep = await _sweep(
            _source(hub, max_documents_per_run=2), FakeWriter(), cursor="b" * 40
        )
        assert (sweep.status, sweep.error_code) == ("partial", "source_ceiling")
        assert sweep.cursor is None


@pytest.mark.asyncio
class TestSweepingEveryRepo:
    def _two(self) -> tuple[FakeGitHub, tuple[RepoConfig, ...]]:
        hub = FakeGitHub()
        first = hub.repo(REPO.full_name, head=HEAD)
        first.add("README.md", "# one\n")
        second = hub.repo(SECOND.full_name, head="d" * 40)
        second.add("README.md", "# two\n")
        return hub, (RepoConfig(repo=REPO), RepoConfig(repo=SECOND))

    async def test_each_repo_gets_its_own_cursor(self) -> None:
        hub, repos = self._two()
        source = _source(hub, repos=repos)
        sweeps = await source.sweep_all(FakeWriter())
        assert [(item.repo.full_name, item.cursor) for item in sweeps] == [
            ("earlycore/agent-control", HEAD),
            ("earlycore/handbook", "d" * 40),
        ]

    async def test_the_stored_cursor_is_looked_up_by_full_name(self) -> None:
        hub, repos = self._two()
        source = _source(hub, repos=repos)
        sweeps = await source.sweep_all(
            FakeWriter(), cursors={"earlycore/agent-control": HEAD}
        )
        assert sweeps[0].seen == 0
        assert sweeps[1].indexed == 1

    async def test_the_document_budget_is_shared_across_repos(self) -> None:
        hub, repos = self._two()
        source = _source(hub, repos=repos, max_documents_per_run=1)
        sweeps = await source.sweep_all(FakeWriter())
        assert len(sweeps) == 1
        assert sweeps[0].indexed == 1

    async def test_a_repo_that_fails_does_not_stop_the_next_one(self) -> None:
        hub, repos = self._two()
        del hub.repos[REPO.full_name.lower()]
        sweeps = await _source(hub, repos=repos).sweep_all(FakeWriter())
        assert sweeps[0].status == "failed"
        assert sweeps[1].status == "ok"


class TestTheMimeGuess:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("README.md", "text/markdown"),
            ("docs/a.markdown", "text/markdown"),
            ("README", "text/markdown"),
            ("docs/notes.txt", "text/plain"),
            ("docs/api.rst", "text/plain"),
            ("docs/rows.csv", "text/csv"),
            ("docs/page.html", "text/html"),
        ],
    )
    def test_the_extension_names_a_type_the_converter_reads(
        self, path: str, expected: str
    ) -> None:
        assert source_mime_for(path) == expected
