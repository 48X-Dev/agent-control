"""What reaches the corpus, without a corpus.

The session is a fake that records statements and hands back canned rows, so
these pin the decisions rather than Postgres: which items are refused before
anything is converted, when a replay writes nothing, and that a document whose
conversion failed still gets a row and still gets no chunks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from agent_control_knowledge_sync.convert import Converted
from agent_control_knowledge_sync.ingest import Ingestor, IngestRefusal, TombstoneReason
from agent_control_knowledge_sync.ingest_guard import AgentOutputGuard
from sqlalchemy.dialects import postgresql

SOURCE_ID = 7

HANDBOOK = b"""# Onboarding

Laptops are ordered on the first day and arrive already enrolled in management,
so nobody waits on IT for a machine they can use. Badges take about a week and
the front desk issues a temporary one in the meantime, which opens every door
the permanent badge opens except the server room.

## Expenses

Anything under fifty pounds needs no approval and anything over it needs one
line of justification in the expense tool, which routes to whoever your manager
is on the day you file it rather than on the day you spent the money.
"""


@dataclass(frozen=True, slots=True)
class Item:
    """Stands in for ``drive_client.DriveItem``."""

    id: str = "file-1"
    name: str = "Onboarding.md"
    mime_type: str = "text/markdown"
    modified_time: datetime | None = datetime(2026, 8, 1, tzinfo=UTC)
    size: int | None = 4096
    md5_checksum: str | None = "d41d8cd98f00b204e9800998ecf8427e"
    trashed: bool = False
    shortcut_target_id: str | None = None
    folder_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Fetched:
    """Stands in for ``drive_client.FetchedContent``.

    The media type is the fetch's, not the item's, because an export replaces
    it: a Google Doc arrives here as markdown under a Drive-native ``mime_type``.
    """

    data: bytes
    media_type: str = "text/markdown"


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0]

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Records every statement and answers from a queue of canned rows."""

    def __init__(self, results: list[list[Any]]) -> None:
        self.results = list(results)
        self.executed: list[tuple[Any, Any]] = []

    async def execute(self, statement: Any, params: Any = None) -> FakeResult:
        self.executed.append((statement, params))
        return FakeResult(self.results.pop(0) if self.results else [])

    def begin(self) -> FakeSession:
        return self

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class Row:
    """One ``documents`` row as ``_existing`` reads it.

    The metadata defaults are what ``Item()`` produces, so a plain ``Row`` is a
    document that has not drifted and a drifted one is spelled out per test.
    """

    def __init__(
        self,
        id: int,
        content_sha256: str,
        tombstoned_at: datetime | None = None,
        *,
        path: str = "Onboarding.md",
        title: str = "Onboarding.md",
        source_mime: str = "text/markdown",
        source_modified_at: datetime | None = datetime(2026, 8, 1, tzinfo=UTC),
        bytes: int = 4096,
    ) -> None:
        self.id = id
        self.content_sha256 = content_sha256
        self.tombstoned_at = tombstoned_at
        self.path = path
        self.title = title
        self.source_mime = source_mime
        self.source_modified_at = source_modified_at
        self.bytes = bytes


def build(
    *results: list[Any], converter: Any = None, guard: Any = None
) -> tuple[Ingestor, FakeSession]:
    session = FakeSession(list(results))
    ingestor = Ingestor(lambda: session, SOURCE_ID, converter=converter, guard=guard)  # type: ignore[arg-type]
    return ingestor, session


class Ancestry:
    """One node's parents, for the guard under test."""

    def __init__(self, parents: dict[str, tuple[str, ...]]) -> None:
        self._parents = parents

    async def parents(self, node_id: str) -> tuple[str, ...]:
        return self._parents.get(node_id, ())


def statements(session: FakeSession, kind: type) -> list[Any]:
    return [statement for statement, _ in session.executed if isinstance(statement, kind)]


def shape(session: FakeSession) -> list[str]:
    """The statement kinds this session saw, in the order it saw them."""
    kinds = (
        (sa.Select, "select"),
        (sa.Insert, "insert"),
        (sa.Delete, "delete"),
        (sa.Update, "update"),
    )
    return [
        next(name for cls, name in kinds if isinstance(statement, cls))
        for statement, _ in session.executed
    ]


