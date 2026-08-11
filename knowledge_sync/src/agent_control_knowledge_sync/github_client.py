"""What the sync asks GitHub for, inside the allowlist and nowhere else.

How a call is made is ``github_transport.py``'s: the retry ladder, the hourly
budget, and the error family both layers raise. What stays here is the reading
itself, and the one rule that is not the Drive client's. Every call asserts its
repo against the allowlist this client was built with, because under the classic
PAT in use GitHub enforces no scope of its own (section 6, 2026-08-10). An
upstream that could not be reached still raises rather than answering, so
nothing downstream can read "the network broke" as "the file is gone".

``transport``, ``repo_metadata`` and ``assert_allowed`` are public because the
issue channel reads through this same client. Both channels spend one hourly
budget, so one object counts it and one asserts the allowlist. The error classes
are re-exported here for the same reason: this is the module the channels import.
"""

from __future__ import annotations

import base64
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from agent_control_models.knowledge import is_secret_filename

from .allowlist import REFUSED_PATH_SEGMENTS, RepoConfig, RepoRef
from .config import SyncConfig
from .github_transport import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubRefusalError,
    GitHubRepoError,
    GitHubResyncError,
    GitHubScopeError,
    GitHubTransport,
    GitHubTreeTruncatedError,
    GitHubUnreachableError,
)

__all__ = [
    "GitHubClient",
    "GitHubError",
    "GitHubFile",
    "GitHubRateLimitedError",
    "GitHubRefusalError",
    "GitHubRepoError",
    "GitHubResyncError",
    "GitHubScopeError",
    "GitHubTreeTruncatedError",
    "GitHubUnreachableError",
    "RepoMetadata",
    "external_id_for",
    "is_indexable_path",
    "path_refusal",
]

# Compare answers with at most this many files however it is paged, so hitting
# it means the diff is not the whole diff and the repo is walked again instead.
COMPARE_FILE_CAP = 300

_REFUSED_SUFFIXES = (
    ".lock",
    "-lock.json",
    "-lock.yaml",
    "-lock.yml",
)
_REFUSED_FILENAMES = frozenset(
    {"package-lock.json", "yarn.lock", "npm-shrinkwrap.json", "go.sum", "gemfile.lock"}
)
_DOCS_PREFIX = "docs/"
_README_PREFIX = "readme"
_MARKDOWN_SUFFIXES = (".md", ".markdown")


@dataclass(frozen=True, slots=True)
class GitHubFile:
    """One blob on a default branch, as the corpus identifies it."""

    repo: RepoRef
    path: str
    sha: str
    size: int
    external_id: str


@dataclass(frozen=True, slots=True)
class RepoMetadata:
    """``/repos/{full_name}`` reduced to what a run reads, so no channel fetches it twice."""

    default_branch: str
    private: bool


def external_id_for(repo: RepoRef, path: str) -> str:
    """``owner/repo:path``, the identity `documents.external_id` carries."""
    return f"{repo.full_name}:{path}"


def path_refusal(path: str) -> str | None:
    """The code refusing this path outright, or ``None``; checked before any admit."""
    segments = path.split("/")
    leaf = segments[-1]
    if REFUSED_PATH_SEGMENTS.intersection(segments[:-1]):
        return "denied_path"
    lowered = leaf.lower()
    if lowered in _REFUSED_FILENAMES or lowered.endswith(_REFUSED_SUFFIXES):
        return "denied_path"
    if ".min." in lowered:
        return "denied_path"
    if is_secret_filename(leaf):
        return "secret_file"
    return None


def is_indexable_path(path: str, include_paths: Sequence[str] = ()) -> bool:
    """Slice one: READMEs anywhere, everything under `docs/`, root `*.md`, plus widenings."""
    if not path or path.startswith("/"):
        return False
    leaf = path.rsplit("/", 1)[-1].lower()
    if leaf.startswith(_README_PREFIX):
        return True
    if path.startswith(_DOCS_PREFIX):
        return True
    if "/" not in path and leaf.endswith(_MARKDOWN_SUFFIXES):
        return True
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in include_paths)


