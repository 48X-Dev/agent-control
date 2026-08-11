"""The GitHub half of a run: one client, both channels, one record per repo.

Token and allowlist or the channel is off, and it says which half is missing at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from .allowlist import AllowlistError, RepoConfig, load_allowlist
from .config import SyncConfig
from .github_client import GitHubClient, GitHubError
from .github_issues import IssueRefusal, sync_issue_channels
from .github_source import (
    CURSOR_KEY,
    SOURCE_KIND,
    GitHubDocument,
    GitHubSource,
    RepoSweep,
    WriteOutcome,
)
from .ingest import Ingestor, SourceItem
from .journal import SourceState, SyncFailedError, SyncJournal, Tally
from .lease import SessionFactory

__all__ = [
    "CURSOR_KEY",
    "SOURCE_KIND",
    "GitHubChannel",
    "RepoWriter",
    "github_channel",
    "github_journal",
    "run_github",
]

TOKEN_MISSING = "github channel off: AGENT_KNOWLEDGE_GITHUB_TOKEN is unset"
ALLOWLIST_EMPTY = "github channel off: %s lists no repositories"
CHANNEL_ON = "github channel on: %d repo(s) from %s"

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GitHubChannel:
    """A configured channel: a credential and the repos it is allowed to read."""

    token: str
    repos: tuple[RepoConfig, ...]


def github_channel(config: SyncConfig) -> GitHubChannel | None:
    """The channel, or ``None`` and one line saying which half is missing."""
    if not config.github_token:
        _LOG.info(TOKEN_MISSING)
        return None
    try:
        repos = load_allowlist(config.allowlist_path)
    except AllowlistError as exc:
        raise SyncFailedError(str(exc), code=exc.code) from exc
    if not repos:
        _LOG.info(ALLOWLIST_EMPTY, config.allowlist_path)
        return None
    _LOG.info(CHANNEL_ON, len(repos), config.allowlist_path)
    return GitHubChannel(token=config.github_token, repos=repos)


def github_journal(sessions: SessionFactory) -> SyncJournal:
    """A journal speaking ``github_repo`` and storing head shas, not page tokens."""
    return SyncJournal(sessions, kind=SOURCE_KIND, cursor_key=CURSOR_KEY)


class RepoWriter:
    """``github_source.KnowledgeWriter`` over one ``Ingestor`` per repo source row."""

    def __init__(self, journal: SyncJournal, ingestors: dict[str, Ingestor]) -> None:
        self._journal = journal
        self._ingestors = ingestors
        self.bytes_fetched = 0

    @property
    def secrets_skipped(self) -> int:
        return sum(one.secrets_skipped for one in self._ingestors.values())

    async def write(self, document: GitHubDocument) -> WriteOutcome:
        self.bytes_fetched += len(document.data)
        outcome = await self._ingestor(document.external_id).ingest(_source_item(document))
        return WriteOutcome(
            indexed=outcome.refusal_code is None and not outcome.skipped_unchanged,
            unchanged=outcome.skipped_unchanged,
            refusal_code=outcome.refusal_code,
        )

    async def tombstone(self, external_id: str, *, reason: str) -> bool:
        return await self._ingestor(external_id).tombstone(external_id, reason=reason)

    async def live_external_ids(self) -> set[str]:
        live: set[str] = set()
        for ingestor in self._ingestors.values():
            live |= await ingestor.live_external_ids()
        return live

    def _ingestor(self, external_id: str) -> Ingestor:
        """Route on the ``owner/repo`` half of the id; each repo owns a source row."""
        found = self._ingestors.get(external_id.partition(":")[0])
        if found is None:
            raise SyncFailedError(
                f"{external_id} names no repository this run opened a source row for.",
                code="github_source_unknown",
            )
        return found


async def run_github(
    channel: GitHubChannel,
    tally: Tally,
    *,
    config: SyncConfig,
    http: httpx.AsyncClient,
    journal: SyncJournal,
    sessions: SessionFactory,
) -> str | None:
    """Both channels over every allowlisted repo, and the first code that was not ``ok``."""
    client = GitHubClient(channel.token, http, config, repos=channel.repos)
    states = await _open_sources(journal, channel.repos)
    writer = RepoWriter(
        journal, {name: Ingestor(sessions, state.id) for name, state in states.items()}
    )
    source = GitHubSource(client, channel.repos, config)
    cursors = {name: state.cursor for name, state in states.items()}
    reported: str | None = None
    for sweep in await source.sweep_all(writer, cursors=cursors):
        await _record(journal, states[sweep.repo.full_name].id, sweep, tally)
        if not sweep.complete and reported is None:
            # A run row reading `ok` while a repo went unread is the invisible
            # half-mirror this plan exists to refuse.
            reported = sweep.error_code or "github_error"
    tally.secrets_skipped += writer.secrets_skipped
    tally.bytes_fetched += writer.bytes_fetched
    issues = await _run_issues(channel, tally, config=config, client=client, sessions=sessions)
    return reported or issues


async def _run_issues(
    channel: GitHubChannel,
    tally: Tally,
    *,
    config: SyncConfig,
    client: GitHubClient,
    sessions: SessionFactory,
) -> str | None:
    """Phase 6, dark until a repo's allowlist entry opts in, on the same hourly budget."""
    if not any(entry.github_issues_enabled for entry in channel.repos):
        return None
    try:
        outcomes = await sync_issue_channels(
            channel.repos,
            sessions=sessions,
            client=client,
            max_documents=config.max_documents_per_run,
        )
    except GitHubError as exc:
        # The files channel already landed; failing the whole run would bury it.
        _LOG.warning("issue channel stopped: %s", exc)
        return exc.code
    reported: str | None = None
    for outcome in outcomes:
        tally.seen += outcome.documents_indexed + outcome.documents_unchanged
        tally.indexed += outcome.documents_indexed
        tally.unchanged += outcome.documents_unchanged
        tally.secrets_skipped += outcome.secrets_skipped
        # `disabled` is the default state of every repo, not a silence to report.
        if outcome.refusal_code not in (None, IssueRefusal.DISABLED):
            tally.refuse(str(outcome.refusal_code))
            reported = reported or str(outcome.refusal_code)
    return reported


