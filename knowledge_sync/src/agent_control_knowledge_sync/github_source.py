"""One pass over the allowlisted repos, as a source adapter the run loop drives.

Cursors advance only for completed sweeps, and an unreachable repo tombstones nothing.
"""

from __future__ import annotations

import mimetypes
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from agent_control_models.files import sniff_mime

from .allowlist import RepoConfig, RepoRef
from .config import SyncConfig
from .github_client import (
    GitHubClient,
    GitHubError,
    GitHubFile,
    GitHubRefusalError,
    GitHubResyncError,
    external_id_for,
)

__all__ = [
    "SOURCE_KIND",
    "TOMBSTONE_DELETED",
    "TOMBSTONE_EXCLUDED",
    "GitHubDocument",
    "GitHubSource",
    "KnowledgeWriter",
    "RepoSweep",
    "WriteOutcome",
    "document_path",
]

# `sources.kind` and the two `documents.tombstone_reason` values this module can
# justify, spelled here rather than imported so a concurrent edit to the write
# path cannot break this module's import.
SOURCE_KIND = "github_repo"
TOMBSTONE_DELETED = "deleted"
TOMBSTONE_EXCLUDED = "excluded"

CURSOR_KEY = "head_sha"

_MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".mdx": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/plain",
    ".adoc": "text/plain",
    ".org": "text/plain",
    ".csv": "text/csv",
}
_DEFAULT_MIME = "text/markdown"


@dataclass(frozen=True, slots=True)
class GitHubDocument:
    """One repo file resolved to what the corpus stores, bytes included."""

    external_id: str
    path: str
    title: str
    source_mime: str
    modified_at: datetime | None
    size: int
    data: bytes


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """What one write did, in the three states a run counts separately."""

    indexed: bool = False
    unchanged: bool = False
    refusal_code: str | None = None


class KnowledgeWriter(Protocol):
    """What this source needs of the corpus, and the whole of what the run loop supplies."""

    async def write(self, document: GitHubDocument) -> WriteOutcome: ...

    async def tombstone(self, external_id: str, *, reason: str) -> bool: ...

    async def live_external_ids(self) -> set[str]: ...


@dataclass(slots=True)
class RepoSweep:
    """One repo's pass: what to record, and the cursor to store only if it earned one."""

    repo: RepoRef
    cursor: str | None = None
    status: str = "ok"
    error_code: str | None = None
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    tombstoned: int = 0
    refusals: Counter[str] = field(default_factory=Counter)

    @property
    def complete(self) -> bool:
        return self.status == "ok"


def document_path(repo: RepoRef, path: str) -> str:
    """The citation form: ``agent-control:docs/plans/task-dispatcher.md``."""
    return f"{repo.name}:{path}"


