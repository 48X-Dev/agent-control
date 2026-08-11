"""The one channel strangers can write into, and the four things that must hold.

A GitHub answering over a stubbed httpx transport, in the JSON shapes the real
API returns, so the URLs and query parameters this code builds are what these
exercise rather than a mock of them. The membership endpoint is stubbed at its
three real answers - 204, 404 and the 302 GitHub gives a caller that is not
itself an organisation member - because the difference between those is the
whole of ``author_kind`` and a fake that answered only 204 and 404 would hide
the case that matters.

Four of these are the ones the plan would be wrong about if they ever failed:
a public repo is refused by name even with the flag on, a repo without the flag
asks GitHub nothing, a non-member lands external, and an author whose
membership cannot be determined lands external rather than workspace.

The channel reads through ``GitHubClient``, so the transport assertions here are
about the ladder it inherited: a 429 is waited out and a 5xx retried where this
module used to raise ``repo_metadata_unreadable`` on the first one.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync import github_client as github_client_module
from agent_control_knowledge_sync import github_transport as github_transport_module
from agent_control_knowledge_sync.allowlist import RepoConfig, RepoRef
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials
from agent_control_knowledge_sync.drive_transport import BACKOFF_SECONDS
from agent_control_knowledge_sync.github_client import GitHubClient, GitHubRateLimitedError
from agent_control_knowledge_sync.github_client import GitHubScopeError as ScopeError
from agent_control_knowledge_sync.github_issue_ingest import ISSUES_REF_SUFFIX, SOURCE_TRUST
from agent_control_knowledge_sync.github_issues import (
    AuthorKind,
    GitHubIssueReader,
    IssueChannelRefusedError,
    IssueRefusal,
    OrgMembership,
    sync_issue_channels,
    sync_repo_issues,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio

OWNER = "acme"
NAME = "handbook"
REPO = RepoRef(owner=OWNER, name=NAME)
OPTED_IN = RepoConfig(repo=REPO, github_issues_enabled=True)
DARK = RepoConfig(repo=REPO)
TOKEN = "ghp-not-a-real-token"

CREDS = DriveCredentials(
    client_id="123456789012-abcdefg.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)

MEMBER = "dana"
STRANGER = "passer-by"
INVISIBLE = "quiet-one"

BODY = """Laptops are ordered on the first day and arrive already enrolled, so nobody
waits on IT for a machine they can use. Badges take about a week and the front
desk issues a temporary one in the meantime, which opens every door the
permanent badge opens except the server room itself.
"""

_REPO_RE = re.compile(r"^/repos/([^/]+/[^/]+)$")
_ISSUES_RE = re.compile(r"^/repos/[^/]+/[^/]+/issues$")
_COMMITS_RE = re.compile(r"^/repos/[^/]+/[^/]+/commits$")
_REVIEWS_RE = re.compile(r"^/repos/[^/]+/[^/]+/pulls/(\d+)/reviews$")
_MEMBERS_RE = re.compile(r"^/orgs/([^/]+)/members/(.+)$")


@dataclass
class FakeGitHub:
    """An in-memory repo plus the request log the refusal assertions read."""

    private: bool = True
    default_branch: str = "main"
    repo_status: int = 200
    issues: list[dict[str, Any]] = field(default_factory=list)
    reviews: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    commits: list[dict[str, Any]] = field(default_factory=list)
    members: set[str] = field(default_factory=set)
    membership_status: dict[str, int] = field(default_factory=dict)
    public_repos: set[str] = field(default_factory=set)
    rate_limit: dict[str, str] = field(default_factory=dict)
    transient: dict[str, list[httpx.Response]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def membership_calls(self) -> list[str]:
        return [path for path in self.paths() if _MEMBERS_RE.match(path)]

    def fail_next(self, fragment: str, *responses: httpx.Response) -> None:
        """Queue answers for any path holding ``fragment``, served before the real one."""
        self.transient.setdefault(fragment, []).extend(responses)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._answer(request)
        response.headers.update(self.rate_limit)
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = int(request.url.params.get("page", "1"))
        for fragment, queued in self.transient.items():
            if fragment in path and queued:
                return queued.pop(0)
        named = _REPO_RE.match(path)
        if named is not None:
            if self.repo_status != 200:
                return httpx.Response(self.repo_status, json={"message": "Not Found"})
            full_name = named.group(1)
            return httpx.Response(
                200,
                json={
                    "full_name": full_name,
                    "private": self.private and full_name not in self.public_repos,
                    "default_branch": self.default_branch,
                },
            )
        if _ISSUES_RE.match(path):
            return _page(self.issues, page)
        if _COMMITS_RE.match(path):
            return _page(self.commits, page)
        review = _REVIEWS_RE.match(path)
        if review is not None:
            return _page(self.reviews.get(int(review.group(1)), []), page)
        member = _MEMBERS_RE.match(path)
        if member is not None:
            return self._membership(member.group(2))
        return httpx.Response(404, json={"message": "Not Found"})

    def _membership(self, login: str) -> httpx.Response:
        """204 a member, 404 a stranger, and whatever else was planted for the blind case."""
        if login in self.members:
            return httpx.Response(204)
        override = self.membership_status.get(login)
        if override is not None:
            return httpx.Response(override, json={"message": "Moved"})
        return httpx.Response(404, json={"message": "Not Found"})


def _page(rows: list[dict[str, Any]], page: int) -> httpx.Response:
    """One page of 100, which is what makes the client stop asking for more."""
    start = (page - 1) * 100
    return httpx.Response(200, json=rows[start : start + 100])


def issue_row(
    number: int,
    *,
    title: str = "Laptop policy",
    body: str = BODY,
    login: str | None = MEMBER,
    association: str = "MEMBER",
    pull: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": number,
        "title": title,
        "body": body,
        "user": None if login is None else {"login": login},
        "author_association": association,
        "updated_at": "2026-08-01T09:30:00Z",
    }
    if pull:
        row["pull_request"] = {"url": f"https://api.github.com/repos/{OWNER}/{NAME}/pulls/{number}"}
    return row


def review_row(review_id: int, *, body: str, login: str = MEMBER) -> dict[str, Any]:
    return {
        "id": review_id,
        "body": body,
        "user": {"login": login},
        "author_association": "MEMBER",
        "state": "APPROVED",
        "submitted_at": "2026-08-02T10:00:00Z",
    }


def commit_row(sha: str, *, message: str, login: str | None = MEMBER) -> dict[str, Any]:
    return {
        "sha": sha,
        "author": None if login is None else {"login": login},
        "commit": {
            "message": message,
            "committer": {"date": "2026-08-03T11:00:00Z"},
        },
    }


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff without the wait, and a record of what would have been waited."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(github_transport_module, "_sleep", fake_sleep)
    return waits


def _config() -> SyncConfig:
    return SyncConfig(
        credentials=CREDS,
        root_folder_id="0ABsharedDriveRoot",
        database_url="postgresql+psycopg://knowledge_sync@localhost/agent_knowledge",
    )


def shared_client(http: httpx.AsyncClient, *repos: RepoConfig) -> GitHubClient:
    """The client both channels read through, carrying the allowlist and the budget."""
    return GitHubClient(TOKEN, http, _config(), repos=repos or (OPTED_IN,))


def readers(http: httpx.AsyncClient) -> tuple[GitHubIssueReader, OrgMembership]:
    """A reader and a membership cache on one client, so one counter sees both."""
    client = shared_client(http)
    return GitHubIssueReader(client), OrgMembership(client, org=OWNER)


async def read_issues(github: FakeGitHub) -> list[Any]:
    """The reader alone, which is where ``author_kind`` is decided."""
    async with github.client() as http:
        reader, org = readers(http)
        found, _ = await reader.issue_documents(REPO, org, since=None, limit=50)
        return found


class ExplodingSessions:
    """A session factory that fails the test if the channel touches the corpus."""

    def __call__(self) -> Any:
        raise AssertionError("the dark channel opened a database session")


# --- the four that matter ------------------------------------------------------


async def test_public_repo_issue_text_is_refused_by_name_despite_the_flag() -> None:
    github = FakeGitHub(private=False, issues=[issue_row(214)])
    async with github.client() as http:
        with pytest.raises(IssueChannelRefusedError) as refused:
            await sync_repo_issues(
                OPTED_IN, sessions=ExplodingSessions(), client=shared_client(http)
            )
    assert refused.value.code == IssueRefusal.PUBLIC_REPO
    assert refused.value.code == "public_repo_issue_text_refused"
    # The refusal has to land before any of the text does.
    assert github.paths() == [f"/repos/{OWNER}/{NAME}"]


async def test_a_repo_without_the_flag_asks_github_nothing() -> None:
    github = FakeGitHub(issues=[issue_row(214)], commits=[commit_row("abc1234", message="Fix")])
    async with github.client() as http:
        outcome = await sync_repo_issues(
            DARK, sessions=ExplodingSessions(), client=shared_client(http)
        )
    assert outcome.refusal_code == IssueRefusal.DISABLED
    assert outcome.documents_indexed == 0
    assert outcome.source_id is None
    assert github.requests == []


async def test_a_non_member_author_is_external() -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(9, login=STRANGER)])
    found = await read_issues(github)
    assert [document.author_kind for document in found] == [AuthorKind.EXTERNAL]


async def test_membership_that_cannot_be_determined_is_external_not_workspace() -> None:
    """GitHub 302s a caller that is not itself an org member, which is not a 'no'."""
    github = FakeGitHub(
        membership_status={INVISIBLE: 302},
        issues=[issue_row(9, login=INVISIBLE)],
    )
    async with github.client() as http:
        reader, org = readers(http)
        found, _ = await reader.issue_documents(REPO, org, since=None, limit=50)
    assert found[0].author_kind == AuthorKind.EXTERNAL
    assert found[0].author_kind != AuthorKind.WORKSPACE
    assert org.undetermined == 1


# --- the positive control, and what must not promote ---------------------------


async def test_a_confirmed_member_is_workspace() -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(9, login=MEMBER)])
    found = await read_issues(github)
    assert found[0].author_kind == AuthorKind.WORKSPACE
    # 'unknown' is the Drive answer, where the API carries no owner at all.
    # This channel always has an opinion, and it is one of these two.
    assert {kind.value for kind in AuthorKind} == {"workspace", "external"}


async def test_a_payload_claiming_MEMBER_does_not_beat_the_membership_endpoint() -> None:  # noqa: N802
    """``author_association`` is attacker-adjacent metadata; it may demote, never promote."""
    github = FakeGitHub(issues=[issue_row(9, login=STRANGER, association="MEMBER")])
    found = await read_issues(github)
    assert found[0].author_kind == AuthorKind.EXTERNAL


async def test_a_settled_non_member_association_spends_no_call() -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(9, login=MEMBER, association="NONE")])
    found = await read_issues(github)
    assert found[0].author_kind == AuthorKind.EXTERNAL
    assert github.membership_calls() == []


async def test_one_membership_call_per_login_however_many_issues() -> None:
    github = FakeGitHub(
        members={MEMBER},
        issues=[issue_row(number, login=MEMBER) for number in (1, 2, 3)],
    )
    await read_issues(github)
    assert github.membership_calls() == [f"/orgs/{OWNER}/members/{MEMBER}"]


async def test_an_author_that_is_no_account_at_all_is_external() -> None:
    github = FakeGitHub(issues=[issue_row(9, login=None)])
    found = await read_issues(github)
    assert found[0].author_kind == AuthorKind.EXTERNAL
    assert github.membership_calls() == []


# --- what gets indexed, per section 6 ------------------------------------------


async def test_issues_and_pull_requests_carry_distinct_external_ids() -> None:
    github = FakeGitHub(
        members={MEMBER},
        issues=[issue_row(214), issue_row(215, pull=True, title="Add the loaner section")],
    )
    async with github.client() as http:
        reader, org = readers(http)
        found, pulls = await reader.issue_documents(REPO, org, since=None, limit=50)
    assert [document.external_id for document in found] == ["issue:214", "pr:215"]
    assert [document.path for document in found] == [
        f"{OWNER}/{NAME}#214",
        f"{OWNER}/{NAME}#215",
    ]
    assert pulls == [215]


async def test_the_issue_title_becomes_the_heading_a_snippet_is_cited_by() -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214, title="Laptop policy")])
    found = await read_issues(github)
    assert found[0].title == "Laptop policy"
    assert found[0].text.startswith("# Laptop policy\n\n")


async def test_a_review_summary_is_its_own_document_and_a_silent_review_is_not() -> None:
    github = FakeGitHub(
        members={MEMBER},
        issues=[issue_row(215, pull=True)],
        reviews={215: [review_row(9001, body="Looks right. " + BODY), review_row(9002, body="")]},
    )
    async with github.client() as http:
        reader, org = readers(http)
        found = await reader.review_documents(REPO, org, numbers=[215], limit=50)
    assert [document.external_id for document in found] == ["review:9001"]
    assert found[0].path == f"{OWNER}/{NAME}#215 (review)"


async def test_a_commit_document_is_the_subject_line_only() -> None:
    github = FakeGitHub(
        members={MEMBER},
        commits=[commit_row("abc1234def", message="Refuse public repo issue text\n\nBody here.")],
    )
    async with github.client() as http:
        reader, org = readers(http)
        found = await reader.commit_documents(REPO, org, branch="main", since=None, limit=500)
    assert found[0].external_id == "commit:abc1234def"
    assert found[0].text == "Refuse public repo issue text"
    assert found[0].path == f"{OWNER}/{NAME}@abc1234"


async def test_a_commit_whose_email_matched_no_account_is_external() -> None:
    github = FakeGitHub(
        members={MEMBER}, commits=[commit_row("abc1234", message="Tidy", login=None)]
    )
    async with github.client() as http:
        reader, org = readers(http)
        found = await reader.commit_documents(REPO, org, branch="main", since=None, limit=500)
    assert found[0].author_kind == AuthorKind.EXTERNAL


async def test_a_hostile_title_is_normalized_before_it_reaches_the_header() -> None:
    """4.2 normalizes at index time; the title here is typed by whoever opened the issue."""
    github = FakeGitHub(
        members={MEMBER},
        issues=[issue_row(9, title="Laptops\n‮sdrawkcab")],
    )
    found = await read_issues(github)
    assert "\n" not in found[0].title
    assert "‮" not in found[0].title
    assert "" not in found[0].title
    assert found[0].title.startswith("Laptops")


async def test_repo_metadata_that_will_not_answer_refuses_rather_than_reading() -> None:
    github = FakeGitHub(repo_status=403, issues=[issue_row(214)])
    async with github.client() as http:
        with pytest.raises(IssueChannelRefusedError) as refused:
            await sync_repo_issues(
                OPTED_IN, sessions=ExplodingSessions(), client=shared_client(http)
            )
    assert refused.value.code == IssueRefusal.REPO_UNREADABLE
    assert github.paths() == [f"/repos/{OWNER}/{NAME}"]


# --- the shared transport, which is what this channel used to go without --------


async def test_a_429_with_retry_after_is_waited_out_rather_than_refused(
    slept: list[float],
) -> None:
    """The reason the channel moved onto the client: it waits where it used to give up."""
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    github.fail_next("/issues", httpx.Response(429, headers={"Retry-After": "3"}, json=[]))
    found = await read_issues(github)
    assert [document.external_id for document in found] == ["issue:214"]
    assert slept == [3.0]


async def test_a_5xx_is_retried_on_the_shared_ladder(slept: list[float]) -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    github.fail_next("/issues", httpx.Response(503, json={"message": "Server Error"}))
    found = await read_issues(github)
    assert [document.external_id for document in found] == ["issue:214"]
    assert slept == [BACKOFF_SECONDS]


async def test_an_exhausted_ladder_is_unreachable_not_a_repo_this_channel_may_not_read() -> None:
    """A transport failure and a refusal must not reach a run as the same value."""
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    github.fail_next("/issues", *[httpx.Response(503, json={}) for _ in range(5)])
    async with github.client() as http:
        reader, org = readers(http)
        with pytest.raises(github_client_module.GitHubUnreachableError):
            await reader.issue_documents(REPO, org, since=None, limit=50)


async def test_a_spent_budget_is_the_same_counter_the_files_channel_reads() -> None:
    """One credential, 5,000/hour, so the reader must not burn what the walk is pacing."""
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    github.rate_limit = {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": str(int(time.time()) + 3600),
    }
    async with github.client() as http:
        client = shared_client(http)
        reader = GitHubIssueReader(client)
        await reader.default_branch_of_private_repo(REPO)
        assert client.rate_limit_remaining == 0
        with pytest.raises(GitHubRateLimitedError) as caught:
            await reader.issue_documents(
                REPO, OrgMembership(client, org=OWNER), since=None, limit=50
            )
    assert caught.value.code == "rate_limited"


async def test_the_branch_and_the_private_flag_come_from_one_call() -> None:
    """``repo_metadata`` carries both, so nothing fetches ``/repos/{full_name}`` twice."""
    github = FakeGitHub(members={MEMBER}, default_branch="trunk")
    async with github.client() as http:
        reader = GitHubIssueReader(shared_client(http))
        assert await reader.default_branch_of_private_repo(REPO) == "trunk"
    assert github.paths() == [f"/repos/{OWNER}/{NAME}"]


async def test_a_repo_outside_the_allowlist_never_reaches_the_wire() -> None:
    """Section 6's assertion, now the client's rather than a second copy in here."""
    outsider = RepoConfig(repo=RepoRef(owner=OWNER, name="not-listed"), github_issues_enabled=True)
    github = FakeGitHub(issues=[issue_row(214)])
    async with github.client() as http:
        with pytest.raises(ScopeError) as caught:
            await sync_repo_issues(
                outsider, sessions=ExplodingSessions(), client=shared_client(http)
            )
    assert caught.value.code == "repo_not_allowlisted"
    assert github.requests == []


# --- against a real corpus -----------------------------------------------------


def _sessions(corpus: Any) -> Any:
    return async_sessionmaker(create_async_engine(corpus.sync_url), expire_on_commit=False)


def rows(corpus: Any, sql: str) -> list[Any]:
    from tests.conftest import query

    return query(corpus, sql)


async def test_the_channel_writes_documents_chunks_and_its_own_source_row(corpus: Any) -> None:
    github = FakeGitHub(
        members={MEMBER},
        issues=[issue_row(214), issue_row(215, login=STRANGER, pull=True)],
        reviews={215: [review_row(9001, body="Approving. " + BODY)]},
        commits=[commit_row("abc1234", message="Ship the loaner section")],
    )
    async with github.client() as http:
        outcome = await sync_repo_issues(
            OPTED_IN, sessions=_sessions(corpus), client=shared_client(http)
        )

    assert outcome.refusal_code is None
    assert outcome.documents_indexed == 4
    # Only the PR was opened by a stranger, and this count is what 8.5's post
    # control reads as `external_author_count`.
    assert outcome.external_authors == 1
    assert outcome.chunks_written >= 4

    source = rows(corpus, "SELECT kind, ref, trust, last_verified_at FROM sources")[0]
    assert source["kind"] == "github_repo"
    assert source["ref"] == f"{OWNER}/{NAME}{ISSUES_REF_SUFFIX}"
    assert source["trust"] == SOURCE_TRUST
    assert source["last_verified_at"] is not None

    stored = rows(corpus, "SELECT external_id, author_kind FROM documents ORDER BY external_id")
    assert [(row["external_id"], row["author_kind"]) for row in stored] == [
        ("commit:abc1234", "workspace"),
        ("issue:214", "workspace"),
        ("pr:215", "external"),
        ("review:9001", "workspace"),
    ]
    assert rows(corpus, "SELECT count(*) AS n FROM chunks")[0]["n"] >= 4


async def test_a_second_pass_over_unchanged_issues_writes_nothing(corpus: Any) -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    for _ in range(2):
        async with github.client() as http:
            outcome = await sync_repo_issues(
                OPTED_IN, sessions=_sessions(corpus), client=shared_client(http)
            )
    assert outcome.documents_indexed == 0
    assert outcome.documents_unchanged == 1
    assert rows(corpus, "SELECT count(*) AS n FROM documents")[0]["n"] == 1


async def test_a_membership_that_changed_rewrites_author_kind(corpus: Any) -> None:
    """The text is identical, so only ``author_kind`` in the comparison catches this."""
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214, login=MEMBER)])
    async with github.client() as http:
        await sync_repo_issues(OPTED_IN, sessions=_sessions(corpus), client=shared_client(http))
    assert rows(corpus, "SELECT author_kind FROM documents")[0]["author_kind"] == "workspace"

    github.members.clear()
    async with github.client() as http:
        outcome = await sync_repo_issues(
            OPTED_IN, sessions=_sessions(corpus), client=shared_client(http)
        )
    assert outcome.documents_indexed == 1
    assert rows(corpus, "SELECT author_kind FROM documents")[0]["author_kind"] == "external"


async def test_a_dark_repo_leaves_no_source_row_behind(corpus: Any) -> None:
    """An enabled source that never verifies would drag the corpus staleness line down."""
    github = FakeGitHub(issues=[issue_row(214)])
    async with github.client() as http:
        outcome = await sync_repo_issues(
            DARK, sessions=_sessions(corpus), client=shared_client(http)
        )
    assert outcome.refusal_code == IssueRefusal.DISABLED
    assert rows(corpus, "SELECT count(*) AS n FROM sources")[0]["n"] == 0
    assert rows(corpus, "SELECT count(*) AS n FROM documents")[0]["n"] == 0


async def test_the_cursor_advances_so_the_next_pass_asks_only_for_changes(corpus: Any) -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    async with github.client() as http:
        await sync_repo_issues(OPTED_IN, sessions=_sessions(corpus), client=shared_client(http))
    stored = rows(corpus, "SELECT cursor ->> 'issues_since' AS since FROM sources")[0]
    assert stored["since"]

    github.requests.clear()
    async with github.client() as http:
        await sync_repo_issues(OPTED_IN, sessions=_sessions(corpus), client=shared_client(http))
    issues_call = next(r for r in github.requests if r.url.path.endswith("/issues"))
    assert issues_call.url.params.get("since") == stored["since"]


async def test_a_public_repo_writes_nothing_even_after_a_run_that_worked(corpus: Any) -> None:
    """The private check runs every pass, not once at opt-in time."""
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    async with github.client() as http:
        await sync_repo_issues(OPTED_IN, sessions=_sessions(corpus), client=shared_client(http))
    before = rows(corpus, "SELECT count(*) AS n FROM documents")[0]["n"]

    github.private = False
    github.issues.append(issue_row(215, title="Now visible to the world"))
    async with github.client() as http:
        with pytest.raises(IssueChannelRefusedError) as refused:
            await sync_repo_issues(OPTED_IN, sessions=_sessions(corpus), client=shared_client(http))
    assert refused.value.code == IssueRefusal.PUBLIC_REPO
    assert rows(corpus, "SELECT count(*) AS n FROM documents")[0]["n"] == before


async def test_a_sweep_records_the_refusal_and_still_indexes_the_rest(corpus: Any) -> None:
    """The allowlist is the sweep's argument, and one bad repo does not stop the others."""
    site = RepoRef(owner=OWNER, name="marketing-site")
    public = RepoConfig(repo=site, github_issues_enabled=True)
    github = FakeGitHub(
        members={MEMBER},
        issues=[issue_row(214)],
        public_repos={f"{OWNER}/marketing-site"},
    )
    async with github.client() as http:
        outcomes = await sync_issue_channels(
            [public, OPTED_IN],
            sessions=_sessions(corpus),
            client=shared_client(http, public, OPTED_IN),
        )
    assert [outcome.refusal_code for outcome in outcomes] == [IssueRefusal.PUBLIC_REPO, None]
    assert outcomes[1].documents_indexed == 1
    stored = rows(corpus, "SELECT ref FROM sources")
    assert [row["ref"] for row in stored] == [f"{OWNER}/{NAME}{ISSUES_REF_SUFFIX}"]


async def test_the_stored_chunk_is_searchable_text_not_an_empty_row(corpus: Any) -> None:
    github = FakeGitHub(members={MEMBER}, issues=[issue_row(214)])
    async with github.client() as http:
        await sync_repo_issues(OPTED_IN, sessions=_sessions(corpus), client=shared_client(http))
    chunk = rows(corpus, "SELECT heading_path, body FROM chunks ORDER BY id LIMIT 1")[0]
    assert chunk["heading_path"] == "Laptop policy"
    assert "Badges take about a week" in chunk["body"]
