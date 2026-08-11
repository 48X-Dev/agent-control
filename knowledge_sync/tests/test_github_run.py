"""The gate on the GitHub channel and the routing behind it, without a corpus.

Both halves configured or it is off, and a deployment that never heard of GitHub is off.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials
from agent_control_knowledge_sync.github_run import (
    RepoWriter,
    _source_item,
    github_channel,
    github_journal,
)
from agent_control_knowledge_sync.github_source import CURSOR_KEY, SOURCE_KIND, GitHubDocument
from agent_control_knowledge_sync.ingest import IngestOutcome, SourceItem
from agent_control_knowledge_sync.journal import SyncFailedError
from sqlalchemy.dialects import postgresql

CREDS = DriveCredentials(
    client_id="123456789012-abcdefg.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)

ALLOWLIST = """
github:
  repos:
    - repo: earlycore/agent-control
"""


def _config(tmp_path: Path, *, token: str | None = "ghp-token", body: str | None = None) -> Any:
    path = tmp_path / "knowledge.yaml"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    return SyncConfig(
        credentials=CREDS,
        root_folder_id="root-1",
        database_url="postgresql+psycopg://knowledge_sync:x@localhost/agent_knowledge",
        github_token=token,
        allowlist_path=path,
    )


class FakeIngestor:
    """One repo's writer, recording what reached it."""

    def __init__(self, *, live: set[str] | None = None) -> None:
        self.written: list[SourceItem] = []
        self.tombstoned: list[tuple[str, str]] = []
        self.live = live or set()
        self.secrets_skipped = 2

    async def ingest(self, item: SourceItem) -> IngestOutcome:
        self.written.append(item)
        return IngestOutcome("1", 3, False, None)

    async def tombstone(self, external_id: str, *, reason: str = "deleted") -> bool:
        self.tombstoned.append((external_id, reason))
        return True

    async def live_external_ids(self) -> set[str]:
        return set(self.live)


class FakeRow:
    id = 7
    cursor = None


class FakeSession:
    """Records the statements a journal builds, so the SQL is the assertion."""

    def __init__(self) -> None:
        self.executed: list[tuple[Any, Any]] = []

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append((statement, params))
        return self

    def one(self) -> FakeRow:
        return FakeRow()

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _document(external_id: str, *, path: str = "docs/plan.md") -> GitHubDocument:
    return GitHubDocument(
        external_id=external_id,
        path=path,
        title="plan.md",
        source_mime="text/markdown",
        modified_at=None,
        size=6,
        data=b"# plan",
    )


# --- the gate ---------------------------------------------------------------


def test_no_token_leaves_the_channel_off_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A deployment that never heard of GitHub must sync Drive exactly as before."""
    with caplog.at_level(logging.INFO):
        assert github_channel(_config(tmp_path, token=None, body=ALLOWLIST)) is None

    assert "AGENT_KNOWLEDGE_GITHUB_TOKEN is unset" in caplog.text


def test_no_allowlist_file_leaves_the_channel_off_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        assert github_channel(_config(tmp_path)) is None

    assert "lists no repositories" in caplog.text


def test_an_empty_allowlist_indexes_nothing_rather_than_everything(tmp_path: Path) -> None:
    """Section 6: this file is the only thing enforcing scope under a classic PAT."""
    assert github_channel(_config(tmp_path, body="github:\n  repos: []\n")) is None


def test_both_halves_configured_turns_the_channel_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        channel = github_channel(_config(tmp_path, body=ALLOWLIST))

    assert channel is not None
    assert [item.repo.full_name for item in channel.repos] == ["earlycore/agent-control"]
    assert "github channel on: 1 repo(s)" in caplog.text


def test_a_malformed_allowlist_fails_the_run_rather_than_disabling_the_channel(
    tmp_path: Path,
) -> None:
    """Silently off is the failure mode this whole file exists to refuse."""
    with pytest.raises(SyncFailedError) as caught:
        github_channel(_config(tmp_path, body="github:\n  repos:\n    - repo: '*/*'\n"))

    assert caught.value.code == "allowlist_repo_form"


@pytest.mark.asyncio
async def test_the_github_journal_writes_its_own_kind_and_cursor() -> None:
    """A head sha stored under `start_page_token` is a cursor nothing reads back."""
    assert (SOURCE_KIND, CURSOR_KEY) == ("github_repo", "head_sha")
    session = FakeSession()
    journal = github_journal(lambda: session)  # type: ignore[arg-type,return-value]

    await journal.ensure_source(ref="earlycore/agent-control", display_name="agent-control")
    await journal.advance_cursor(7, "a" * 40)

    ensure, advance = (
        dict(statement.compile(dialect=postgresql.dialect()).params)
        for statement, _ in session.executed
    )
    assert ensure["kind"] == SOURCE_KIND
    assert CURSOR_KEY in advance.values()


# --- the writer -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_routes_to_the_repo_that_owns_the_source_row() -> None:
    """Two repos are two `sources` rows, so the id decides which one a file lands in."""
    first, second = FakeIngestor(), FakeIngestor()
    writer = RepoWriter(
        None,  # type: ignore[arg-type]
        {"earlycore/agent-control": first, "earlycore/handbook": second},
    )

    outcome = await writer.write(_document("earlycore/handbook:docs/plan.md"))

    assert outcome.indexed is True
    assert first.written == []
    assert [item.external_id for item in second.written] == ["earlycore/handbook:docs/plan.md"]


@pytest.mark.asyncio
async def test_a_tombstone_routes_the_same_way() -> None:
    ingestor = FakeIngestor()
    writer = RepoWriter(None, {"earlycore/agent-control": ingestor})  # type: ignore[arg-type]

    assert await writer.tombstone("earlycore/agent-control:docs/old.md", reason="deleted") is True
    assert ingestor.tombstoned == [("earlycore/agent-control:docs/old.md", "deleted")]


@pytest.mark.asyncio
async def test_live_ids_are_the_union_the_reconcile_prefix_filters() -> None:
    writer = RepoWriter(
        None,  # type: ignore[arg-type]
        {
            "earlycore/agent-control": FakeIngestor(live={"earlycore/agent-control:README.md"}),
            "earlycore/handbook": FakeIngestor(live={"earlycore/handbook:docs/a.md"}),
        },
    )

    assert await writer.live_external_ids() == {
        "earlycore/agent-control:README.md",
        "earlycore/handbook:docs/a.md",
    }


@pytest.mark.asyncio
async def test_an_id_naming_no_opened_repo_refuses_rather_than_guessing() -> None:
    writer = RepoWriter(None, {"earlycore/agent-control": FakeIngestor()})  # type: ignore[arg-type]

    with pytest.raises(SyncFailedError) as caught:
        await writer.write(_document("someone/else:docs/plan.md"))

    assert caught.value.code == "github_source_unknown"


@pytest.mark.asyncio
async def test_the_writer_counts_the_bytes_and_the_scrubs_a_run_reports() -> None:
    writer = RepoWriter(None, {"earlycore/agent-control": FakeIngestor()})  # type: ignore[arg-type]

    await writer.write(_document("earlycore/agent-control:docs/plan.md"))

    assert writer.bytes_fetched == 6
    assert writer.secrets_skipped == 2


def test_a_repo_file_keeps_its_own_type_on_both_sides_of_the_conversion() -> None:
    """The extension is all a blob declares, so the fetch's type is the same guess."""
    item = _source_item(_document("earlycore/agent-control:docs/plan.md"))

    assert (item.media_type, item.source_mime) == ("text/markdown", "text/markdown")
    assert item.path == "docs/plan.md"
    assert item.deleted is False
