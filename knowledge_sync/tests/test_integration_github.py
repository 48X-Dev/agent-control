"""GitHub through ``run_once``, against a faked API and a real corpus.

The one thing unit tests cannot see: whether the channel is actually registered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.sync import run_once

from tests.conftest import execute, query, scalar
from tests.fakes.drive import FakeDrive
from tests.fakes.github import FakeGitHub
from tests.integration_support import CREDENTIALS, ROOT_ID, config_for, populate

pytestmark = pytest.mark.asyncio

REPO = "earlycore/agent-control"

ALLOWLIST = f"""
github:
  repos:
    - repo: {REPO}
"""

DISPATCHER = """# Task dispatcher

The dispatcher leases one task at a time and renews the lease every thirty
seconds while the executor works. A lease that lapses is stolen by the next
claimant, which marks the orphaned row and carries on rather than waiting for a
human to notice that a container died in the night.
"""

README = """# agent-control

The control plane, the executors and the knowledge corpus live in this
repository. Each half ships with the compose passthrough, the Apple script line
and the environment example that make it actually reach the container it is for.
"""


def github_config(corpus: Any, allowlist: Path) -> SyncConfig:
    """The Drive config every run in here uses, plus the GitHub half."""
    return SyncConfig(
        credentials=CREDENTIALS,
        root_folder_id=ROOT_ID,
        database_url=corpus.sync_url,
        max_file_bytes=1_000_000,
        max_documents_per_run=50,
        request_timeout_seconds=5.0,
        github_token="ghp-not-a-real-token",
        allowlist_path=allowlist,
    )


def allowlist_file(tmp_path: Path, body: str = ALLOWLIST) -> Path:
    path = tmp_path / "knowledge.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def populate_repo(github: FakeGitHub) -> None:
    """One allowlisted repo holding the two shapes slice one indexes."""
    repo = github.repo(REPO)
    repo.add("README.md", README)
    repo.add("docs/plans/task-dispatcher.md", DISPATCHER)
    repo.add("src/agent_control/dispatcher.py", "# not indexed in slice one\n")


def github_source(corpus: Any) -> dict[str, Any]:
    rows = query(corpus, "SELECT * FROM sources WHERE kind = 'github_repo'")
    assert rows, "the run registered no source for the allowlisted repo"
    return dict(rows[0])


# --- the channel, registered ------------------------------------------------


async def test_a_repos_files_reach_the_corpus_through_run_once(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """Phases 5 and 6 were dead code until the run loop asked for them."""
    populate(drive)
    populate_repo(github)

    counters = await run_once(github_config(corpus, allowlist_file(tmp_path)))

    # The citation is the repo's own name; the identity carries the owner too.
    paths = {row["path"] for row in query(corpus, "SELECT path FROM documents")}
    assert "agent-control:README.md" in paths
    assert "agent-control:docs/plans/task-dispatcher.md" in paths
    ids = {row["external_id"] for row in query(corpus, "SELECT external_id FROM documents")}
    assert f"{REPO}:README.md" in ids
    # Three Drive files and the two indexable repo files, the source tree refused.
    assert counters.indexed == 5


async def test_a_repo_file_is_searchable_rather_than_merely_stored(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """A row with no chunks is unfindable on purpose; these are meant to be found."""
    populate(drive)
    populate_repo(github)

    await run_once(github_config(corpus, allowlist_file(tmp_path)))

    found = scalar(
        corpus,
        "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE d.external_id = :id",
        id=f"{REPO}:docs/plans/task-dispatcher.md",
    )
    assert found > 0


async def test_the_repo_source_row_carries_the_kind_and_the_head_sha(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """4.2 spells the GitHub cursor {"head_sha": ...}; a page token here reads back as nothing."""
    populate(drive)
    populate_repo(github)

    await run_once(github_config(corpus, allowlist_file(tmp_path)))

    source = github_source(corpus)
    assert (source["kind"], source["ref"]) == ("github_repo", REPO)
    assert source["cursor"] == {"head_sha": "a" * 40}
    assert source["last_run_status"] == "ok"
    assert source["last_verified_at"] is not None


async def test_a_second_run_diffs_the_head_instead_of_walking_again(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """5.1: an unchanged head is a verification, and it costs no tree call."""
    populate(drive)
    populate_repo(github)
    allowlist = allowlist_file(tmp_path)
    await run_once(github_config(corpus, allowlist))
    drive.set_changes("t1", changed=(), new_token="t1")
    github.requests.clear()

    counters = await run_once(github_config(corpus, allowlist))

    assert not [path for path in github.paths() if "/git/trees/" in path]
    assert counters.indexed == 0
    assert github_source(corpus)["last_verified_at"] is not None


async def test_a_file_gone_from_a_rewritten_history_is_tombstoned_not_left_searchable(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """The reconcile diffs the walk against what the corpus still holds live."""
    populate(drive)
    repo = github.repo(REPO)
    repo.add("README.md", README)
    repo.add("docs/plans/task-dispatcher.md", DISPATCHER)
    allowlist = allowlist_file(tmp_path)
    await run_once(github_config(corpus, allowlist))
    # A force push: the stored base is no longer an ancestor, so compare 404s
    # and the repo is walked whole. That walk is also the removal evidence.
    del repo.files["docs/plans/task-dispatcher.md"]
    repo.head = "b" * 40
    drive.set_changes("t1", changed=(), new_token="t1")

    await run_once(github_config(corpus, allowlist))

    gone = query(
        corpus,
        "SELECT tombstoned_at, tombstone_reason FROM documents WHERE external_id = :id",
        id=f"{REPO}:docs/plans/task-dispatcher.md",
    )
    assert gone[0]["tombstoned_at"] is not None
    assert gone[0]["tombstone_reason"] == "excluded"
    assert github_source(corpus)["last_run_error_code"] == "force_push_relist"


async def test_a_repo_the_run_could_not_read_is_not_an_ok_run(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """Drive succeeded, so only the run row can say that half the mirror is missing."""
    populate(drive)

    counters = await run_once(github_config(corpus, allowlist_file(tmp_path)))

    assert counters.indexed == 3
    last = query(corpus, "SELECT status, error_code FROM sync_runs ORDER BY id DESC LIMIT 1")[0]
    assert (last["status"], last["error_code"]) == ("partial", "repo_unreachable")
    source = github_source(corpus)
    assert (source["last_run_status"], source["last_run_error_code"]) == (
        "failed",
        "repo_unreachable",
    )
    # A repo nobody could read is not a repo anybody verified.
    assert source["last_verified_at"] is None
    assert source["cursor"] is None


async def test_a_repo_the_allowlist_does_not_name_is_not_read(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """Under a classic PAT the credential would have answered, so this file is the boundary."""
    populate(drive)
    populate_repo(github)
    github.repo("someone/private").add("README.md", "# secrets\n")

    await run_once(github_config(corpus, allowlist_file(tmp_path)))

    assert not [path for path in github.paths() if "someone/private" in path]
    assert not query(corpus, "SELECT id FROM documents WHERE path LIKE 'someone/private%'")


# --- Phase 6, dark until a repo opts in -------------------------------------


ISSUES_ALLOWLIST = f"""
github:
  repos:
    - repo: {REPO}
      github_issues_enabled: true
