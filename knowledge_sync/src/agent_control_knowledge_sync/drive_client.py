"""What the sync asks Drive for: the root, the subtree, the changes, the bytes.

How those calls are made, with the shared-drive flags and the retries, is
``drive_transport.py``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import SyncConfig
from .drive_auth import DriveTokenProvider
from .drive_transport import DriveError, DriveTransport

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
NATIVE_MIME_PREFIX = "application/vnd.google-apps."

_MARKDOWN_MIME = "text/markdown"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

EXPORT_MIME_TYPES: dict[str, str] = {
    "application/vnd.google-apps.document": _MARKDOWN_MIME,
    "application/vnd.google-apps.spreadsheet": _XLSX_MIME,
    "application/vnd.google-apps.presentation": _PPTX_MIME,
}

_ITEM_FIELDS = "id,name,mimeType,modifiedTime,size,md5Checksum,trashed,shortcutDetails/targetId"
_LIST_FIELDS = f"nextPageToken,files({_ITEM_FIELDS})"
_CHANGE_FIELDS = f"nextPageToken,newStartPageToken,changes(fileId,removed,file({_ITEM_FIELDS}))"

_PAGE_SIZE = 200
_PARENT_WALK_LIMIT = 32

LOCATION_UNKNOWN = "location_unknown"
"""Drive would not answer the parent walk, so where the file lives is not known."""

PARENT_WALK_TOO_DEEP = "parent_walk_too_deep"
"""The chain ran past the walk limit without reaching the root, which answers nothing."""

_LOG = logging.getLogger(__name__)

_SHARED_DRIVE_HINT = (
    "Every call this client makes carries supportsAllDrives=true, so if that flag was "
    "removed the same 404 would mean a Shared Drive the request could not see. With the "
    "flag in place the remaining causes are: the folder is not shared with the reader "
    "account, or the id belongs to a different drive."
)


class DriveRefusalError(DriveError):
    """One item refused by name, so a run counts it instead of losing it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DriveRootUnreachableError(DriveError):
    """The corpus root did not resolve, which reads exactly like a successful empty sync."""

    code = "root_unreachable"


@dataclass(frozen=True, slots=True)
class DriveItem:
    """One Drive file as the sync needs it."""

    id: str
    name: str
    mime_type: str
    modified_time: datetime
    size: int | None
    md5_checksum: str | None
    trashed: bool
    shortcut_target_id: str | None
    folder_path: tuple[str, ...] = ()
    """The folders between the corpus root and this file, which the citation needs."""


@dataclass(frozen=True, slots=True)
class FetchedContent:
    """Bytes and the media type they actually are, which an export changes."""

    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class DriveChange:
    """One entry from the changes feed; `item` is absent when the file is gone."""

    file_id: str
    removed: bool
    item: DriveItem | None


@dataclass(frozen=True, slots=True)
class DriveRefusalRecord:
    """A per-item refusal the walk survives and the run summary reports."""

    file_id: str
    name: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class UnderRoot:
    """Confirmed: the parent chain was walked and it reaches the corpus root."""

    folders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OutsideRoot:
    """Confirmed: the chain was walked to its end and the corpus root is not on it."""


@dataclass(frozen=True, slots=True)
class LocationUnknown:
    """Drive would not say where the file lives. A refusal, never a tombstone."""

    code: str
    detail: str


FolderLocation = UnderRoot | OutsideRoot | LocationUnknown
"""A transport failure and a genuine absence are different answers, so they are different types."""


