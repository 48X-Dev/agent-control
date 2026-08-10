"""A GitHub that answers over httpx, in the JSON shapes the real API returns.

Stubbed at the transport and not at the client, so the URLs, query parameters and
headers the client builds are what these tests exercise. Two shapes here are
deliberately unforgiving. A recursive tree can answer ``truncated: true`` with a
200 and a partial list, because that is what GitHub does and refusing it is the
assertion K4 added. And a repo this fake does not hold answers 404 rather than an
empty tree, because an empty tree is what an out-of-scope read would look like.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

API_HOST = "api.github.com"

_COMPARE_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/compare/(.+?)\.\.\.(.+)$")
_TREE_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/git/trees/([^/]+)$")
_BLOB_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/git/blobs/([^/]+)$")
_COMMITS_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/commits$")
_REPO_RE = re.compile(r"^/repos/([^/]+)/([^/]+)$")

DEFAULT_COMMIT_DATE = "2026-08-09T18:20:00Z"


def blob_sha(content: bytes) -> str:
    """Git's own blob hash, so a fixture's shas behave like the real ones."""
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


@dataclass
class FakeRepo:
    """One repository: its default branch, its head, and the blobs on it."""

    owner: str
    name: str
    default_branch: str = "main"
    head: str = "a" * 40
    commit_date: str = DEFAULT_COMMIT_DATE
    files: dict[str, bytes] = field(default_factory=dict)
    truncated: bool = False
    compares: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def add(self, path: str, content: bytes | str = b"# doc\n") -> str:
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.files[path] = body
        return blob_sha(body)

    def tree(self) -> list[dict[str, Any]]:
        return [
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha(content),
                "size": len(content),
            }
            for path, content in self.files.items()
        ]

    def set_compare(
        self,
        base: str,
        *,
        modified: tuple[str, ...] = (),
        removed: tuple[str, ...] = (),
        renamed: tuple[tuple[str, str], ...] = (),
        status: str = "ahead",
    ) -> None:
        """Register one compare answer against the base sha that asks for it."""
        files: list[dict[str, Any]] = []
        for path in modified:
            files.append(
                {"filename": path, "status": "modified", "sha": blob_sha(self.files[path])}
            )
        for path in removed:
            files.append({"filename": path, "status": "removed", "sha": "0" * 40})
        for previous, path in renamed:
            files.append(
                {
                    "filename": path,
                    "status": "renamed",
                    "previous_filename": previous,
                    "sha": blob_sha(self.files[path]),
                }
            )
        self.compares[base] = {"status": status, "files": files}


class FakeGitHub:
    """An in-memory host plus the request log the scope assertions read."""

    def __init__(self) -> None:
        self.repos: dict[str, FakeRepo] = {}
        self.requests: list[httpx.Request] = []
        self.statuses: dict[str, list[int]] = {}
        self.rate_limit: dict[str, str] = {}

    def repo(self, full_name: str, **kwargs: Any) -> FakeRepo:
        owner, _, name = full_name.partition("/")
        made = FakeRepo(owner=owner, name=name, **kwargs)
        self.repos[full_name.lower()] = made
        return made

    def fail_next(self, fragment: str, *statuses: int) -> None:
        """Queue transient statuses for any path containing ``fragment``."""
        self.statuses.setdefault(fragment, []).extend(statuses)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        queued = self._queued(path)
        if queued is not None:
            return self._respond(queued, {"message": "Server Error"})
        for pattern, handler in (
            (_COMPARE_RE, self._compare),
            (_TREE_RE, self._tree),
            (_BLOB_RE, self._blob),
            (_COMMITS_RE, self._commits),
            (_REPO_RE, self._repo),
        ):
            match = pattern.match(path)
            if match is not None:
                return handler(request, match)
        return self._respond(404, {"message": "Not Found"})

    def _queued(self, path: str) -> int | None:
        for fragment, statuses in self.statuses.items():
            if fragment in path and statuses:
                return statuses.pop(0)
        return None

    def _respond(self, status: int, body: Any) -> httpx.Response:
        return httpx.Response(status, json=body, headers=dict(self.rate_limit))

    def _find(self, match: re.Match[str]) -> FakeRepo | None:
        return self.repos.get(f"{match.group(1)}/{match.group(2)}".lower())

    def _repo(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        repo = self._find(match)
        if repo is None:
            return self._respond(404, {"message": "Not Found"})
        return self._respond(
            200, {"full_name": repo.full_name, "default_branch": repo.default_branch}
        )

    def _commits(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        repo = self._find(match)
        if repo is None:
            return self._respond(404, {"message": "Not Found"})
        if request.url.params.get("sha") not in (None, repo.default_branch, repo.head):
            return self._respond(422, {"message": "No commit found for SHA"})
        return self._respond(
            200,
            [
                {
                    "sha": repo.head,
                    "commit": {"committer": {"date": repo.commit_date}},
                }
            ],
        )

    def _tree(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        repo = self._find(match)
        if repo is None or match.group(3) != repo.head:
            return self._respond(404, {"message": "Not Found"})
        return self._respond(
            200,
            {"sha": repo.head, "truncated": repo.truncated, "tree": repo.tree()},
        )

    def _blob(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        repo = self._find(match)
        if repo is None:
            return self._respond(404, {"message": "Not Found"})
        wanted = match.group(3)
        for content in repo.files.values():
            if blob_sha(content) == wanted:
                return self._respond(
                    200,
                    {
                        "sha": wanted,
                        "size": len(content),
                        "encoding": "base64",
                        "content": base64.b64encode(content).decode("ascii"),
                    },
                )
        return self._respond(404, {"message": "Not Found"})

    def _compare(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        repo = self._find(match)
        if repo is None:
            return self._respond(404, {"message": "Not Found"})
        payload = repo.compares.get(match.group(3))
        if payload is None:
            return self._respond(404, {"message": "Not Found"})
        return self._respond(200, payload)