class GitHubSource:
    """Sweeps the allowlisted repos through a writer, one repo at a time."""

    def __init__(
        self,
        client: GitHubClient,
        repos: Sequence[RepoConfig],
        config: SyncConfig,
    ) -> None:
        self._client = client
        self._repos = tuple(repos)
        self._config = config

    @property
    def repos(self) -> tuple[RepoConfig, ...]:
        """What a run must ensure a `sources` row for, in order."""
        return self._repos

    async def sweep_all(
        self,
        writer: KnowledgeWriter,
        *,
        cursors: Mapping[str, str | None] | None = None,
        budget: int | None = None,
    ) -> list[RepoSweep]:
        """Every allowlisted repo under one shared document budget."""
        stored = cursors or {}
        remaining = self._config.max_documents_per_run if budget is None else budget
        results: list[RepoSweep] = []
        for repo_config in self._repos:
            sweep = await self.sweep(
                repo_config,
                writer,
                cursor=stored.get(repo_config.repo.full_name),
                budget=remaining,
            )
            results.append(sweep)
            remaining -= sweep.seen
            if sweep.error_code == "rate_limited" or remaining <= 0:
                break
        return results

    async def sweep(
        self,
        repo_config: RepoConfig,
        writer: KnowledgeWriter,
        *,
        cursor: str | None = None,
        budget: int | None = None,
    ) -> RepoSweep:
        """One repo: walk it or diff it, and report what a run should write down."""
        repo = repo_config.repo
        sweep = RepoSweep(repo=repo)
        allowance = self._config.max_documents_per_run if budget is None else budget
        before = Counter(self._client.refusals)
        try:
            if cursor is None:
                await self._walk(repo_config, writer, sweep, allowance)
            else:
                await self._replay(repo_config, writer, sweep, cursor, allowance)
        except GitHubError as exc:
            _fail(sweep, exc)
        sweep.refusals += Counter(self._client.refusals) - before
        return sweep

    async def _walk(
        self,
        repo_config: RepoConfig,
        writer: KnowledgeWriter,
        sweep: RepoSweep,
        allowance: int,
    ) -> None:
        """The whole default branch. A complete walk is also the removal evidence."""
        repo = repo_config.repo
        head = await self._client.head_sha(repo, await self._client.default_branch(repo))
        indexed: set[str] = set()
        async for file in self._client.walk_files(repo):
            if sweep.seen >= allowance:
                sweep.status = "partial"
                sweep.error_code = "source_ceiling"
                return
            indexed.add(file.external_id)
            await self._ingest(file, writer, sweep)
        await self._reconcile(writer, sweep, indexed)
        sweep.cursor = head

    async def _replay(
        self,
        repo_config: RepoConfig,
        writer: KnowledgeWriter,
        sweep: RepoSweep,
        cursor: str,
        allowance: int,
    ) -> None:
        """The diff since the cursor, falling back to a whole walk on a rewritten history."""
        repo = repo_config.repo
        try:
            changed, removed, head = await self._client.changed_files(repo, cursor)
        except GitHubResyncError as resync:
            await self._walk(repo_config, writer, sweep, allowance)
            if sweep.complete:
                sweep.status = "partial"
                sweep.error_code = resync.code
            return
        for external_id in (external_id_for(repo, path) for path in removed):
            if await writer.tombstone(external_id, reason=TOMBSTONE_DELETED):
                sweep.tombstoned += 1
        for file in changed:
            if sweep.seen >= allowance:
                sweep.status = "partial"
                sweep.error_code = "source_ceiling"
                return
            await self._ingest(file, writer, sweep)
        sweep.cursor = head

    async def _reconcile(
        self, writer: KnowledgeWriter, sweep: RepoSweep, indexed: set[str]
    ) -> None:
        """Tombstone what a complete tree did not contain, and only then."""
        prefix = f"{sweep.repo.full_name}:"
        for external_id in await writer.live_external_ids():
            if external_id.startswith(prefix) and external_id not in indexed:
                if await writer.tombstone(external_id, reason=TOMBSTONE_EXCLUDED):
                    sweep.tombstoned += 1

    async def _ingest(self, file: GitHubFile, writer: KnowledgeWriter, sweep: RepoSweep) -> None:
        """Fetch one blob, refuse what is not text, and hand the rest to the writer."""
        sweep.seen += 1
        try:
            data = await self._client.fetch_blob(file)
        except GitHubRefusalError as refusal:
            sweep.refusals[refusal.code] += 1
            return
        if sniff_mime(data) is not None:
            sweep.refusals["binary"] += 1
            return
        outcome = await writer.write(self._document(file, data))
        if outcome.refusal_code is not None:
            sweep.refusals[outcome.refusal_code] += 1
        elif outcome.unchanged:
            sweep.unchanged += 1
        elif outcome.indexed:
            sweep.indexed += 1

    def _document(self, file: GitHubFile, data: bytes) -> GitHubDocument:
        return GitHubDocument(
            external_id=file.external_id,
            path=document_path(file.repo, file.path),
            title=file.path.rsplit("/", 1)[-1],
            source_mime=source_mime_for(file.path),
            modified_at=self._client.last_commit_at(file.repo),
            size=len(data),
            data=data,
        )


def source_mime_for(path: str) -> str:
    """A repo file has no declared type, so the extension names one the converter reads."""
    leaf = path.rsplit("/", 1)[-1]
    suffix = f".{leaf.rsplit('.', 1)[-1].lower()}" if "." in leaf else ""
    if not suffix:
        return _DEFAULT_MIME
    known = _MIME_BY_SUFFIX.get(suffix)
    if known is not None:
        return known
    guessed, _ = mimetypes.guess_type(leaf)
    return guessed or "text/plain"


def _fail(sweep: RepoSweep, exc: GitHubError) -> None:
    """A repo the run could not read keeps its cursor and tombstones nothing."""
    sweep.status = "failed"
    sweep.error_code = exc.code
    sweep.cursor = None
