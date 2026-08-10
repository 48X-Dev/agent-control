"""The GitHub read path, against stubbed transports, with no token and no network.

Three properties are asserted rather than assumed. A repo outside the allowlist
must not reach the wire at all, because the credential in use would have
answered. A truncated tree must refuse, because GitHub returns 200 with a
partial list and indexing it would be a silently incomplete mirror. And an
upstream that could not be reached must raise, never return something a caller
could read as "this does not exist".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync import github_client as github_client_module
from agent_control_knowledge_sync.allowlist import RepoConfig, RepoRef
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials
from agent_control_knowledge_sync.github_client import (
    COMPARE_FILE_CAP,
    GitHubClient,
    GitHubError,
    GitHubFile,
    GitHubRateLimitedError,
    GitHubRefusalError,
    GitHubRepoError,
    GitHubResyncError,
    GitHubScopeError,
    GitHubTreeTruncatedError,
    GitHubUnreachableError,
    is_indexable_path,
    path_refusal,
)

from tests.fakes.github import FakeGitHub, FakeRepo, blob_sha

REPO = RepoRef(owner="earlycore", name="agent-control")
OTHER = RepoRef(owner="earlycore", name="not-listed")

CREDS = DriveCredentials(
    client_id="123456789012-abcdefg.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff without the wait, and a record of what would have been waited."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(github_client_module, "_sleep", fake_sleep)
    return waits


def _config(**overrides: Any) -> SyncConfig:
    base: dict[str, Any] = {
        "credentials": CREDS,
        "root_folder_id": "0ABsharedDriveRoot",
        "database_url": "postgresql+psycopg://knowledge_sync@localhost/agent_knowledge",
    }
    base.update(overrides)
    return SyncConfig(**base)


def _client(
    hub: FakeGitHub,
    *,
    repos: tuple[RepoConfig, ...] = (RepoConfig(repo=REPO),),
    **overrides: Any,
) -> GitHubClient:
    http = httpx.AsyncClient(transport=hub.transport())
    return GitHubClient("ghp_not_a_real_token", http, _config(**overrides), repos=repos)


def _hub(*paths: str, **repo_kwargs: Any) -> tuple[FakeGitHub, FakeRepo]:
    hub = FakeGitHub()
    repo = hub.repo(REPO.full_name, **repo_kwargs)
    for path in paths:
        repo.add(path, f"# {path}\n")
    return hub, repo


async def _walked(client: GitHubClient, repo: RepoRef = REPO) -> list[GitHubFile]:
    return [file async for file in client.walk_files(repo)]


class TestTheAllowlistIsAssertedAtTheCallSite:
    """GitHub would answer for any of these, which is exactly why the client will not."""

    @pytest.mark.asyncio
    async def test_walking_an_unlisted_repo_never_reaches_the_wire(self) -> None:
        hub, _ = _hub("README.md")
        hub.repo(OTHER.full_name).add("README.md", "# other\n")
        client = _client(hub)
        with pytest.raises(GitHubScopeError) as caught:
            await _walked(client, OTHER)
        assert caught.value.code == "repo_not_allowlisted"
        assert hub.requests == []

    @pytest.mark.asyncio
    async def test_resolving_a_branch_asserts(self) -> None:
        hub, _ = _hub("README.md")
        client = _client(hub)
        with pytest.raises(GitHubScopeError):
            await client.default_branch(OTHER)
        with pytest.raises(GitHubScopeError):
            await client.head_sha(OTHER, "main")
        assert hub.requests == []

    @pytest.mark.asyncio
    async def test_diffing_an_unlisted_repo_asserts(self) -> None:
        hub, _ = _hub("README.md")
        with pytest.raises(GitHubScopeError):
            await _client(hub).changed_files(OTHER, "b" * 40)
        assert hub.requests == []

    @pytest.mark.asyncio
    async def test_fetching_a_blob_from_an_unlisted_repo_is_refused(self) -> None:
        hub, _ = _hub("README.md")
        file = GitHubFile(
            repo=OTHER, path="README.md", sha="a" * 40, size=4, external_id="x:README.md"
        )
        with pytest.raises(GitHubScopeError):
            await _client(hub).fetch_blob(file)
        assert hub.requests == []

    @pytest.mark.asyncio
    async def test_an_empty_allowlist_admits_nothing(self) -> None:
        hub, _ = _hub("README.md")
        with pytest.raises(GitHubScopeError):
            await _walked(_client(hub, repos=()))
        assert hub.requests == []


class TestTheIndexedSet:
    @pytest.mark.asyncio
    async def test_slice_one_is_readmes_docs_and_root_markdown(self) -> None:
        hub, _ = _hub(
            "README.md",
            "docs/plans/task-dispatcher.md",
            "docs/architecture.md",
            "CONTRIBUTING.md",
            "packages/api/README.rst",
            "src/main.py",
            "assets/logo.svg",
            "deep/nested/notes.md",
        )
        paths = [file.path for file in await _walked(_client(hub))]
        assert sorted(paths) == [
            "CONTRIBUTING.md",
            "README.md",
            "docs/architecture.md",
            "docs/plans/task-dispatcher.md",
            "packages/api/README.rst",
        ]

    @pytest.mark.asyncio
    async def test_include_paths_widen_and_nothing_else_does(self) -> None:
        hub, _ = _hub("runbooks/oncall.md", "reference/policies/travel.md", "src/main.py")
        repos = (RepoConfig(repo=REPO, include_paths=("runbooks",)),)
        paths = [file.path for file in await _walked(_client(hub, repos=repos))]
        assert paths == ["runbooks/oncall.md"]

    @pytest.mark.asyncio
    async def test_an_include_path_is_a_prefix_not_a_substring(self) -> None:
        hub, _ = _hub("runbooks-archive/old.md", "runbooks/oncall.md")
        repos = (RepoConfig(repo=REPO, include_paths=("runbooks",)),)
        paths = [file.path for file in await _walked(_client(hub, repos=repos))]
        assert paths == ["runbooks/oncall.md"]

    @pytest.mark.asyncio
    async def test_the_external_id_is_owner_repo_colon_path(self) -> None:
        hub, _ = _hub("docs/plans/task-dispatcher.md")
        walked = await _walked(_client(hub))
        assert walked[0].external_id == "earlycore/agent-control:docs/plans/task-dispatcher.md"

    @pytest.mark.asyncio
    async def test_the_blob_sha_and_size_travel_with_the_file(self) -> None:
        hub, repo = _hub()
        repo.add("README.md", "# hello\n")
        walked = await _walked(_client(hub))
        assert walked[0].sha == blob_sha(b"# hello\n")
        assert walked[0].size == len(b"# hello\n")


class TestAlwaysRefused:
    @pytest.mark.parametrize(
        "path",
        [
            "node_modules/pkg/README.md",
            "vendor/lib/README.md",
            "third_party/x/README.md",
            "dist/README.md",
            "build/README.md",
            "docs/bundle.min.js",
            "docs/package-lock.json",
            "docs/uv.lock",
            "docs/yarn.lock",
        ],
    )
    @pytest.mark.asyncio
    async def test_vendored_generated_and_lockfiles_are_refused_and_counted(
        self, path: str
    ) -> None:
        hub, _ = _hub(path, "README.md")
        client = _client(hub)
        paths = [file.path for file in await _walked(client)]
        assert paths == ["README.md"]
        assert client.refusals["denied_path"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["docs/.env", "docs/id_rsa", "docs/server.pem"])
    async def test_a_filename_that_is_itself_a_credential_is_refused(self, path: str) -> None:
        hub, _ = _hub(path)
        client = _client(hub)
        assert await _walked(client) == []
        assert client.refusals["secret_file"] == 1

    def test_one_rule_admits_a_vendored_readme_and_the_other_takes_it_back(self) -> None:
        assert is_indexable_path("node_modules/x/README.md") is True
        assert path_refusal("node_modules/x/README.md") == "denied_path"
        assert path_refusal("docs/plans/task-dispatcher.md") is None


class TestATruncatedTreeRefuses:
    """K4's assertion: GitHub answers 200 with a partial tree rather than erroring."""

    @pytest.mark.asyncio
    async def test_the_repo_is_refused_rather_than_partially_indexed(self) -> None:
        hub, repo = _hub("README.md", "docs/a.md")
        repo.truncated = True
        with pytest.raises(GitHubTreeTruncatedError) as caught:
            await _walked(_client(hub))
        assert caught.value.code == "tree_truncated"

    @pytest.mark.asyncio
    async def test_a_complete_tree_is_walked(self) -> None:
        hub, _ = _hub("README.md")
        assert len(await _walked(_client(hub))) == 1