def params(statement: Any) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def failing_converter(data: bytes, *, declared_mime: str | None) -> Converted:
    return Converted(text="", status="failed", error_code="converter_error")


@pytest.mark.asyncio
async def test_a_new_document_writes_a_row_and_its_chunks() -> None:
    ingestor, session = build([], [42])
    outcome = await ingestor.ingest(Item(), Fetched(HANDBOOK))

    assert outcome.document_id == "42"
    assert outcome.chunks_written > 0
    assert outcome.skipped_unchanged is False
    assert outcome.refusal_code is None

    written = statements(session, sa.Insert)
    assert len(written) == 2
    assert params(written[0])["title"] == "Onboarding.md"
    assert params(written[0])["author_kind"] == "unknown"
    assert params(written[0])["conversion_status"] == "exported"


@pytest.mark.asyncio
async def test_the_path_carries_the_folders_the_document_was_found_under() -> None:
    """Two folders each holding a `notes.md` are one row unless the path says which."""
    ingestor, session = build([], [42])
    item = Item(name="notes.md", folder_path=("Onboarding", "Laptops"))
    await ingestor.ingest(item, Fetched(HANDBOOK))

    assert params(statements(session, sa.Insert)[0])["path"] == "Onboarding/Laptops/notes.md"


@pytest.mark.asyncio
async def test_the_media_type_comes_from_the_fetch_not_from_the_item() -> None:
    """A Doc exports to markdown; its own type is one no converter accepts."""
    ingestor, session = build([], [42])
    await ingestor.ingest(Item(mime_type="application/vnd.google-apps.document"), Fetched(HANDBOOK))

    written = params(statements(session, sa.Insert)[0])
    assert written["conversion_status"] == "exported"
    # The row still records what Drive called it, which is the source's own type.
    assert written["source_mime"] == "application/vnd.google-apps.document"


@pytest.mark.asyncio
async def test_chunks_are_deleted_before_they_are_written() -> None:
    """A replay of the same batch must not double a document's chunks."""
    ingestor, session = build([], [42])
    await ingestor.ingest(Item(), Fetched(HANDBOOK))

    assert shape(session) == ["select", "insert", "delete", "insert"]


@pytest.mark.asyncio
async def test_unchanged_content_writes_nothing() -> None:
    ingestor, session = build([], [42])
    first = await ingestor.ingest(Item(), Fetched(HANDBOOK))
    digest = params(statements(session, sa.Insert)[0])["content_sha256"]

    replayed, replay_session = build([Row(id=42, content_sha256=digest)])
    outcome = await replayed.ingest(Item(), Fetched(HANDBOOK))

    assert outcome.skipped_unchanged is True
    assert outcome.document_id == "42"
    assert outcome.chunks_written == 0
    assert len(replay_session.executed) == 1
    assert first.document_id == "42"


@pytest.mark.asyncio
async def test_a_rename_updates_the_citation_without_rewriting_the_chunks() -> None:
    """`Q3 review.pptx` renamed to `Q3 review FINAL.pptx` hashes the same.

    Comparing content alone skips the row, and every future snippet cites a
    filename that no longer exists. The chunks are the same bytes under a new
    name, so they are not rewritten.
    """
    ingestor, session = build([], [42])
    await ingestor.ingest(Item(), Fetched(HANDBOOK))
    digest = params(statements(session, sa.Insert)[0])["content_sha256"]

    renamed, replay = build([Row(id=42, content_sha256=digest)])
    outcome = await renamed.ingest(Item(name="Onboarding FINAL.md"), Fetched(HANDBOOK))

    assert outcome.skipped_unchanged is True
    assert outcome.metadata_refreshed is True
    assert outcome.chunks_written == 0
    assert shape(replay) == ["select", "update"]
    updated = params(statements(replay, sa.Update)[0])
    assert updated["title"] == "Onboarding FINAL.md"
    assert updated["path"] == "Onboarding FINAL.md"