class GitHubClient:
    """Reads allowlisted repos. A repo absent from ``repos`` cannot be requested at all."""

    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient,
        config: SyncConfig,
        *,
        repos: Sequence[RepoConfig] = (),
    ) -> None:
        self.transport = GitHubTransport(token, client, config)
        self._config = config
        self._admitted = {item.repo.full_name.lower(): item for item in repos}
        self._heads: dict[str, tuple[str, datetime | None]] = {}
        self.refusals: Counter[str] = Counter()

    @property
    def rate_limit_remaining(self) -> int | None:
        """What the last answer said was left of the budget the two channels share."""
        return self.transport.rate_limit_remaining

    @property
    def rate_limit_reset_at(self) -> float | None:
        """When that budget refills, in epoch seconds, or ``None`` if nothing said."""
        return self.transport.rate_limit_reset_at

    async def repo_metadata(self, repo: RepoRef) -> RepoMetadata:
        """One ``/repos/{full_name}`` call, since both channels read from that answer."""
        self.assert_allowed(repo)
        payload = await self.transport.json(f"/repos/{repo.full_name}", {}, subject=repo.full_name)
        return RepoMetadata(
            default_branch=str(payload.get("default_branch") or ""),
            private=bool(payload.get("private")),
        )

    async def default_branch(self, repo: RepoRef) -> str:
        """The branch slice one indexes, read from the repo rather than assumed."""
        branch = (await self.repo_metadata(repo)).default_branch
        if not branch:
            raise GitHubRepoError(
                "repo_unreachable", f"{repo.full_name} reported no default branch."
            )
        return branch

    async def head_sha(self, repo: RepoRef, branch: str) -> str:
        """The commit this run pins, and the date its files are last known good at."""
        self.assert_allowed(repo)
        payload = await self.transport.json(
            f"/repos/{repo.full_name}/commits",
            {"sha": branch, "per_page": "1"},
            subject=f"{repo.full_name}@{branch}",
            expect_list=True,
        )
        commits = payload.get("items") or []
        if not commits or not isinstance(commits[0], dict):
            raise GitHubRepoError(
                "branch_unreachable", f"{repo.full_name} has no commits on {branch!r}."
            )
        head = commits[0]
        sha = str(head.get("sha") or "")
        if not sha:
            raise GitHubRepoError(
                "branch_unreachable", f"{repo.full_name}@{branch} answered with no commit sha."
            )
        self._heads[repo.full_name.lower()] = (sha, _commit_time(head))
        return sha

    def last_commit_at(self, repo: RepoRef) -> datetime | None:
        """When the pinned commit landed; the honest upper bound on a file's age."""
        cached = self._heads.get(repo.full_name.lower())
        return None if cached is None else cached[1]

    async def walk_files(self, repo: RepoRef) -> AsyncIterator[GitHubFile]:
        """Every indexable blob at the head commit, or a refusal if the tree was cut."""
        self.assert_allowed(repo)
        sha = await self._resolved_head(repo)
        payload = await self.transport.json(
            f"/repos/{repo.full_name}/git/trees/{sha}",
            {"recursive": "1"},
            subject=f"{repo.full_name}@{sha}",
        )
        if payload.get("truncated"):
            raise GitHubTreeTruncatedError(
                f"GitHub truncated the recursive tree for {repo.full_name}@{sha}. It answers "
                "200 with a partial tree rather than erroring, so indexing this would be a "
                "silently incomplete mirror; the repo is refused instead."
            )
        for entry in payload.get("tree") or []:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            file = self._admit(repo, str(entry.get("path") or ""), entry)
            if file is not None:
                yield file

    async def changed_files(
        self, repo: RepoRef, base_sha: str
    ) -> tuple[list[GitHubFile], list[str], str]:
        """The diff since the stored cursor: files to re-read, paths gone, the new head.

        Raises :class:`GitHubResyncError` when the stored base is no longer an
        ancestor. A force push leaves a cursor that compare answers 404 or
        ``diverged`` for, and the only correct response is to walk the repo again:
        a partial diff against a rewritten history is missing whatever the rewrite
        touched, silently.
        """
        self.assert_allowed(repo)
        head = await self._resolved_head(repo)
        if head == base_sha:
            return [], [], head
        subject = f"{repo.full_name} {base_sha}...{head}"
        response = await self.transport.request(
            f"/repos/{repo.full_name}/compare/{base_sha}...{head}", {"per_page": "100"}
        )
        if response.status_code in (404, 422):
            raise GitHubResyncError(
                "force_push_relist",
                f"GitHub answered HTTP {response.status_code} comparing {subject}; the stored "
                "base is no longer an ancestor, which is what a force push looks like. The "
                "repo is walked whole rather than diffed against a history that moved.",
            )
        payload = self.transport.payload(response, subject)
        if payload.get("status") == "diverged":
            raise GitHubResyncError(
                "force_push_relist",
                f"{subject} reports 'diverged': the stored base is not an ancestor of the "
                "head, so the diff would omit whatever the rewrite touched.",
            )
        entries = payload.get("files") or []
        if len(entries) >= COMPARE_FILE_CAP:
            raise GitHubResyncError(
                "compare_truncated",
                f"{subject} returned {len(entries)} files, at or over GitHub's {COMPARE_FILE_CAP} "
                "ceiling, so the diff is not the whole diff.",
            )
        return (*self._split(repo, entries), head)

    async def fetch_blob(self, file: GitHubFile) -> bytes:
        """One blob's bytes, refusing the oversize before spending a call on it."""
        self.assert_allowed(file.repo)
        ceiling = self._config.max_file_bytes
        if file.size > ceiling:
            raise GitHubRefusalError(
                "oversize",
                f"{file.external_id} is {file.size} bytes, over the {ceiling}-byte ceiling; "
                "it was refused rather than downloaded.",
            )
        response = await self.transport.request(
            f"/repos/{file.repo.full_name}/git/blobs/{file.sha}", {}
        )
        if response.status_code in (403, 404):
            raise GitHubRefusalError(
                "blob_unreadable",
                f"GitHub answered HTTP {response.status_code} for {file.external_id}; its "
                "bytes are not in the corpus.",
            )
        payload = self.transport.payload(response, file.external_id)
        data = _decode_blob(payload, file.external_id)
        if len(data) > ceiling:
            raise GitHubRefusalError(
                "oversize",
                f"{file.external_id} came back as {len(data)} bytes, over the "
                f"{ceiling}-byte ceiling.",
            )
        return data

    def assert_allowed(self, repo: RepoRef) -> None:
        """At the call site, not only at load time: GitHub will not refuse this for us."""
        if repo.full_name.lower() not in self._admitted:
            raise GitHubScopeError(
                f"{repo.full_name} is not in the allowlist, so this sync does not read it. "
                "The credential in use would have answered, which is why this is asserted "
                "here and not left to GitHub."
            )

    def _admit(self, repo: RepoRef, path: str, entry: Mapping[str, Any]) -> GitHubFile | None:
        """One tree or compare entry through the filters, counting what they refuse."""
        config = self._admitted[repo.full_name.lower()]
        if not path or not is_indexable_path(path, config.include_paths):
            return None
        refusal = path_refusal(path)
        if refusal is not None:
            self.refusals[refusal] += 1
            return None
        size = entry.get("size")
        return GitHubFile(
            repo=repo,
            path=path,
            sha=str(entry.get("sha") or ""),
            size=int(size) if isinstance(size, int) else 0,
            external_id=external_id_for(repo, path),
        )

    def _split(self, repo: RepoRef, entries: Sequence[Any]) -> tuple[list[GitHubFile], list[str]]:
        """Compare's file list into what to re-read and what to tombstone."""
        changed: list[GitHubFile] = []
        removed: list[str] = []
        config = self._admitted[repo.full_name.lower()]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("filename") or "")
            status = str(entry.get("status") or "")
            previous = str(entry.get("previous_filename") or "")
            if previous and is_indexable_path(previous, config.include_paths):
                removed.append(previous)
            if status == "removed":
                if is_indexable_path(path, config.include_paths):
                    removed.append(path)
                continue
            file = self._admit(repo, path, entry)
            if file is not None:
                changed.append(file)
        return changed, removed

    async def _resolved_head(self, repo: RepoRef) -> str:
        cached = self._heads.get(repo.full_name.lower())
        if cached is not None:
            return cached[0]
        return await self.head_sha(repo, await self.default_branch(repo))


def _decode_blob(payload: Mapping[str, Any], subject: str) -> bytes:
    encoding = str(payload.get("encoding") or "")
    content = payload.get("content")
    if not isinstance(content, str):
        raise GitHubRefusalError("blob_unreadable", f"{subject} carried no blob content.")
    if encoding == "base64":
        try:
            return base64.b64decode(content)
        except (ValueError, TypeError) as exc:
            raise GitHubRefusalError(
                "blob_unreadable", f"{subject} carried base64 that does not decode."
            ) from exc
    if encoding in ("utf-8", "utf8", ""):
        return content.encode("utf-8")
    raise GitHubRefusalError(
        "blob_unreadable",
        f"{subject} came back {encoding!r}-encoded, which this sync does not read.",
    )


def _commit_time(head: Mapping[str, Any]) -> datetime | None:
    commit = head.get("commit")
    if not isinstance(commit, dict):
        return None
    committer = commit.get("committer") or commit.get("author")
    if not isinstance(committer, dict):
        return None
    raw = committer.get("date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