class TestResolvingTheBranch:
    @pytest.mark.asyncio
    async def test_the_default_branch_is_read_not_assumed(self) -> None:
        hub, repo = _hub("README.md", default_branch="trunk")
        assert await _client(hub).default_branch(REPO) == "trunk"

    @pytest.mark.asyncio
    async def test_the_head_sha_and_its_commit_date_come_from_one_call(self) -> None:
        hub, repo = _hub("README.md", head="c" * 40)
        client = _client(hub)
        assert await client.head_sha(REPO, "main") == "c" * 40
        stamped = client.last_commit_at(REPO)
        assert stamped is not None
        assert stamped.year == 2026
        assert stamped.tzinfo is not None

    @pytest.mark.asyncio
    async def test_a_repo_the_token_cannot_see_is_a_named_repo_error(self) -> None:
        hub = FakeGitHub()
        with pytest.raises(GitHubRepoError) as caught:
            await _client(hub).default_branch(REPO)
        assert caught.value.code == "repo_unreachable"

    @pytest.mark.asyncio
    async def test_the_request_carries_the_token_and_the_api_version(self) -> None:
        hub, _ = _hub("README.md")
        await _client(hub).default_branch(REPO)
        headers = hub.requests[0].headers
        assert headers["authorization"] == "Bearer ghp_not_a_real_token"
        assert headers["x-github-api-version"] == "2022-11-28"
        assert hub.requests[0].url.path == "/repos/earlycore/agent-control"


