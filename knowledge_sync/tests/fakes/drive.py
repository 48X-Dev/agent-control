"""A Drive that answers over httpx, in the JSON shapes the real API returns.

Stubbed at the transport, and unforgiving: an unparseable query answers 400, never [].
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_HOST = "www.googleapis.com"
FILES_PATH = "/drive/v3/files"
CHANGES_PATH = "/drive/v3/changes"
START_TOKEN_PATH = "/drive/v3/changes/startPageToken"

FOLDER_MIME = "application/vnd.google-apps.folder"
DOCUMENT_MIME = "application/vnd.google-apps.document"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
MARKDOWN_MIME = "text/markdown"

_PARENT_IN_Q = re.compile(r"'([^']+)'\s+in\s+parents")
_CHANGE_TIME = "2026-08-01T09:30:00.000Z"


@dataclass
class FakeFile:
    """One Drive item, native or uploaded."""

    id: str
    name: str
    parents: tuple[str, ...] = ()
    mime_type: str = MARKDOWN_MIME
    modified_time: str = "2026-08-01T09:00:00.000Z"
    content: bytes = b""
    exports: dict[str, bytes] = field(default_factory=dict)
    trashed: bool = False
    shortcut_target_id: str | None = None

    @property
    def native(self) -> bool:
        return self.mime_type.startswith("application/vnd.google-apps.")

    def metadata(self) -> dict[str, Any]:
        """The field set `files.get` returns, minus what the sync never reads."""
        row: dict[str, Any] = {
            "kind": "drive#file",
            "id": self.id,
            "name": self.name,
            "mimeType": self.mime_type,
            "modifiedTime": self.modified_time,
            "trashed": self.trashed,
            "parents": list(self.parents),
        }
        if not self.native:
            row["size"] = str(len(self.content))
            row["md5Checksum"] = hashlib.md5(self.content).hexdigest()
        if self.shortcut_target_id is not None:
            row["shortcutDetails"] = {
                "targetId": self.shortcut_target_id,
                "targetMimeType": MARKDOWN_MIME,
            }
        return row


@dataclass
class ChangeBatch:
    """What one `changes.list` call answers for one page token."""

    changes: list[dict[str, Any]]
    new_start_page_token: str


class FakeDrive:
    """An in-memory corpus plus the request log the flag assertions read."""

    def __init__(self, *, start_page_token: str = "t1") -> None:
        self.files: dict[str, FakeFile] = {}
        self.start_page_token = start_page_token
        self.batches: dict[str, ChangeBatch] = {}
        self.media_failures: dict[str, int] = {}
        self.unreachable: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.access_tokens = 0

    # --- building the corpus ------------------------------------------------

    def add(self, item: FakeFile) -> FakeFile:
        self.files[item.id] = item
        return item

    def folder(self, file_id: str, name: str, *parents: str) -> FakeFile:
        return self.add(FakeFile(id=file_id, name=name, parents=parents, mime_type=FOLDER_MIME))

    def markdown(self, file_id: str, name: str, body: str, *parents: str) -> FakeFile:
        return self.add(
            FakeFile(id=file_id, name=name, parents=parents, content=body.encode("utf-8"))
        )

    def set_changes(
        self,
        page_token: str,
        *,
        changed: tuple[str, ...] = (),
        removed: tuple[str, ...] = (),
        new_token: str | None = None,
    ) -> None:
        """Register one page of the changes feed against the token that asks for it."""
        entries = [self._change(file_id) for file_id in changed]
        entries += [self._removal(file_id) for file_id in removed]
        self.batches[page_token] = ChangeBatch(entries, new_token or page_token)

    def _change(self, file_id: str) -> dict[str, Any]:
        return {
            "kind": "drive#change",
            "changeType": "file",
            "time": _CHANGE_TIME,
            "removed": False,
            "fileId": file_id,
            "file": self.files[file_id].metadata(),
        }

    def _removal(self, file_id: str) -> dict[str, Any]:
        return {
            "kind": "drive#change",
            "changeType": "file",
            "time": _CHANGE_TIME,
            "removed": True,
            "fileId": file_id,
        }

    # --- what the tests assert against --------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def drive_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == DRIVE_HOST]

    def list_requests(self) -> list[httpx.Request]:
        """The two endpoints that return collections, and only those."""
        return [
            r for r in self.drive_requests() if r.url.path.rstrip("/") in {FILES_PATH, CHANGES_PATH}
        ]

    # --- the wire -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).startswith(TOKEN_URL):
            self.access_tokens += 1
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})

        path = request.url.path.rstrip("/")
        if path == START_TOKEN_PATH:
            return httpx.Response(
                200,
                json={"kind": "drive#startPageToken", "startPageToken": self.start_page_token},
            )
        if path == CHANGES_PATH:
            return self._changes(request)
        if path == FILES_PATH:
            return self._list(request)
        if path.startswith(f"{FILES_PATH}/"):
            return self._file(request, path.removeprefix(f"{FILES_PATH}/"))
        return _not_found(path)

    def _changes(self, request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken", "")
        batch = self.batches.get(token, ChangeBatch([], token))
        return httpx.Response(
            200,
            json={
                "kind": "drive#changeList",
                "changes": batch.changes,
                "newStartPageToken": batch.new_start_page_token,
            },
        )

    def _list(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        match = _PARENT_IN_Q.search(query)
        if match is None:
            unparsed = f"this fake understands only \"'<id>' in parents\"; got {query!r}"
            return httpx.Response(400, json={"error": {"code": 400, "message": unparsed}})
        parent = match.group(1)
        if parent in self.unreachable:
            return _not_found(parent)
        drop_trashed = "trashed" in query
        children = [
            item.metadata()
            for item in self.files.values()
            if parent in item.parents and not (drop_trashed and item.trashed)
        ]
        return httpx.Response(
            200,
            json={"kind": "drive#fileList", "incompleteSearch": False, "files": children},
        )

    def _file(self, request: httpx.Request, tail: str) -> httpx.Response:
        file_id, _, verb = tail.partition("/")
        if file_id in self.unreachable or file_id not in self.files:
            return _not_found(file_id)
        item = self.files[file_id]
        if verb == "export":
            wanted = request.url.params.get("mimeType", "")
            if wanted not in item.exports:
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "code": 403,
                            "message": f"Export only supports Docs Editors files; asked {wanted!r}",
                        }
                    },
                )
            return httpx.Response(200, content=item.exports[wanted])
        if request.url.params.get("alt") == "media":
            failure = self.media_failures.get(file_id)
            if failure is not None:
                return httpx.Response(
                    failure, json={"error": {"code": failure, "message": "Backend Error"}}
                )
            return httpx.Response(200, content=item.content)
        return httpx.Response(
            200, content=json.dumps(item.metadata()), headers={"content-type": "application/json"}
        )


def _not_found(what: str) -> httpx.Response:
    """Drive's own 404 body, which is what an unshared folder returns too."""
    return httpx.Response(
        404,
        json={
            "error": {
                "code": 404,
                "message": f"File not found: {what}.",
                "errors": [
                    {
                        "domain": "global",
                        "reason": "notFound",
                        "message": f"File not found: {what}.",
                    }
                ],
            }
        },
    )