async def _open_sources(
    journal: SyncJournal, repos: Sequence[RepoConfig]
) -> dict[str, SourceState]:
    """One ``sources`` row per repo, since 4.2 keys the cursor on ``owner/repo``."""
    states: dict[str, SourceState] = {}
    for repo_config in repos:
        name = repo_config.repo.full_name
        states[name] = await journal.ensure_source(ref=name, display_name=name)
    return states


async def _record(journal: SyncJournal, source_id: int, sweep: RepoSweep, tally: Tally) -> None:
    """The cursor a sweep earned, the status it reports, and its counts."""
    if sweep.cursor is not None:
        await journal.advance_cursor(source_id, sweep.cursor)
    if sweep.status == "failed":
        # A repo the run could not read is not a repo the run verified, so the
        # freshness clock stays where it was rather than reading as checked.
        await journal.mark_source_failed(
            ref=sweep.repo.full_name, error_code=sweep.error_code or "github_error"
        )
    else:
        await journal.mark_verified(source_id, status=sweep.status, error_code=sweep.error_code)
    tally.seen += sweep.seen
    tally.indexed += sweep.indexed
    tally.unchanged += sweep.unchanged
    tally.tombstoned += sweep.tombstoned
    for code, count in sweep.refusals.items():
        tally.refusals[code] += count


def _source_item(document: GitHubDocument) -> SourceItem:
    """A repo file has no declared type, so the extension's guess is also the fetch's."""
    return SourceItem(
        external_id=document.external_id,
        path=document.path,
        title=document.title,
        data=document.data,
        media_type=document.source_mime,
        source_mime=document.source_mime,
        modified_at=document.modified_at,
        size=document.size,
    )
