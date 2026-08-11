"""Phase 6: issue and PR text, private repos only, dark until a repo opts in.

A non-private repo is refused by name, and any author not confirmed a member is external.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from agent_control_models.knowledge import normalize_index_name, normalize_index_path

from .allowlist import RepoConfig, RepoRef
from .github_client import (
    GitHubClient,
    GitHubError,
    GitHubRateLimitedError,
    GitHubScopeError,
    GitHubUnreachableError,
)
from .github_issue_ingest import (
    IssueDocument,
    StoreOutcome,
    advance_cursor,
    ensure_source,
    store_document,
)
from .lease import SessionFactory

__all__ = [
    "COMMIT_SUBJECT_LIMIT",
    "AuthorKind",
    "GitHubIssueReader",
    "IssueChannelRefusedError",
    "IssueRefusal",
    "IssueSyncOutcome",
    "OrgMembership",
    "sync_issue_channels",
    "sync_repo_issues",
]

LOGGER = logging.getLogger(__name__)

COMMIT_SUBJECT_LIMIT = 500
MAX_DOCUMENTS_DEFAULT = 2_000

_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

# ``author_association`` values that settle "not an organisation member" without
# spending a call. The set only ever skips work in the external direction:
# COLLABORATOR is absent because an outside collaborator and a member who also
# holds repo access can both wear it.
_SETTLED_EXTERNAL = frozenset(
    {"CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "NONE"}
)


class AuthorKind(StrEnum):
    """The two values this channel writes; ``unknown`` is never one of them."""

    WORKSPACE = "workspace"
    EXTERNAL = "external"


class IssueRefusal(StrEnum):
    """Named refusals, so a run records this channel's silences instead of skipping."""

    PUBLIC_REPO = "public_repo_issue_text_refused"
    DISABLED = "github_issues_disabled"
    REPO_UNREADABLE = "repo_metadata_unreadable"


class IssueChannelRefusedError(RuntimeError):
    """A repo this channel will not read, carrying the code a run records."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IssueSyncOutcome:
    """What one pass over one repo's issue channel did, and what it refused."""

    source_id: int | None = None
    documents_indexed: int = 0
    documents_unchanged: int = 0
    chunks_written: int = 0
    external_authors: int = 0
    membership_undetermined: int = 0
    secrets_skipped: int = 0
    refusal_code: str | None = None


def _seg(value: str) -> str:
    return quote(value, safe="")


def _repo_path(repo: RepoRef) -> str:
    return f"/repos/{_seg(repo.owner)}/{_seg(repo.name)}"


@contextmanager
def _readable(subject: str) -> Iterator[None]:
    """An answer this channel cannot use is its refusal; the rest keep their own codes."""
    try:
        yield
    except (GitHubScopeError, GitHubUnreachableError, GitHubRateLimitedError):
        raise
    except GitHubError as exc:
        raise IssueChannelRefusedError(
            f"GitHub would not answer for {subject}: {exc}",
            code=IssueRefusal.REPO_UNREADABLE,
        ) from exc


class OrgMembership:
    """Membership per login, cached per run; undetermined reads external (GitHub 302s if blind)."""

    def __init__(self, client: GitHubClient, *, org: str) -> None:
        self._client = client
        self._org = org
        self._cache: dict[str, AuthorKind] = {}
        self.undetermined = 0

    async def author_kind(self, login: str | None, association: str | None = None) -> AuthorKind:
        """Workspace only for a confirmed member; everything else is external."""
        if not login or not _LOGIN_RE.match(login):
            return AuthorKind.EXTERNAL
        if association is not None and association.upper() in _SETTLED_EXTERNAL:
            return AuthorKind.EXTERNAL
        cached = self._cache.get(login)
        if cached is None:
            cached = await self._probe(login)
            self._cache[login] = cached
        return cached

    async def _probe(self, login: str) -> AuthorKind:
        path = f"/orgs/{_seg(self._org)}/members/{_seg(login)}"
        try:
            response = await self._client.transport.request(path, {})
        except GitHubUnreachableError as exc:
            return self._blind(login, type(exc).__name__)
        if response.status_code == 204:
            return AuthorKind.WORKSPACE
        if response.status_code == 404:
            return AuthorKind.EXTERNAL
        return self._blind(login, f"HTTP {response.status_code}")

    def _blind(self, login: str, detail: str) -> AuthorKind:
        self.undetermined += 1
        LOGGER.warning(
            "Membership of %s in %s is undetermined (%s); indexing as external. A classic "
            "repo-scoped token carries no read:org and answers this way for every author.",
            login,
            self._org,
            detail,
        )
        return AuthorKind.EXTERNAL


