"""Section 11's guard: ancestry rather than equality, memoised, and its stated limit."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials, DriveTokenProvider
from agent_control_knowledge_sync.drive_client import DriveClient
from agent_control_knowledge_sync.drive_transport import DriveTransport
from agent_control_knowledge_sync.ingest import Ingestor
from agent_control_knowledge_sync.ingest_guard import (
    AGENT_OUTPUT_REFUSAL,
    AgentOutputGuard,
    DriveAncestry,
)
from agent_control_knowledge_sync.journal import SyncJournal
from agent_control_knowledge_sync.lease import hold_lease, mint_token
from agent_control_knowledge_sync.sync import corpus_sessions, run_once_with

from tests.conftest import query
from tests.fakes.drive import SHORTCUT_MIME, FakeDrive, FakeFile

EXECUTOR_ROOT = "executor-root-1"

# Company Knowledge/Ops Handbook/laptops.md, and the agent tree three levels
# down from its own root, which is the share an operator actually makes.
TREE = {
    "laptops.md": ("ops-handbook",),
    "ops-handbook": ("corpus-root",),
    "corpus-root": (),
    "deliverable.md": ("2026-08",),
    "2026-08": ("researcher",),
    "researcher": (EXECUTOR_ROOT,),
    EXECUTOR_ROOT: (),
}


class FakeAncestry:
    """One node's parents, logging every lookup so the walking can be counted."""

    def __init__(self, parents: dict[str, tuple[str, ...]] | None = None) -> None:
        self._parents = TREE if parents is None else parents
        self.lookups: list[str] = []

    async def parents(self, node_id: str) -> tuple[str, ...]:
        self.lookups.append(node_id)
        return self._parents.get(node_id, ())


def guard(**overrides: object) -> tuple[AgentOutputGuard, FakeAncestry]:
    ancestry = FakeAncestry()
    root = overrides.get("root", EXECUTOR_ROOT)
    return AgentOutputGuard(root if isinstance(root, str) else None, ancestry), ancestry


pytestmark = pytest.mark.asyncio


async def test_an_unset_executor_root_disables_the_guard() -> None:
    """Half on is the state section 12 makes name itself; it is not a refusal."""
    disabled, ancestry = guard(root=None)

    assert disabled.enabled is False
    assert await disabled.refuses("deliverable.md") is False
    assert ancestry.lookups == []


async def test_whitespace_is_the_same_as_unset() -> None:
    assert AgentOutputGuard("   ", FakeAncestry()).enabled is False


async def test_a_guard_with_no_way_to_walk_is_disabled() -> None:
    assert AgentOutputGuard(EXECUTOR_ROOT, None).enabled is False


async def test_the_executor_root_itself_is_refused_without_a_single_call() -> None:
    checked, ancestry = guard()

    assert await checked.refuses(EXECUTOR_ROOT) is True
    assert ancestry.lookups == []


async def test_a_folder_three_levels_inside_the_agent_tree_is_refused() -> None:
    """Equality on the root id misses this, which is the share people actually make."""
    checked, _ = guard()

    assert await checked.refuses("deliverable.md") is True
    assert checked.refused == 1


async def test_a_workspace_document_is_not_refused() -> None:
    checked, _ = guard()

    assert await checked.refuses("laptops.md") is False
    assert checked.refused == 0


async def test_a_chain_that_truncates_above_what_the_account_sees_is_allowed() -> None:
    """The stated limit: the reader walks only as high as its own visibility reaches."""
    checked = AgentOutputGuard(EXECUTOR_ROOT, FakeAncestry({"orphan.md": ()}))

    assert await checked.refuses("orphan.md") is False


async def test_two_documents_in_one_folder_cost_one_walk() -> None:
    checked, ancestry = guard()
    await checked.refuses("laptops.md")
    ancestry.lookups.clear()

    await checked.refuses("laptops.md")

    assert ancestry.lookups == []


async def test_a_verdict_is_remembered_for_every_ancestor_it_was_walked_through() -> None:
    checked, ancestry = guard()
    await checked.refuses("deliverable.md")
    ancestry.lookups.clear()

    assert await checked.refuses("2026-08") is True
    assert ancestry.lookups == []


async def test_a_parent_cycle_stops_at_the_walk_limit() -> None:
    """A malformed tree must not hang the sync; it answers no and moves on."""
    checked = AgentOutputGuard(EXECUTOR_ROOT, FakeAncestry({"a": ("b",), "b": ("a",)}))

    assert await checked.refuses("a") is False