class TestFetchingBlobs:
    @pytest.mark.asyncio
    async def test_base64_content_is_decoded(self) -> None:
        hub, repo = _hub()
        repo.add("README.md", "# hello world\n")
        client = _client(hub)
        walked = await _walked(client)
        assert await client.fetch_blob(walked[0]) == b"# hello world\n"

    @pytest.mark.asyncio
    async def test_an_oversize_blob_is_refused_before_it_is_downloaded(self) -> None:
        hub, repo = _hub()
        repo.add("README.md", "x" * 400)
        client = _client(hub, max_file_bytes=100)
        walked = await _walked(client)
        before = len(hub.requests)
        with pytest.raises(GitHubRefusalError) as caught:
            await client.fetch_blob(walked[0])
        assert caught.value.code == "oversize"
        assert len(hub.requests) == before

    @pytest.mark.asyncio
    async def test_an_oversize_blob_whose_size_was_unknown_is_refused_after_decoding(self) -> None:
        hub, repo = _hub()
        repo.add("README.md", "x" * 400)
        client = _client(hub, max_file_bytes=100)
        walked = await _walked(client)
        unsized = GitHubFile(
            repo=walked[0].repo,
            path=walked[0].path,
            sha=walked[0].sha,
            size=0,
            external_id=walked[0].external_id,
        )
        with pytest.raises(GitHubRefusalError) as caught:
            await client.fetch_blob(unsized)
        assert caught.value.code == "oversize"

    @pytest.mark.asyncio
    async def test_a_blob_the_token_cannot_read_is_a_named_refusal(self) -> None:
        hub, repo = _hub("README.md")
        client = _client(hub)
        missing = GitHubFile(
            repo=REPO, path="README.md", sha="f" * 40, size=4, external_id="x:README.md"
        )
        with pytest.raises(GitHubRefusalError) as caught:
            await client.fetch_blob(missing)
        assert caught.value.code == "blob_unreadable"


