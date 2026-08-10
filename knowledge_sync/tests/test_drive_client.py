"""What the sync asks Drive for: the root, the subtree, the changes, the bytes."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from agent_control_knowledge_sync import drive_transport as drive_transport_module
from agent_control_knowledge_sync.drive_client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    DriveError,
    DriveRefusalError,
    DriveRootUnreachableError,
    LocationUnknown,
    OutsideRoot,
    UnderRoot,
)
from agent_control_knowledge_sync.drive_transport import MAX_ATTEMPTS

from tests.drive_support import (
    PPTX_MIME,
    ROOT_ID,
    XLSX_MIME,
    Handler,
    _client,
    _file,
    _item,
    _root_folder,
    _tree_handler,
)


@pytest.fixture(autouse=True)
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff without the wait, patched on the transport, which is where the retry loop lives."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(drive_transport_module, "_sleep", fake_sleep)
    return waits


# --- the root -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_root_that_is_not_a_folder_is_refused() -> None:
    drive, _ = _client(
        lambda request: httpx.Response(200, json=_file(ROOT_ID, "notes.txt", "text/plain"))
    )
    with pytest.raises(DriveRootUnreachableError) as caught:
        await drive.resolve_root()
    assert "not a folder" in str(caught.value)


# --- the walk -----------------------------------------------------------------


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
    """A count alone cannot tell a ceiling from a corpus that happens to be that big."""
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
    shortcut = _file("sc1", "Board pack", SHORTCUT_MIME, shortcutDetails={"targetId": "target-1"})
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
    """A 5xx that outlives every retry says nothing about where the file lives."""
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