def _quote(value: str) -> str:
    """Escapes a value for a Drive `q` string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_time(raw: object) -> datetime:
    """RFC3339 into an aware datetime; a missing time reads as 1970, visibly wrong."""
    if not isinstance(raw, str) or not raw:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)


def _parse_item(payload: Mapping[str, Any]) -> DriveItem:
    """One `files` resource into a DriveItem."""
    size = payload.get("size")
    checksum = payload.get("md5Checksum")
    shortcut = payload.get("shortcutDetails") or {}
    target = shortcut.get("targetId")
    return DriveItem(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        mime_type=str(payload.get("mimeType") or ""),
        modified_time=_parse_time(payload.get("modifiedTime")),
        size=int(size) if size is not None else None,
        md5_checksum=str(checksum) if checksum is not None else None,
        trashed=bool(payload.get("trashed", False)),
        shortcut_target_id=str(target) if target else None,
    )


class DriveClient:
    """Reads the corpus subtree; every request carries supportsAllDrives=true."""

    def __init__(
        self, tokens: DriveTokenProvider, client: httpx.AsyncClient, config: SyncConfig
    ) -> None:
        self._transport = DriveTransport(tokens, client, config)
        self._config = config
        self._root: DriveItem | None = None
        self.refusals: list[DriveRefusalRecord] = []
        self.walk_truncated = False
        """Set only when the ceiling stopped a walk that still had documents to give."""

    async def resolve_root(self) -> DriveItem:
        """The corpus root, or a typed refusal naming the shared-drive cause.

        Held after the first success: its name is the first segment of every
        citation this client produces, and a run resolves it before anything
        else, so both the walk and the changes replay read it for free.
        """
        if self._root is not None:
            return self._root
        folder_id = self._config.root_folder_id
        response = await self._transport.request(f"/files/{folder_id}", {"fields": _ITEM_FIELDS})
        if response.status_code in (403, 404):
            raise DriveRootUnreachableError(
                f"Drive answered HTTP {response.status_code} for the corpus root "
                f"{folder_id}. {_SHARED_DRIVE_HINT} This refusal exists because the "
                "alternative is a run that indexes nothing and reports success."
            )
        if response.status_code != 200:
            raise DriveError(
                f"Drive answered HTTP {response.status_code} for the corpus root {folder_id}."
            )
        item = _parse_item(response.json())
        if item.mime_type != FOLDER_MIME:
            raise DriveRootUnreachableError(
                f"The corpus root {folder_id} is a {item.mime_type!r}, not a folder."
            )
        self._root = item
        return item

    async def start_cursor(self) -> str:
        """The changes-feed token to store before the first walk."""
        payload = await self._transport.json("/changes/startPageToken", {})
        token = payload.get("startPageToken")
        if not token:
            raise DriveError("Drive returned no startPageToken for the changes feed.")
        return str(token)

    async def walk_subtree(self) -> AsyncIterator[DriveItem]:
        """Every document under the root, breadth first, stopping at the run ceiling.

        The ceiling is tested against a document in hand, so a corpus holding
        exactly the ceiling ends on its own and is not reported as truncated.
        """
        self.walk_truncated = False
        root = await self.resolve_root()
        ceiling = self._config.max_documents_per_run
        pending: list[tuple[str, tuple[str, ...]]] = [(root.id, (root.name,))]
        seen = {root.id}
        yielded = 0
        while pending:
            folder_id, folders = pending.pop(0)
            async for child in self._list_children(folder_id):
                item = await self._follow_shortcut(child)
                if item is None or item.id in seen:
                    continue
                seen.add(item.id)
                if item.mime_type == FOLDER_MIME:
                    pending.append((item.id, (*folders, item.name)))
                    continue
                if yielded >= ceiling:
                    self.walk_truncated = True
                    return
                yield replace(item, folder_path=folders)
                yielded += 1

    async def list_changes(self, cursor: str) -> tuple[list[DriveChange], str]:
        """One drain of the changes feed and the cursor to store after committing."""
        changes: list[DriveChange] = []
        page_token = cursor
        while True:
            payload = await self._transport.json(
                "/changes",
                {
                    "pageToken": page_token,
                    "fields": _CHANGE_FIELDS,
                    "pageSize": str(_PAGE_SIZE),
                    "includeRemoved": "true",
                },
                list_call=True,
            )
            for entry in payload.get("changes") or []:
                file_id = str(entry.get("fileId") or "")
                if not file_id:
                    continue
                file_payload = entry.get("file")
                changes.append(
                    DriveChange(
                        file_id=file_id,
                        removed=bool(entry.get("removed")) or file_payload is None,
                        item=_parse_item(file_payload) if file_payload else None,
                    )
                )
            next_page = payload.get("nextPageToken")
            if next_page:
                page_token = str(next_page)
                continue
            new_cursor = payload.get("newStartPageToken")
            if not new_cursor:
                raise DriveError("The changes feed ended without a newStartPageToken.")
            return changes, str(new_cursor)

    async def fetch_content(self, item: DriveItem) -> FetchedContent:
        """Exports a Google-native file, downloads everything else, refuses the oversize.

        The media type travels with the bytes because an export replaces it: a
        Doc's own type is a Drive-native one no converter accepts, and reading
        it off the item would refuse every Doc in the corpus.
        """
        target = await self._shortcut_target(item)
        ceiling = self._config.max_file_bytes
        if target.size is not None and target.size > ceiling:
            raise DriveRefusalError(
                "oversize",
                f"{target.name!r} is {target.size} bytes, over the {ceiling}-byte ceiling; "
                "it was refused rather than downloaded.",
            )
        export_mime = EXPORT_MIME_TYPES.get(target.mime_type)
        if export_mime is not None:
            payload = await self._export(target, export_mime)
        elif target.mime_type.startswith(NATIVE_MIME_PREFIX):
            raise DriveRefusalError(
                "no_text_export",
                f"{target.name!r} is a {target.mime_type!r}, which this sync has no export for.",
            )
        else:
            payload = await self._download(target)
        if len(payload) > ceiling:
            raise DriveRefusalError(
                "oversize",
                f"{target.name!r} came back as {len(payload)} bytes, over the "
                f"{ceiling}-byte ceiling.",
            )
        return FetchedContent(payload, export_mime or target.mime_type)

    async def get_item(self, file_id: str) -> DriveItem:
        """One file's metadata, or a named refusal when the reader cannot see it."""
        response = await self._transport.request(f"/files/{file_id}", {"fields": _ITEM_FIELDS})
        if response.status_code in (403, 404):
            raise DriveRefusalError(
                "unreadable",
                f"Drive answered HTTP {response.status_code} for file {file_id}; the reader "
                "account cannot open it.",
            )
        if response.status_code != 200:
            raise DriveError(f"Drive answered HTTP {response.status_code} for file {file_id}.")
        return _parse_item(response.json())

    async def resolve_folder_path(self, file_id: str) -> FolderLocation:
        """Where a changed file sits: under the root, outside it, or not determinable.

        The changes feed carries no path, so this walk is the only citation a
        change has. The root's own name leads, because the fence header prints
        this path alone.
        """
        root_id = self._config.root_folder_id
        folders: list[str] = []
        current = file_id
        for _ in range(_PARENT_WALK_LIMIT):
            if current == root_id:
                return UnderRoot(((await self.resolve_root()).name, *reversed(folders)))
            fields = {"fields": "id,name,parents"}
            response = await self._transport.request(f"/files/{current}", fields)
            if response.status_code != 200:
                return LocationUnknown(
                    LOCATION_UNKNOWN,
                    f"Drive answered HTTP {response.status_code} walking the parents of "
                    f"{file_id} at {current}; where it lives is unknown, not outside.",
                )
            payload = response.json()
            if current != file_id:
                folders.append(str(payload.get("name") or ""))
            parents = payload.get("parents") or []
            if not parents:
                return OutsideRoot()
            current = str(parents[0])
        return LocationUnknown(
            PARENT_WALK_TOO_DEEP,
            f"The parents of {file_id} ran past {_PARENT_WALK_LIMIT} without reaching the root.",
        )

    async def _list_children(self, folder_id: str) -> AsyncIterator[DriveItem]:
        """One folder's children, paginated; an unreadable folder is a counted refusal."""
        page_token: str | None = None
        while True:
            params = {
                "q": f"'{_quote(folder_id)}' in parents and trashed = false",
                "fields": _LIST_FIELDS,
                "pageSize": str(_PAGE_SIZE),
                "orderBy": "folder,name",
            }
            if page_token:
                params["pageToken"] = page_token
            response = await self._transport.request("/files", params, list_call=True)
            if response.status_code in (403, 404):
                self._refuse(
                    DriveRefusalRecord(
                        file_id=folder_id,
                        name="",
                        code="unreadable_folder",
                        detail=(
                            f"Drive answered HTTP {response.status_code} listing folder "
                            f"{folder_id}; its contents are not in the corpus."
                        ),
                    )
                )
                return
            if response.status_code != 200:
                raise DriveError(
                    f"Drive answered HTTP {response.status_code} listing folder {folder_id}."
                )
            payload = response.json()
            for entry in payload.get("files") or []:
                yield _parse_item(entry)
            next_page = payload.get("nextPageToken")
            if not next_page:
                return
            page_token = str(next_page)

    async def _follow_shortcut(self, item: DriveItem) -> DriveItem | None:
        """Resolves a shortcut during the walk, recording a refusal rather than skipping."""
        if item.mime_type != SHORTCUT_MIME:
            return item
        try:
            return await self._shortcut_target(item)
        except DriveRefusalError as refusal:
            self._refuse(
                DriveRefusalRecord(
                    file_id=item.id, name=item.name, code=refusal.code, detail=str(refusal)
                )
            )
            return None

    def _refuse(self, record: DriveRefusalRecord) -> None:
        """Records a refusal the walk survives, and says so once."""
        self.refusals.append(record)
        _LOG.warning("refused item=%s code=%s: %s", record.file_id, record.code, record.detail)

    async def _shortcut_target(self, item: DriveItem) -> DriveItem:
        """A shortcut's target, or the item itself when it is not a shortcut."""
        if item.mime_type != SHORTCUT_MIME:
            return item
        target_id = item.shortcut_target_id
        if not target_id:
            raise DriveRefusalError(
                "shortcut_unresolved",
                f"Shortcut {item.name!r} ({item.id}) carries no shortcutDetails.targetId.",
            )
        try:
            return await self.get_item(target_id)
        except DriveRefusalError as refusal:
            raise DriveRefusalError(
                "shortcut_unreadable",
                f"Shortcut {item.name!r} ({item.id}) points at {target_id}, which the reader "
                f"account cannot open: {refusal}",
            ) from refusal

    async def _export(self, item: DriveItem, mime_type: str) -> bytes:
        """The Google-native path: no bytes to download, so ask for a conversion."""
        path = f"/files/{item.id}/export"
        response = await self._transport.request(path, {"mimeType": mime_type})
        if response.status_code == 403 and "exportSizeLimitExceeded" in response.text:
            raise DriveRefusalError(
                "export_too_large",
                f"Drive refused to export {item.name!r}: it is over Google's export ceiling.",
            )
        self._raise_for_item(response, item)
        return response.content

    async def _download(self, item: DriveItem) -> bytes:
        """The hot path: uploaded files come back as their own bytes."""
        response = await self._transport.request(f"/files/{item.id}", {"alt": "media"})
        self._raise_for_item(response, item)
        return response.content

    def _raise_for_item(self, response: httpx.Response, item: DriveItem) -> None:
        if response.status_code in (403, 404):
            raise DriveRefusalError(
                "unreadable",
                f"Drive answered HTTP {response.status_code} for {item.name!r} ({item.id}); "
                "the reader account cannot open it.",
            )
        if response.status_code != 200:
            raise DriveError(
                f"Drive answered HTTP {response.status_code} for {item.name!r} ({item.id})."
            )
