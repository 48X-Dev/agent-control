"""The stubbed collaborators a run is driven through, and the runner that drives them."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials
from agent_control_knowledge_sync.drive_client import (
    DriveChange,
    DriveItem,
    DriveRefusalRecord,
    FetchedContent,
    FolderLocation,
    UnderRoot,
)
from agent_control_knowledge_sync.ingest import (
    REFUSAL_TOMBSTONES,
    IngestOutcome,
    SourceItem,
    TombstoneReason,
)
from agent_control_knowledge_sync.sync import (
    RunCounters,
    run_once_with,
)

ROOT_ID = "folder-root"

ROOT = DriveItem(
    id=ROOT_ID,
    name="Company Knowledge",
    mime_type="application/vnd.google-apps.folder",
    modified_time=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    size=None,
    md5_checksum=None,
    trashed=False,
    shortcut_target_id=None,
)


def _item(file_id: str, name: str = "Handbook.pdf") -> DriveItem:
    return DriveItem(
        id=file_id,
        name=name,
        mime_type="application/pdf",
        modified_time=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
        size=1024,
        md5_checksum="abc",
        trashed=False,
        shortcut_target_id=None,
    )


def _config(**overrides: Any) -> SyncConfig:
    fields: dict[str, Any] = {
        "credentials": DriveCredentials(client_id="id", client_secret="s", refresh_token="r"),
        "root_folder_id": ROOT_ID,
        "database_url": "postgresql+psycopg://knowledge_sync:x@localhost/agent_knowledge",
    }
    fields.update(overrides)
    return SyncConfig(**fields)


class FakeClient:
    """Every Drive call the runner makes, logged in the order it made them."""

    def __init__(
        self,
        *,
        items: list[DriveItem] | None = None,
        changes: list[DriveChange] | None = None,
        new_cursor: str = "cursor-2",
        content: dict[str, bytes | Exception] | None = None,
        locations: dict[str, FolderLocation] | None = None,
        root: DriveItem | Exception = ROOT,
        walk_truncated: bool = False,
    ) -> None:
        self.items = items or []
        self.changes = changes or []
        self.new_cursor = new_cursor
        self.content = content or {}
        self.locations = locations or {}
        self.root = root
        self.walk_truncated = walk_truncated
        self.refusals: list[DriveRefusalRecord] = []
        self.events: list[str] = []

    async def resolve_root(self) -> DriveItem:
        self.events.append("resolve_root")
        if isinstance(self.root, Exception):
            raise self.root
        return self.root

    async def start_cursor(self) -> str:
        self.events.append("start_cursor")
        return "cursor-1"

    async def walk_subtree(self) -> AsyncIterator[DriveItem]:
        for item in self.items:
            self.events.append(f"walk:{item.id}")
            yield item

    async def list_changes(self, cursor: str) -> tuple[list[DriveChange], str]:
        self.events.append(f"list_changes:{cursor}")
        return self.changes, self.new_cursor

    async def resolve_folder_path(self, file_id: str) -> FolderLocation:
        self.events.append(f"folder_path:{file_id}")
        return self.locations.get(file_id, UnderRoot(()))

    async def fetch_content(self, item: DriveItem) -> FetchedContent:
        self.events.append(f"fetch:{item.id}")
        answer = self.content.get(item.id, b"body")
        if isinstance(answer, Exception):
            raise answer
        return FetchedContent(answer, item.mime_type)


class FakeIngestor:
    """Records what reached it and answers whatever the test set up."""

    def __init__(
        self,
        *,
        outcomes: dict[str, IngestOutcome] | None = None,
        tombstones: dict[str, bool] | None = None,
        secrets_skipped: int = 0,
    ) -> None:
        self.outcomes = outcomes or {}
        self.tombstones = tombstones or {}
        self.secrets_skipped = secrets_skipped
        self.events: list[str] = []

    async def ingest(self, item: SourceItem) -> IngestOutcome:
        self.events.append(f"ingest:{item.external_id}")
        return self.outcomes.get(item.external_id, IngestOutcome("1", 3, False, None))

    async def tombstone(self, external_id: str, *, reason: str = TombstoneReason.DELETED) -> bool:
        self.events.append(f"tombstone:{external_id}:{reason}")
        return self.tombstones.get(external_id, True)

    async def refuse_fetch(self, external_id: str, code: str) -> bool:
        reason = REFUSAL_TOMBSTONES.get(code)
        if reason is None:
            return False
        self.events.append(f"refuse_fetch:{external_id}:{reason}")
        return self.tombstones.get(external_id, True)


class FakeJournal:
    """The corpus rows, as an ordered event log rather than a database."""

    def __init__(self, *, cursor: str | None = None, schema_error: Exception | None = None) -> None:
        self.cursor = cursor
        self.schema_error = schema_error
        self.events: list[tuple[str, Any]] = []

    async def assert_schema(self) -> int:
        self.events.append(("assert_schema", None))
        if self.schema_error is not None:
            raise self.schema_error
        return 3

    async def lapse_orphans(self, holder: str) -> int:
        self.events.append(("lapse_orphans", holder))
        return 0

    async def open_run(self, holder: str) -> int:
        self.events.append(("open_run", holder))
        return 7

    async def close_run(
        self, run_id: int, *, status: str, counters: RunCounters, tally: Any, error_code: str | None
    ) -> None:
        self.events.append(("close_run", (status, error_code, counters)))

    async def ensure_source(self, *, ref: str, display_name: str) -> Any:
        self.events.append(("ensure_source", (ref, display_name)))
        from agent_control_knowledge_sync.sync import SourceState

        return SourceState(id=11, cursor=self.cursor)

    async def advance_cursor(self, source_id: int, cursor: str) -> None:
        self.events.append(("advance_cursor", cursor))

    async def mark_verified(self, source_id: int, *, status: str, error_code: str | None) -> None:
        self.events.append(("mark_verified", (status, error_code)))

    async def mark_source_failed(self, *, ref: str, error_code: str) -> None:
        self.events.append(("mark_source_failed", error_code))

    async def sweep_tombstones(self, retention_days: int) -> int:
        self.events.append(("sweep_tombstones", retention_days))
        return 0


class FakeLease:
    def __init__(self, *, renews: bool = True) -> None:
        self.holder = "run-token"
        self.renews = renews
        self.renewals = 0

    async def renew(self) -> bool:
        self.renewals += 1
        return self.renews


async def _run(
    client: FakeClient,
    journal: FakeJournal,
    ingestor: FakeIngestor,
    *,
    lease: FakeLease | None = None,
    config: SyncConfig | None = None,
    github: Any = None,
) -> RunCounters:
    return await run_once_with(
        config or _config(),
        client=client,  # type: ignore[arg-type]
        journal=journal,  # type: ignore[arg-type]
        lease=lease or FakeLease(),  # type: ignore[arg-type]
        ingestor_factory=lambda _: ingestor,  # type: ignore[arg-type,return-value]
        github=github,
    )


def _kinds(journal: FakeJournal) -> list[str]:
    return [name for name, _ in journal.events]