class TestTheIncrementalPath:
    @pytest.mark.asyncio
    async def test_an_unmoved_head_costs_no_compare_call(self) -> None:
        hub, repo = _hub("README.md")
        client = _client(hub)
        changed, removed, head = await client.changed_files(REPO, repo.head)
        assert (changed, removed, head) == ([], [], repo.head)
        assert not any("compare" in path for path in hub.paths())

    @pytest.mark.asyncio
    async def test_modified_and_removed_are_split(self) -> None:
        hub, repo = _hub("README.md", "docs/a.md")
        repo.set_compare("b" * 40, modified=("docs/a.md",), removed=("docs/gone.md",))
        changed, removed, head = await _client(hub).changed_files(REPO, "b" * 40)
        assert [file.path for file in changed] == ["docs/a.md"]
        assert removed == ["docs/gone.md"]
        assert head == repo.head

    @pytest.mark.asyncio
    async def test_a_rename_removes_the_old_path_and_reads_the_new_one(self) -> None:
        hub, repo = _hub("docs/new.md")
        repo.set_compare("b" * 40, renamed=(("docs/old.md", "docs/new.md"),))
        changed, removed, _ = await _client(hub).changed_files(REPO, "b" * 40)
        assert [file.path for file in changed] == ["docs/new.md"]
        assert removed == ["docs/old.md"]

    @pytest.mark.asyncio
    async def test_a_change_outside_the_indexed_set_is_neither(self) -> None:
        hub, repo = _hub("src/main.py")
        repo.set_compare("b" * 40, modified=("src/main.py",), removed=("src/old.py",))
        changed, removed, _ = await _client(hub).changed_files(REPO, "b" * 40)
        assert (changed, removed) == ([], [])

    @pytest.mark.asyncio
    async def test_a_removal_is_offered_even_when_a_newer_rule_would_refuse_the_file(self) -> None:
        """Removals are filtered by scope alone: taking something out is the safe direction."""
        hub, repo = _hub("README.md")
        repo.set_compare("b" * 40, removed=("docs/package-lock.json",))
        _, removed, _ = await _client(hub).changed_files(REPO, "b" * 40)
        assert removed == ["docs/package-lock.json"]


class TestTheForcePushFallback:
    """A rewritten history cannot be diffed, and a partial diff would be silent."""

    @pytest.mark.asyncio
    async def test_a_missing_base_asks_for_a_relist(self) -> None:
        hub, _ = _hub("README.md")
        with pytest.raises(GitHubResyncError) as caught:
            await _client(hub).changed_files(REPO, "b" * 40)
        assert caught.value.code == "force_push_relist"

    @pytest.mark.asyncio
    async def test_a_diverged_compare_asks_for_a_relist(self) -> None:
        hub, repo = _hub("README.md")
        repo.set_compare("b" * 40, status="diverged")
        with pytest.raises(GitHubResyncError) as caught:
            await _client(hub).changed_files(REPO, "b" * 40)
        assert caught.value.code == "force_push_relist"

    @pytest.mark.asyncio
    async def test_a_compare_at_the_file_ceiling_asks_for_a_relist(self) -> None:
        hub, repo = _hub(*[f"docs/f{index}.md" for index in range(COMPARE_FILE_CAP)])
        repo.set_compare("b" * 40, modified=tuple(repo.files))
        with pytest.raises(GitHubResyncError) as caught:
            await _client(hub).changed_files(REPO, "b" * 40)
        assert caught.value.code == "compare_truncated"