"""


def issue_row(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Lease renewal drops the last batch",
        "body": (
            "A dispatcher that dies between renewing its lease and committing the "
            "batch leaves the batch unapplied and the cursor already advanced, so "
            "the next claimant never replays it. The fix advances the cursor last."
        ),
        "updated_at": "2026-08-09T18:20:00Z",
        "user": {"login": "someone"},
        "author_association": "MEMBER",
    }


async def test_the_issue_channel_is_dark_unless_a_repo_opts_in(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """`github_issues_enabled` defaults false, so no source row and no call."""
    populate(drive)
    repo = github.repo(REPO, private=True, issues=[issue_row(214)])
    repo.add("README.md", README)

    await run_once(github_config(corpus, allowlist_file(tmp_path)))

    assert not [path for path in github.paths() if path.endswith("/issues")]
    assert query(corpus, "SELECT id FROM sources WHERE ref LIKE '%#issues'") == []


async def test_an_opted_in_private_repo_lands_its_issue_text(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """Phase 6 was dead code too; this is the run loop asking for it."""
    populate(drive)
    repo = github.repo(REPO, private=True, issues=[issue_row(214)])
    repo.add("README.md", README)

    await run_once(github_config(corpus, allowlist_file(tmp_path, ISSUES_ALLOWLIST)))

    stored = query(corpus, "SELECT * FROM documents WHERE external_id = 'issue:214'")
    assert stored, "the issue channel wrote nothing"
    # A membership call the credential cannot answer is undetermined, and
    # undetermined reads external rather than being promoted to workspace.
    assert stored[0]["author_kind"] == "external"
    source = query(corpus, "SELECT * FROM sources WHERE ref = :ref", ref=f"{REPO}#issues")
    assert source[0]["trust"] == "external_authors"
    assert source[0]["cursor"] is not None


async def test_an_opted_in_public_repo_is_refused_by_name_and_counted(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    """Section 7: a public repo is where arbitrary strangers write into the corpus."""
    populate(drive)
    github.repo(REPO, issues=[issue_row(214)]).add("README.md", README)

    counters = await run_once(github_config(corpus, allowlist_file(tmp_path, ISSUES_ALLOWLIST)))

    assert counters.refusals_by_code.get("public_repo_issue_text_refused") == 1
    assert query(corpus, "SELECT id FROM documents WHERE external_id = 'issue:214'") == []


# --- the deployment that never heard of GitHub ------------------------------


async def test_a_deployment_with_no_github_configuration_syncs_drive_as_before(
    drive: FakeDrive, github: FakeGitHub, corpus: Any
) -> None:
    """The whole point of the gate: an existing deployment's behaviour cannot move."""
    populate(drive)
    populate_repo(github)

    counters = await run_once(config_for(corpus))

    assert counters.indexed == 3
    assert github.requests == []
    assert query(corpus, "SELECT id FROM sources WHERE kind = 'github_repo'") == []
    titles = {row["title"] for row in query(corpus, "SELECT title FROM documents")}
    assert titles == {"laptops.md", "phones.md", "releases.md"}


