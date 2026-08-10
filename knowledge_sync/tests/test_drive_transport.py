"""How the sync talks to Drive: the shared-drive flags, the retries, the one token refresh."""

from __future__ import annotations

import httpx
import pytest
from agent_control_knowledge_sync import drive_transport as drive_transport_module
from agent_control_knowledge_sync.drive_client import (
    FOLDER_MIME,
    DriveClient,
    DriveError,
    DriveRootUnreachableError,
)

from tests.drive_support import (
    ROOT_ID,
    _client,
    _config,
    _file,
    _provider,
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


# --- the flags, and the 404 they otherwise hide -------------------------------


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