class GitHubIssueReader:
    """One repo's issue channel: issues, PRs, review summaries and commit subjects."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def default_branch_of_private_repo(self, repo: RepoRef) -> str:
        """Refuse a public repo by name before a byte of its text is read."""
        with _readable(repo.full_name):
            metadata = await self._client.repo_metadata(repo)
        if not metadata.private:
            raise IssueChannelRefusedError(
                f"{repo.full_name} is public, so anyone holding a GitHub account can author its "
                f"issue text. Section 7 refuses that outright, whatever knowledge.yaml says.",
                code=IssueRefusal.PUBLIC_REPO,
            )
        return metadata.default_branch or "main"

    async def issue_documents(
        self, repo: RepoRef, org: OrgMembership, *, since: str | None, limit: int
    ) -> tuple[list[IssueDocument], list[int]]:
        """Issue and PR titles and bodies, plus the PR numbers reviews are read for."""
        params = {"state": "all", "sort": "updated", "direction": "desc"}
        if since:
            params["since"] = since
        found: list[IssueDocument] = []
        pulls: list[int] = []
        for row in await self._pages(repo, f"{_repo_path(repo)}/issues", params, limit):
            number = _int(row.get("number"))
            if number is None:
                continue
            kind = "pr" if "pull_request" in row else "issue"
            if kind == "pr":
                pulls.append(number)
            found.append(
                _document(
                    f"{kind}:{number}",
                    path=f"{repo.full_name}#{number}",
                    title=_text(row.get("title")) or f"#{number}",
                    body=_text(row.get("body")),
                    author=await _author_of(org, row),
                    at=row.get("updated_at"),
                )
            )
        return found, pulls

    async def review_documents(
        self, repo: RepoRef, org: OrgMembership, *, numbers: list[int], limit: int
    ) -> list[IssueDocument]:
        """Review summary comments; a review that said nothing is not a document."""
        found: list[IssueDocument] = []
        for number in numbers:
            if len(found) >= limit:
                break
            path = f"{_repo_path(repo)}/pulls/{number}/reviews"
            for row in await self._pages(repo, path, {}, limit - len(found)):
                body = _text(row.get("body"))
                review_id = _int(row.get("id"))
                if not body.strip() or review_id is None:
                    continue
                found.append(
                    _document(
                        f"review:{review_id}",
                        path=f"{repo.full_name}#{number} (review)",
                        title=f"Review on #{number}",
                        body=body,
                        author=await _author_of(org, row),
                        at=row.get("submitted_at"),
                    )
                )
        return found

    async def commit_documents(
        self, repo: RepoRef, org: OrgMembership, *, branch: str, since: str | None, limit: int
    ) -> list[IssueDocument]:
        """Default-branch commit message subjects, each its own document."""
        params = {"sha": branch}
        if since:
            params["since"] = since
        found: list[IssueDocument] = []
        rows = await self._pages(
            repo, f"{_repo_path(repo)}/commits", params, min(limit, COMMIT_SUBJECT_LIMIT)
        )
        for row in rows:
            sha = _text(row.get("sha"))
            commit = _mapping(row.get("commit"))
            subject = _text(commit.get("message")).split("\n", 1)[0]
            if not sha or not subject.strip():
                continue
            # No `author_association` on a commit, and `author` is null whenever
            # the commit email matched no account: undetermined, so external.
            found.append(
                _document(
                    f"commit:{sha}",
                    path=f"{repo.full_name}@{sha[:7]}",
                    title=subject,
                    body="",
                    author=await org.author_kind(_login(row.get("author"))),
                    at=_committed_at(commit),
                )
            )
        return found

    async def _pages(
        self, repo: RepoRef, path: str, params: dict[str, str], limit: int
    ) -> list[dict[str, Any]]:
        """One list endpoint on the shared ladder, asserted against the allowlist first."""
        self._client.assert_allowed(repo)
        with _readable(path):
            return await self._client.transport.paginate(path, params, limit=limit)


async def sync_issue_channels(
    repos: Sequence[RepoConfig],
    *,
    sessions: SessionFactory,
    client: GitHubClient,
    max_documents: int = MAX_DOCUMENTS_DEFAULT,
) -> list[IssueSyncOutcome]:
    """Every opted-in repo, one refusal never stopping the rest."""
    outcomes: list[IssueSyncOutcome] = []
    for repo_config in repos:
        try:
            outcomes.append(
                await sync_repo_issues(
                    repo_config,
                    sessions=sessions,
                    client=client,
                    max_documents=max_documents,
                )
            )
        except IssueChannelRefusedError as refused:
            LOGGER.warning("%s: %s", refused.code, refused)
            outcomes.append(IssueSyncOutcome(refusal_code=refused.code))
    return outcomes


async def sync_repo_issues(
    repo_config: RepoConfig,
    *,
    sessions: SessionFactory,
    client: GitHubClient,
    max_documents: int = MAX_DOCUMENTS_DEFAULT,
) -> IssueSyncOutcome:
    """One pass over one repo's issue channel, or the named refusal that stopped it."""
    repo = repo_config.repo
    if not repo_config.github_issues_enabled:
        # The default state, not an error. Nothing is asked of GitHub and no
        # source row appears, since an enabled source that never verifies drags
        # the whole corpus's staleness clock down with it.
        return IssueSyncOutcome(refusal_code=IssueRefusal.DISABLED)

    reader = GitHubIssueReader(client)
    org = OrgMembership(client, org=repo.owner)
    branch = await reader.default_branch_of_private_repo(repo)

    started = datetime.now(UTC)
    source_id, since = await ensure_source(sessions, repo)
    # One budget across the three kinds: issues first, then reviews, then commits.
    issues, pulls = await reader.issue_documents(repo, org, since=since, limit=max_documents)
    budget = max(0, max_documents - len(issues))
    reviews = await reader.review_documents(repo, org, numbers=pulls, limit=budget)
    budget = max(0, budget - len(reviews))
    commits = await reader.commit_documents(repo, org, branch=branch, since=since, limit=budget)

    counts = _Counts()
    for document in [*issues, *reviews, *commits]:
        counts.add(document, await store_document(sessions, source_id, document))
    await advance_cursor(sessions, source_id, started)
    return counts.freeze(source_id, org.undetermined)