async def test_a_token_with_no_allowlist_file_is_off_rather_than_everything(
    drive: FakeDrive, github: FakeGitHub, corpus: Any, tmp_path: Path
) -> None:
    populate(drive)
    populate_repo(github)

    counters = await run_once(github_config(corpus, tmp_path / "absent.yaml"))

    assert counters.indexed == 3
    assert github.requests == []


# --- 4.4's retention sweep --------------------------------------------------


async def test_a_tombstone_past_its_window_is_deleted_by_the_next_pass(
    drive: FakeDrive, corpus: Any
) -> None:
    """Nothing in the corpus is older than today, so the age has to be constructed."""
    populate(drive)
    config = config_for(corpus)
    await run_once(config)
    old = datetime.now(UTC) - timedelta(days=200)
    execute(
        corpus,
        "UPDATE documents SET tombstoned_at = :when, tombstone_reason = 'deleted' "
        "WHERE external_id = :id",
        when=old,
        id="file-phones",
    )
    drive.set_changes("t1", changed=(), new_token="t2")

    await run_once(config)

    assert query(corpus, "SELECT id FROM documents WHERE external_id = 'file-phones'") == []
    assert scalar(corpus, "SELECT count(*) FROM documents") == 2


async def test_a_tombstone_inside_its_window_is_kept_as_the_history_it_is(
    drive: FakeDrive, corpus: Any
) -> None:
    """4.4: the row is the answer to what agents read from before it went away."""
    populate(drive)
    config = config_for(corpus)
    await run_once(config)
    recent = datetime.now(UTC) - timedelta(days=179)
    execute(
        corpus,
        "UPDATE documents SET tombstoned_at = :when, tombstone_reason = 'deleted' "
        "WHERE external_id = :id",
        when=recent,
        id="file-phones",
    )
    drive.set_changes("t1", changed=(), new_token="t2")

    await run_once(config)

    assert query(corpus, "SELECT id FROM documents WHERE external_id = 'file-phones'")