class TestTransportFailuresAreNeverAnAnswer:
    """A 5xx and a 404 must not arrive at the caller as the same thing."""

    @pytest.mark.asyncio
    async def test_a_transient_server_error_is_retried_then_succeeds(
        self, slept: list[float]
    ) -> None:
        hub, _ = _hub("README.md")
        hub.fail_next("/repos/earlycore/agent-control", 503)
        assert await _client(hub).default_branch(REPO) == "main"
        assert slept == [0.5]

    @pytest.mark.asyncio
    async def test_an_exhausted_retry_ladder_raises_rather_than_returning_the_5xx(self) -> None:
        hub, _ = _hub("README.md")
        hub.fail_next("/repos/earlycore/agent-control", 503, 503, 503, 503, 503)
        with pytest.raises(GitHubUnreachableError) as caught:
            await _client(hub).default_branch(REPO)
        assert caught.value.code == "github_unreachable"
        assert "not an answer about whether anything exists" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_dead_connection_raises_unreachable_not_not_found(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(explode))
        client = GitHubClient("ghp_x", http, _config(), repos=(RepoConfig(repo=REPO),))
        with pytest.raises(GitHubUnreachableError):
            await client.default_branch(REPO)

    @pytest.mark.asyncio
    async def test_a_genuine_404_is_a_repo_error_with_its_own_code(self) -> None:
        hub = FakeGitHub()
        with pytest.raises(GitHubRepoError) as caught:
            await _client(hub).default_branch(REPO)
        assert caught.value.code == "repo_unreachable"
        assert not isinstance(caught.value, GitHubUnreachableError)


class TestTheRateLimit:
    """5,000/hour, measured. Waiting out a reset beats spending the last of it."""

    @pytest.mark.asyncio
    async def test_a_spent_budget_with_a_near_reset_waits(
        self, monkeypatch: pytest.MonkeyPatch, slept: list[float]
    ) -> None:
        hub, _ = _hub("README.md")
        client = _client(hub)
        monkeypatch.setattr(github_client_module, "time", SimpleNamespace(time=lambda: 1_000.0))
        hub.rate_limit = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"}
        await client.default_branch(REPO)
        hub.rate_limit = {"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "1010"}
        await client.default_branch(REPO)
        assert slept == [10.0]

    @pytest.mark.asyncio
    async def test_a_spent_budget_with_a_far_reset_refuses_rather_than_holding_the_lease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub, _ = _hub("README.md")
        client = _client(hub)
        monkeypatch.setattr(github_client_module, "time", SimpleNamespace(time=lambda: 1_000.0))
        hub.rate_limit = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"}
        await client.default_branch(REPO)
        with pytest.raises(GitHubRateLimitedError) as caught:
            await client.default_branch(REPO)
        assert caught.value.code == "rate_limited"

    @pytest.mark.asyncio
    async def test_the_remaining_count_is_read_from_every_response(self) -> None:
        hub, _ = _hub("README.md")
        hub.rate_limit = {"X-RateLimit-Remaining": "4321", "X-RateLimit-Reset": "1010"}
        client = _client(hub)
        await client.default_branch(REPO)
        assert client.rate_limit_remaining == 4321

    @pytest.mark.asyncio
    async def test_a_403_carrying_a_spent_budget_is_retried_not_read_as_forbidden(
        self, slept: list[float]
    ) -> None:
        hub, _ = _hub("README.md")
        hub.rate_limit = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}
        hub.fail_next("/repos/earlycore/agent-control", 403)
        assert await _client(hub).default_branch(REPO) == "main"
        assert slept == [0.5]


class TestMalformedAnswers:
    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_refused(self) -> None:
        def prose(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>maintenance</html>")

        http = httpx.AsyncClient(transport=httpx.MockTransport(prose))
        client = GitHubClient("ghp_x", http, _config(), repos=(RepoConfig(repo=REPO),))
        with pytest.raises(GitHubError):
            await client.default_branch(REPO)

    @pytest.mark.asyncio
    async def test_a_repo_with_no_default_branch_is_refused(self) -> None:
        def blank(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"full_name": "earlycore/agent-control"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(blank))
        client = GitHubClient("ghp_x", http, _config(), repos=(RepoConfig(repo=REPO),))
        with pytest.raises(GitHubRepoError) as caught:
            await client.default_branch(REPO)
        assert caught.value.code == "repo_unreachable"