@dataclass(slots=True)
class _Counts:
    """The numbers one pass reports, folded as each document lands."""

    indexed: int = 0
    unchanged: int = 0
    written: int = 0
    external: int = 0
    secrets: int = 0

    def add(self, document: IssueDocument, stored: StoreOutcome) -> None:
        self.secrets += stored.secrets_skipped
        self.external += document.author_kind == AuthorKind.EXTERNAL
        if stored.unchanged:
            self.unchanged += 1
        else:
            self.indexed += 1
            self.written += stored.chunks_written

    def freeze(self, source_id: int, undetermined: int) -> IssueSyncOutcome:
        return IssueSyncOutcome(
            source_id=source_id,
            documents_indexed=self.indexed,
            documents_unchanged=self.unchanged,
            chunks_written=self.written,
            external_authors=self.external,
            membership_undetermined=undetermined,
            secrets_skipped=self.secrets,
        )


def _document(
    external_id: str, *, path: str, title: str, body: str, author: str, at: object
) -> IssueDocument:
    """One document, both fence-header fields normalized at index time per 4.2."""
    clean = normalize_index_name(title) or external_id
    stripped = body.strip()
    return IssueDocument(
        external_id=external_id,
        path=normalize_index_path(path) or external_id,
        title=clean,
        # A heading so `heading_path` cites the issue, and never an empty section.
        text=f"# {clean}\n\n{stripped}" if stripped else clean,
        author_kind=author,
        source_modified_at=_moment(at),
    )


async def _author_of(org: OrgMembership, row: dict[str, Any]) -> AuthorKind:
    return await org.author_kind(
        _login(row.get("user")), _text(row.get("author_association")) or None
    )


def _mapping(raw: object) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _login(user: object) -> str | None:
    login = _mapping(user).get("login")
    return login if isinstance(login, str) and login else None


def _committed_at(commit: dict[str, Any]) -> object:
    for key in ("committer", "author"):
        entry = _mapping(commit.get(key))
        if entry.get("date"):
            return entry["date"]
    return None


def _text(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


def _int(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _moment(raw: object) -> datetime | None:
    """RFC3339 into an aware datetime; anything unparseable is simply absent."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