@pytest.mark.asyncio
async def test_a_move_and_a_touch_are_metadata_drift_too() -> None:
    ingestor, session = build([], [42])
    await ingestor.ingest(Item(), Fetched(HANDBOOK))
    digest = params(statements(session, sa.Insert)[0])["content_sha256"]

    moved, replay = build([Row(id=42, content_sha256=digest)])
    await moved.ingest(
        Item(folder_path=("Onboarding",), modified_time=datetime(2026, 8, 9, tzinfo=UTC)),
        Fetched(HANDBOOK),
    )

    updated = params(statements(replay, sa.Update)[0])
    assert updated["path"] == "Onboarding/Onboarding.md"
    assert updated["source_modified_at"] == datetime(2026, 8, 9, tzinfo=UTC)


@pytest.mark.asyncio
async def test_only_the_columns_that_drifted_are_written() -> None:
    """A rename is one column. Rewriting the row would touch chunks nobody asked for."""
    ingestor, session = build([], [42])
    await ingestor.ingest(Item(), Fetched(HANDBOOK))
    digest = params(statements(session, sa.Insert)[0])["content_sha256"]

    renamed, replay = build([Row(id=42, content_sha256=digest)])
    await renamed.ingest(Item(name="Onboarding FINAL.md"), Fetched(HANDBOOK))

    written = params(statements(replay, sa.Update)[0])
    assert set(written) == {"path", "title", "id_1"}


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("oversize", TombstoneReason.OVERSIZE),
        ("export_too_large", TombstoneReason.OVERSIZE),
        ("unreadable", TombstoneReason.UNSHARED),
        ("shortcut_unreadable", TombstoneReason.UNSHARED),
    ],
)
@pytest.mark.asyncio
async def test_a_fetch_refusal_buries_what_the_corpus_already_holds(
    code: str, reason: TombstoneReason
) -> None:
    """A deck that grew past the ceiling keeps every stale chunk searchable otherwise."""
    ingestor, session = build([42])

    assert await ingestor.refuse_fetch("file-1", code) is True
    assert params(statements(session, sa.Update)[0])["tombstone_reason"] == reason
    assert len(statements(session, sa.Delete)) == 1


@pytest.mark.asyncio
async def test_a_refusal_with_no_tombstone_of_its_own_writes_nothing() -> None:
    """A video has no text path and never had chunks; burying it would invent history."""
    ingestor, session = build([42])

    assert await ingestor.refuse_fetch("file-1", "no_text_export") is False
    assert session.executed == []


@pytest.mark.asyncio
async def test_a_tombstoned_document_with_the_same_bytes_is_written_again() -> None:
    """The file came back. A matching hash is not a reason to leave it buried."""
    ingestor, session = build([], [42])
    await ingestor.ingest(Item(), Fetched(HANDBOOK))
    digest = params(statements(session, sa.Insert)[0])["content_sha256"]

    resurrect, _ = build(
        [Row(id=42, content_sha256=digest, tombstoned_at=datetime(2026, 7, 1, tzinfo=UTC))],
        [42],
    )
    outcome = await resurrect.ingest(Item(), Fetched(HANDBOOK))

    assert outcome.skipped_unchanged is False
    assert outcome.chunks_written > 0


@pytest.mark.asyncio
async def test_a_trashed_item_is_tombstoned_and_never_converted() -> None:
    ingestor, session = build([99])
    outcome = await ingestor.ingest(Item(trashed=True), Fetched(HANDBOOK))

    assert outcome.refusal_code == IngestRefusal.TRASHED
    assert outcome.document_id is None
    assert params(statements(session, sa.Update)[0])["tombstone_reason"] == TombstoneReason.DELETED


@pytest.mark.asyncio
async def test_a_credential_by_filename_never_reaches_the_converter() -> None:
    ingestor, session = build([99])
    outcome = await ingestor.ingest(
        Item(name="id_rsa", mime_type="text/plain"), Fetched(b"-----BEGIN", "text/plain")
    )

    assert outcome.refusal_code == IngestRefusal.SECRET_FILE
    reason = params(statements(session, sa.Update)[0])["tombstone_reason"]
    assert reason == TombstoneReason.SECRET_FILE
    assert statements(session, sa.Insert) == []


