"""The Drive read path, against stubbed transports, with no account and no network.

The flags are the point. A call missing supportsAllDrives on a Shared Drive returns
404 and a list missing includeItemsFromAllDrives returns zero rows, and both look
exactly like a folder nobody shared, so both are asserted rather than assumed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync import drive_transport as drive_transport_module
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials, DriveTokenProvider
from agent_control_knowledge_sync.drive_client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    DriveClient,
    DriveError,
    DriveItem,
    DriveRefusalError,
    DriveRootUnreachableError,
    LocationUnknown,
    OutsideRoot,
    UnderRoot,
)
from agent_control_knowledge_sync.drive_transport import MAX_ATTEMPTS

ROOT_ID = "0ABsharedDriveRoot"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

CREDS = DriveCredentials(
    client_id="123456789012-abcdefg.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff without the wait, and a record of what would have been waited.

    Patched on the transport, which is where the retry loop lives.
    """
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(drive_transport_module, "_sleep", fake_sleep)
    return waits


def _provider() -> tuple[DriveTokenProvider, list[str]]:
    issued: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        issued.append(f"at-{len(issued) + 1}")
        return httpx.Response(200, json={"access_token": issued[-1], "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DriveTokenProvider(credentials=CREDS, client=client), issued


def _config(**overrides: Any) -> SyncConfig:
    base: dict[str, Any] = {
        "credentials": CREDS,
        "root_folder_id": ROOT_ID,
        "database_url": "postgresql+psycopg://knowledge_sync@localhost/agent_knowledge",
    }
    base.update(overrides)
    return SyncConfig(**base)


def _client(handler: Handler, **overrides: Any) -> tuple[DriveClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recorded(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider, _ = _provider()
    transport = httpx.MockTransport(recorded)
    drive = DriveClient(provider, httpx.AsyncClient(transport=transport), _config(**overrides))
    return drive, seen


def _file(file_id: str, name: str, mime: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": "2026-08-01T09:30:00.000Z",
    }
    payload.update(extra)
    return payload


def _item(**overrides: Any) -> DriveItem:
    base: dict[str, Any] = {
        "id": "file-1",
        "name": "Q3 review.pptx",
        "mime_type": PPTX_MIME,
        "modified_time": datetime(2026, 8, 1, tzinfo=UTC),
        "size": 2048,
        "md5_checksum": "d41d8cd98f00b204e9800998ecf8427e",
        "trashed": False,
        "shortcut_target_id": None,
    }
    base.update(overrides)
    return DriveItem(**base)


def _root_folder() -> dict[str, Any]:
    return _file(ROOT_ID, "Company Knowledge", FOLDER_MIME)


# --- the root -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_root_resolves_and_the_call_carries_the_shared_drive_flag() -> None:
    drive, seen = _client(lambda request: httpx.Response(200, json=_root_folder()))
    root = await drive.resolve_root()
    assert root.id == ROOT_ID
    assert root.mime_type == FOLDER_MIME
    assert seen[0].url.params["supportsAllDrives"] == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_an_unreachable_root_refuses_loudly_and_names_the_flag(status: int) -> None:
    """A missing flag presents as a successful sync of zero documents. Not here."""
    drive, _ = _client(lambda request: httpx.Response(status, json={"error": {"code": status}}))
    with pytest.raises(DriveRootUnreachableError) as caught:
        await drive.resolve_root()
    message = str(caught.value)
    assert "supportsAllDrives" in message
    assert ROOT_ID in message


@pytest.mark.asyncio
async def test_a_root_that_is_not_a_folder_is_refused() -> None:
    drive, _ = _client(
        lambda request: httpx.Response(200, json=_file(ROOT_ID, "notes.txt", "text/plain"))
    )
    with pytest.raises(DriveRootUnreachableError) as caught:
        await drive.resolve_root()
    assert "not a folder" in str(caught.value)


# --- the walk -----------------------------------------------------------------


def _tree_handler(pages: dict[str, list[dict[str, Any]]]) -> Handler:
    """Serves a folder id to its children, one page each."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/drive/v3/files/{ROOT_ID}" and "q" not in request.url.params:
            return httpx.Response(200, json=_root_folder())
        if path == "/drive/v3/files":
            query = request.url.params["q"]
            folder_id = query.split("'")[1]
            return httpx.Response(200, json={"files": pages.get(folder_id, [])})
        return httpx.Response(404, json={"error": {"code": 404}})

    return handler


@pytest.mark.asyncio
async def test_every_list_call_carries_include_items_from_all_drives() -> None:
    """Without it a Shared Drive lists zero rows and reports success."""
    drive, seen = _client(_tree_handler({ROOT_ID: [_file("d1", "Plan.pdf", "application/pdf")]}))
    items = [item async for item in drive.walk_subtree()]
    listings = [request for request in seen if request.url.path == "/drive/v3/files"]
    assert [item.id for item in items] == ["d1"]
    assert listings, "the walk made no list call"
    for request in listings:
        assert request.url.params["includeItemsFromAllDrives"] == "true"
        assert request.url.params["supportsAllDrives"] == "true"


@pytest.mark.asyncio
async def test_the_walk_descends_folders_and_paginates() -> None:
    first_page = {
        "files": [_file("sub", "Decks", FOLDER_MIME), _file("d1", "A.pdf", "application/pdf")],
        "nextPageToken": "page-2",
    }
    second_page = {"files": [_file("d2", "B.pdf", "application/pdf")]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/drive/v3/files/{ROOT_ID}":
            return httpx.Response(200, json=_root_folder())
        folder_id = request.url.params["q"].split("'")[1]
        if folder_id == ROOT_ID:
            if request.url.params.get("pageToken") == "page-2":
                return httpx.Response(200, json=second_page)
            return httpx.Response(200, json=first_page)
        return httpx.Response(200, json={"files": [_file("d3", "C.pptx", PPTX_MIME)]})

    drive, _ = _client(handler)
    items = [item async for item in drive.walk_subtree()]
    assert sorted(item.id for item in items) == ["d1", "d2", "d3"]
    # The folder a document was found under is its citation, and the walk is
    # the only place that knows it.
    assert {item.id: item.folder_path for item in items} == {
        "d1": ("Company Knowledge",),
        "d2": ("Company Knowledge",),
        "d3": ("Company Knowledge", "Decks"),
    }


@pytest.mark.asyncio
async def test_the_walk_stops_at_the_run_ceiling() -> None:
    children = [_file(f"d{index}", f"{index}.pdf", "application/pdf") for index in range(5)]
    drive, _ = _client(_tree_handler({ROOT_ID: children}), max_documents_per_run=2)
    items = [item async for item in drive.walk_subtree()]
    assert len(items) == 2
    assert drive.walk_truncated is True


@pytest.mark.asyncio
async def test_a_corpus_of_exactly_the_ceiling_is_not_a_truncated_walk() -> None:
    """A count alone cannot tell a ceiling from a corpus that happens to be that big.

    Reported as truncated it would store no cursor, re-walk in full every run
    and read `partial`/`source_ceiling` forever, with nothing to fix.
    """
    children = [_file(f"d{index}", f"{index}.pdf", "application/pdf") for index in range(3)]
    drive, _ = _client(_tree_handler({ROOT_ID: children}), max_documents_per_run=3)
    items = [item async for item in drive.walk_subtree()]
    assert len(items) == 3
    assert drive.walk_truncated is False


@pytest.mark.asyncio
async def test_a_shortcut_resolves_to_its_target() -> None:
    shortcut = _file(
        "sc1", "Strategy (shortcut)", SHORTCUT_MIME, shortcutDetails={"targetId": "target-1"}
    )
    target = _file("target-1", "Strategy.docx", "application/pdf", size="1200")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/drive/v3/files/{ROOT_ID}":
            return httpx.Response(200, json=_root_folder())
        if request.url.path == "/drive/v3/files/target-1":
            return httpx.Response(200, json=target)
        return httpx.Response(200, json={"files": [shortcut]})

    drive, _ = _client(handler)
    items = [item async for item in drive.walk_subtree()]
    assert [item.id for item in items] == ["target-1"]
    assert items[0].size == 1200
    assert drive.refusals == []


@pytest.mark.asyncio
async def test_an_unreadable_shortcut_target_is_named_not_skipped() -> None:
    """Plan 5.7: a shortcut the loader cannot follow is reported, never a silent gap."""
    shortcut = _file(
        "sc1", "Board pack", SHORTCUT_MIME, shortcutDetails={"targetId": "target-1"}
    )
    sibling = _file("d1", "Notes.pdf", "application/pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/drive/v3/files/{ROOT_ID}":
            return httpx.Response(200, json=_root_folder())
        if request.url.path == "/drive/v3/files/target-1":
            return httpx.Response(404, json={"error": {"code": 404}})
        return httpx.Response(200, json={"files": [shortcut, sibling]})

    drive, _ = _client(handler)
    items = [item async for item in drive.walk_subtree()]
    assert [item.id for item in items] == ["d1"]
    assert [refusal.code for refusal in drive.refusals] == ["shortcut_unreadable"]
    assert "Board pack" in drive.refusals[0].detail


@pytest.mark.asyncio
async def test_a_shortcut_without_a_target_id_is_named() -> None:
    shortcut = _file("sc1", "Dangling", SHORTCUT_MIME)

    drive, _ = _client(_tree_handler({ROOT_ID: [shortcut]}))
    items = [item async for item in drive.walk_subtree()]
    assert items == []
    assert [refusal.code for refusal in drive.refusals] == ["shortcut_unresolved"]


@pytest.mark.asyncio
async def test_an_unreadable_child_folder_is_counted_and_the_walk_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/drive/v3/files/{ROOT_ID}":
            return httpx.Response(200, json=_root_folder())
        folder_id = request.url.params["q"].split("'")[1]
        if folder_id == ROOT_ID:
            return httpx.Response(
                200,
                json={
                    "files": [
                        _file("locked", "Legal", FOLDER_MIME),
                        _file("d1", "Handbook.pdf", "application/pdf"),
                    ]
                },
            )
        return httpx.Response(403, json={"error": {"code": 403}})

    drive, _ = _client(handler)
    items = [item async for item in drive.walk_subtree()]
    assert [item.id for item in items] == ["d1"]
    assert [refusal.code for refusal in drive.refusals] == ["unreadable_folder"]


# --- cursors and changes ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_start_cursor_comes_back_with_the_flag_set() -> None:
    drive, seen = _client(lambda request: httpx.Response(200, json={"startPageToken": "42"}))
    assert await drive.start_cursor() == "42"
    assert seen[0].url.params["supportsAllDrives"] == "true"


@pytest.mark.asyncio
async def test_changes_paginate_and_return_the_cursor_to_store_after_committing() -> None:
    pages = {
        "cursor-1": {
            "changes": [
                {"fileId": "d1", "removed": False, "file": _file("d1", "A.pdf", "application/pdf")}
            ],
            "nextPageToken": "cursor-2",
        },
        "cursor-2": {
            "changes": [
                {"fileId": "d2", "removed": True},
                {"changeType": "drive", "driveId": "0AB"},
            ],
            "newStartPageToken": "cursor-3",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params["pageToken"]])

    drive, seen = _client(handler)
    changes, cursor = await drive.list_changes("cursor-1")
    assert cursor == "cursor-3"
    assert [(change.file_id, change.removed) for change in changes] == [("d1", False), ("d2", True)]
    assert changes[0].item is not None
    assert changes[1].item is None
    for request in seen:
        assert request.url.params["includeItemsFromAllDrives"] == "true"
        assert request.url.params["supportsAllDrives"] == "true"


@pytest.mark.asyncio
async def test_a_changes_feed_with_no_new_cursor_is_an_error_not_a_lost_cursor() -> None:
    drive, _ = _client(lambda request: httpx.Response(200, json={"changes": []}))
    with pytest.raises(DriveError):
        await drive.list_changes("cursor-1")


def _parent_handler(tree: dict[str, dict[str, Any]]) -> Handler:
    """Answers `files.get` with a name, a type and a parent list, which is the whole walk."""

    def handler(request: httpx.Request) -> httpx.Response:
        file_id = request.url.path.rsplit("/", 1)[-1]
        node = tree.get(file_id, {})
        return httpx.Response(
            200,
            json={
                "id": file_id,
                "name": node.get("name", ""),
                "mimeType": node.get("mime", FOLDER_MIME),
                "parents": node.get("parents", []),
            },
        )

    return handler


@pytest.mark.asyncio
async def test_a_file_outside_the_root_has_no_path_under_it() -> None:
    tree = {"d1": {"name": "Stray.pdf", "parents": ["other"]}, "other": {"name": "Elsewhere"}}

    drive, _ = _client(_parent_handler(tree))
    assert await drive.resolve_folder_path("d1") == OutsideRoot()


@pytest.mark.asyncio
async def test_a_file_under_the_root_answers_with_the_whole_chain_above_it() -> None:
    """The changes feed carries no path, so this walk is the only citation there is."""
    tree = {
        ROOT_ID: {"name": "Company Knowledge"},
        "d1": {"name": "Laptops.pdf", "parents": ["sub"]},
        "sub": {"name": "Onboarding", "parents": [ROOT_ID]},
    }

    drive, _ = _client(_parent_handler(tree))
    assert await drive.resolve_folder_path("d1") == UnderRoot(("Company Knowledge", "Onboarding"))


@pytest.mark.asyncio
async def test_a_file_directly_under_the_root_still_names_the_root() -> None:
    """The fence header prints the path alone, so a citation starts at the corpus root."""
    tree = {
        ROOT_ID: {"name": "Company Knowledge"},
        "d1": {"name": "Laptops.pdf", "parents": [ROOT_ID]},
    }

    drive, _ = _client(_parent_handler(tree))
    assert await drive.resolve_folder_path("d1") == UnderRoot(("Company Knowledge",))


@pytest.mark.asyncio
async def test_a_parent_walk_drive_would_not_answer_is_unknown_rather_than_outside() -> None:
    """A 5xx that outlives every retry says nothing about where the file lives.

    Read as `outside` it tombstones a live document and deletes its chunks,
    and the run still reports `ok` because a tombstone is not a refusal.
    """
    drive, seen = _client(lambda request: httpx.Response(503, json={"error": {"code": 503}}))

    location = await drive.resolve_folder_path("d1")

    assert isinstance(location, LocationUnknown)
    assert location.code == "location_unknown"
    assert "503" in location.detail
    assert len(seen) == MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
async def test_no_parent_the_walk_cannot_read_is_mistaken_for_a_move(status: int) -> None:
    """Whatever stopped the walk, the file's location is unknown, not known to be elsewhere."""
    tree = {"d1": {"name": "Laptops.pdf", "parents": ["locked"]}}
    answer = _parent_handler(tree)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.rsplit("/", 1)[-1] == "locked":
            return httpx.Response(status, json={"error": {"code": status}})
        return answer(request)

    drive, _ = _client(handler)
    assert isinstance(await drive.resolve_folder_path("d1"), LocationUnknown)


@pytest.mark.asyncio
async def test_a_parent_chain_that_never_reaches_the_root_is_unknown_too() -> None:
    """A cycle spends the walk limit without proving anything either way."""
    tree = {
        "d1": {"name": "Loop.pdf", "parents": ["a"]},
        "a": {"name": "A", "parents": ["b"]},
        "b": {"name": "B", "parents": ["a"]},
    }

    drive, _ = _client(_parent_handler(tree))
    location = await drive.resolve_folder_path("d1")

    assert isinstance(location, LocationUnknown)
    assert location.code == "parent_walk_too_deep"


# --- content ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native_mime", "export_mime"),
    [
        ("application/vnd.google-apps.document", "text/markdown"),
        ("application/vnd.google-apps.spreadsheet", XLSX_MIME),
        ("application/vnd.google-apps.presentation", PPTX_MIME),
    ],
)
async def test_a_google_native_file_exports_rather_than_downloads(
    native_mime: str, export_mime: str
) -> None:
    drive, seen = _client(lambda request: httpx.Response(200, content=b"exported"))
    payload = await drive.fetch_content(_item(mime_type=native_mime, size=None))
    assert payload.data == b"exported"
    # The export mime travels with the bytes: the item's own type is a native
    # one no converter accepts, so reading it off the item refuses every Doc.
    assert payload.media_type == export_mime
    assert seen[0].url.path == "/drive/v3/files/file-1/export"
    assert seen[0].url.params["mimeType"] == export_mime
    assert seen[0].url.params["supportsAllDrives"] == "true"


@pytest.mark.asyncio
async def test_an_uploaded_file_downloads_with_alt_media() -> None:
    """The real corpus is uploaded pptx and pdf, so this is the hot path."""
    drive, seen = _client(lambda request: httpx.Response(200, content=b"%PDF-1.7"))
    payload = await drive.fetch_content(_item(mime_type="application/pdf"))
    assert (payload.data, payload.media_type) == (b"%PDF-1.7", "application/pdf")
    assert seen[0].url.params["alt"] == "media"
    assert seen[0].url.params["supportsAllDrives"] == "true"


@pytest.mark.asyncio
async def test_an_oversize_file_is_refused_before_a_byte_moves() -> None:
    drive, seen = _client(
        lambda request: httpx.Response(200, content=b"x" * 4096), max_file_bytes=1024
    )
    with pytest.raises(DriveRefusalError) as caught:
        await drive.fetch_content(_item(size=4096))
    assert caught.value.code == "oversize"
    assert seen == [], "the refusal must land before the download"


@pytest.mark.asyncio
async def test_a_body_over_the_ceiling_is_refused_too() -> None:
    """An export carries no size in metadata, so the ceiling has to hold on the body."""
    drive, _ = _client(
        lambda request: httpx.Response(200, content=b"x" * 4096), max_file_bytes=1024
    )
    with pytest.raises(DriveRefusalError) as caught:
        await drive.fetch_content(
            _item(mime_type="application/vnd.google-apps.document", size=None)
        )
    assert caught.value.code == "oversize"


@pytest.mark.asyncio
async def test_an_export_over_googles_own_ceiling_is_named() -> None:
    drive, _ = _client(
        lambda request: httpx.Response(
            403, json={"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}}
        )
    )
    with pytest.raises(DriveRefusalError) as caught:
        await drive.fetch_content(
            _item(mime_type="application/vnd.google-apps.document", size=None)
        )
    assert caught.value.code == "export_too_large"


@pytest.mark.asyncio
async def test_a_native_type_with_no_text_path_is_named() -> None:
    drive, seen = _client(lambda request: httpx.Response(200, content=b""))
    with pytest.raises(DriveRefusalError) as caught:
        await drive.fetch_content(_item(mime_type="application/vnd.google-apps.form", size=None))
    assert caught.value.code == "no_text_export"
    assert seen == []


@pytest.mark.asyncio
async def test_an_unreadable_file_is_a_refusal_not_a_crash() -> None:
    drive, _ = _client(lambda request: httpx.Response(404, json={"error": {"code": 404}}))
    with pytest.raises(DriveRefusalError) as caught:
        await drive.fetch_content(_item(mime_type="application/pdf"))
    assert caught.value.code == "unreadable"


@pytest.mark.asyncio
async def test_fetch_content_follows_a_shortcut_to_its_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"%PDF-1.7")
        return httpx.Response(200, json=_file("target-1", "Real.pdf", "application/pdf"))

    drive, seen = _client(handler)
    shortcut = _item(mime_type=SHORTCUT_MIME, shortcut_target_id="target-1", size=None)
    assert (await drive.fetch_content(shortcut)).data == b"%PDF-1.7"
    assert seen[-1].url.path == "/drive/v3/files/target-1"


# --- retries ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rate_limit_backs_off_for_as_long_as_google_asks(slept: list[float]) -> None:
    replies = [
        httpx.Response(429, headers={"Retry-After": "7"}, json={}),
        httpx.Response(200, json=_root_folder()),
    ]
    drive, seen = _client(lambda request: replies.pop(0))
    assert (await drive.resolve_root()).id == ROOT_ID
    assert slept == [7.0]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_a_server_error_is_retried_with_backoff(slept: list[float]) -> None:
    replies = [
        httpx.Response(503, json={}),
        httpx.Response(500, json={}),
        httpx.Response(200, json=_root_folder()),
    ]
    drive, _ = _client(lambda request: replies.pop(0))
    assert (await drive.resolve_root()).id == ROOT_ID
    assert slept == [0.5, 1.0]


@pytest.mark.asyncio
async def test_a_401_forgets_the_cached_token_once_and_retries() -> None:
    """A token can stop being valid before it stops being unexpired."""
    provider, issued = _provider()
    replies = [httpx.Response(401, json={}), httpx.Response(200, json=_root_folder())]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    drive = DriveClient(
        provider, httpx.AsyncClient(transport=httpx.MockTransport(handler)), _config()
    )
    assert (await drive.resolve_root()).id == ROOT_ID
    assert issued == ["at-1", "at-2"], "the second call must carry a freshly minted token"
    assert seen[0].headers["Authorization"] == "Bearer at-1"
    assert seen[1].headers["Authorization"] == "Bearer at-2"


@pytest.mark.asyncio
async def test_a_second_401_gives_up_rather_than_looping() -> None:
    drive, seen = _client(lambda request: httpx.Response(401, json={}))
    with pytest.raises(DriveError):
        await drive.resolve_root()
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_a_persistent_rate_limit_gives_up(slept: list[float]) -> None:
    drive, seen = _client(lambda request: httpx.Response(429, json={}))
    with pytest.raises(DriveError):
        await drive.resolve_root()
    assert len(seen) == 5
    assert len(slept) == 4


@pytest.mark.asyncio
async def test_an_unreachable_drive_is_an_error_not_a_hang(slept: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    drive, _ = _client(handler)
    with pytest.raises(DriveError) as caught:
        await drive.resolve_root()
    assert "unreachable" in str(caught.value)
    assert len(slept) == 4