async def test_an_empty_id_is_not_walked() -> None:
    checked, ancestry = guard()

    assert await checked.refuses("") is False
    assert ancestry.lookups == []


async def test_a_refusal_names_the_document_and_the_root_it_reached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    checked, _ = guard()

    with caplog.at_level(logging.WARNING):
        await checked.refuses("deliverable.md")

    assert "deliverable.md" in caplog.text
    assert EXECUTOR_ROOT in caplog.text


# --- the adapter over the transport the rest of the sync uses ----------------


class FakeTokens:
    async def bearer_token(self) -> str:
        return "at-1"

    def forget(self) -> None:
        return None


def _transport(handler: object) -> DriveTransport:
    config = SyncConfig(
        credentials=DriveCredentials(client_id="i", client_secret="s", refresh_token="r"),
        root_folder_id="corpus-root",
        database_url="postgresql+psycopg://knowledge_sync:x@localhost/agent_knowledge",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return DriveTransport(FakeTokens(), client, config)  # type: ignore[arg-type]


async def test_the_adapter_asks_drive_for_parents_with_the_shared_drive_flag() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "researcher", "parents": [EXECUTOR_ROOT]})

    parents = await DriveAncestry(_transport(handle)).parents("researcher")

    assert parents == (EXECUTOR_ROOT,)
    assert seen[0].url.params["supportsAllDrives"] == "true"
    assert seen[0].url.params["fields"] == "id,parents"


async def test_a_node_the_reader_cannot_open_has_no_parents_it_can_name() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 404, "message": "File not found."}})

    assert await DriveAncestry(_transport(handle)).parents("hidden") == ()


# --- the whole thing, over a real corpus and real Drive JSON -----------------

CORPUS_ROOT = "corpus-root-1"

HANDBOOK = """# Onboarding

## Laptops

Laptops are reimbursed up to 1500 GBP. Submit the receipt within thirty days of
purchase and the finance team pays it in the next run. A replacement machine is
approved by the hiring manager, never by the agent, and the asset register is
updated by IT the same week the machine arrives on somebody's desk.
"""


def _corpus_config(corpus: Any) -> SyncConfig:
    return SyncConfig(
        credentials=DriveCredentials(client_id="i", client_secret="s", refresh_token="r"),
        root_folder_id=CORPUS_ROOT,
        database_url=corpus.sync_url,
        max_documents_per_run=50,
        request_timeout_seconds=5.0,
        executor_drive_root_id=EXECUTOR_ROOT,
    )


async def test_a_shortcut_into_the_agent_tree_is_refused_by_a_real_run(
    drive: FakeDrive, corpus: Any
) -> None:
    """The accident section 11 names: one deliverables folder, three levels down."""
    config = _corpus_config(corpus)
    drive.folder(CORPUS_ROOT, "Company Knowledge")
    drive.markdown("file-laptops", "laptops.md", HANDBOOK, CORPUS_ROOT)
    drive.folder(EXECUTOR_ROOT, "Agent output")
    drive.folder("researcher-out", "researcher", EXECUTOR_ROOT)
    drive.folder("august", "2026-08", "researcher-out")
    drive.markdown("file-speculation", "competitors.md", HANDBOOK, "august")
    drive.add(
        FakeFile(
            id="shortcut-1",
            name="competitors.md",
            parents=(CORPUS_ROOT,),
            mime_type=SHORTCUT_MIME,
            shortcut_target_id="file-speculation",
        )
    )

    async with corpus_sessions(config) as sessions, httpx.AsyncClient() as http:
        tokens = DriveTokenProvider(config.credentials, http)
        checked = AgentOutputGuard(
            config.executor_drive_root_id, DriveAncestry(DriveTransport(tokens, http, config))
        )
        async with hold_lease(sessions, holder=mint_token()) as lease:
            counters = await run_once_with(
                config,
                client=DriveClient(tokens, http, config),
                journal=SyncJournal(sessions),
                lease=lease,
                ingestor_factory=lambda source_id: Ingestor(sessions, source_id, guard=checked),
            )

    assert counters.refusals_by_code == {AGENT_OUTPUT_REFUSAL: 1}
    assert checked.refused == 1
    indexed = query(corpus, "SELECT title FROM documents WHERE tombstoned_at IS NULL")
    assert [row["title"] for row in indexed] == ["laptops.md"]
