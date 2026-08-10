"""Constants, fakes and builders the two Drive test modules share."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials, DriveTokenProvider
from agent_control_knowledge_sync.drive_client import FOLDER_MIME, DriveClient, DriveItem

ROOT_ID = "0ABsharedDriveRoot"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

CREDS = DriveCredentials(
    client_id="123456789012-abcdefg.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)

Handler = Callable[[httpx.Request], httpx.Response]


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