@pytest.mark.asyncio
async def test_agent_output_never_reaches_the_converter() -> None:
    """Section 11: an agent's speculation becomes a citation a week later otherwise."""
    tree = {"file-1": ("deliverables",), "deliverables": ("executor-root",)}
    ingestor, session = build([99], guard=AgentOutputGuard("executor-root", Ancestry(tree)))

    outcome = await ingestor.ingest(Item(), Fetched(HANDBOOK))

    assert outcome.refusal_code == IngestRefusal.AGENT_OUTPUT
    assert statements(session, sa.Insert) == []
    reason = params(statements(session, sa.Update)[0])["tombstone_reason"]
    assert reason == TombstoneReason.EXCLUDED


@pytest.mark.asyncio
async def test_a_workspace_document_passes_the_guard() -> None:
    guard = AgentOutputGuard("executor-root", Ancestry({"file-1": ("ops-handbook",)}))
    ingestor, session = build([], [42], guard=guard)

    outcome = await ingestor.ingest(Item(), Fetched(HANDBOOK))

    assert outcome.refusal_code is None
    assert outcome.chunks_written > 0
    assert len(statements(session, sa.Insert)) == 2


@pytest.mark.asyncio
async def test_a_shortcut_is_refused_without_touching_the_target() -> None:
    ingestor, session = build()
    outcome = await ingestor.ingest(Item(shortcut_target_id="file-2"), Fetched(HANDBOOK))

    assert outcome.refusal_code == IngestRefusal.SHORTCUT
    assert session.executed == []


@pytest.mark.asyncio
async def test_a_failed_conversion_gets_a_row_and_no_chunks() -> None:
    """Indexing the title alone would let an agent cite a document nobody can read."""
    ingestor, session = build([], [42], converter=failing_converter)
    outcome = await ingestor.ingest(
        Item(mime_type="application/pdf"), Fetched(b"%PDF-1.7", "application/pdf")
    )

    assert outcome.refusal_code == IngestRefusal.CONVERSION_FAILED
    assert outcome.chunks_written == 0
    written = statements(session, sa.Insert)
    assert len(written) == 1
    assert params(written[0])["conversion_status"] == "failed"


@pytest.mark.asyncio
async def test_a_document_that_is_all_credentials_is_stored_with_no_chunks() -> None:
    secrets = b"# Keys\n\n" + b"\n\n".join(
        f"The {name} value is api_key = sk-{'a' * 40}".encode() for name in ("first", "second")
    )
    ingestor, session = build([], [42])
    outcome = await ingestor.ingest(Item(), Fetched(secrets))

    assert outcome.refusal_code == IngestRefusal.ALL_CHUNKS_SCRUBBED
    assert outcome.chunks_written == 0
    assert ingestor.secrets_skipped > 0
    assert len(statements(session, sa.Insert)) == 1


@pytest.mark.asyncio
async def test_tombstoning_something_already_gone_is_false() -> None:
    ingestor, session = build([])
    assert await ingestor.tombstone("file-1") is False
    assert statements(session, sa.Delete) == []


@pytest.mark.asyncio
async def test_a_tombstone_takes_the_chunks_and_keeps_the_row() -> None:
    ingestor, session = build([42])
    assert await ingestor.tombstone("file-1", reason=TombstoneReason.UNSHARED) is True

    assert len(statements(session, sa.Delete)) == 1
    assert params(statements(session, sa.Update)[0])["tombstone_reason"] == TombstoneReason.UNSHARED


@pytest.mark.asyncio
async def test_the_write_is_scoped_to_one_source() -> None:
    """Two sources holding the same Drive id are two documents, not one."""
    ingestor, session = build([], [42])
    await ingestor.ingest(Item(), Fetched(HANDBOOK))

    assert params(statements(session, sa.Insert)[0])["source_id"] == SOURCE_ID
    assert "source_id" in str(statements(session, sa.Select)[0])
