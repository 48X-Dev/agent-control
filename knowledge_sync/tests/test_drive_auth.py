"""The token loop, without a network and without a Google account."""

from __future__ import annotations

import httpx
import pytest
from agent_control_knowledge_sync.drive_auth import (
    DRIVE_READONLY_SCOPE,
    DriveAuthError,
    DriveCredentials,
    DriveTokenProvider,
)

CREDS = DriveCredentials(
    client_id="123456789012-abcdefghijklmnop.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)


def _provider(handler: object, **kwargs: object) -> DriveTokenProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return DriveTokenProvider(
        credentials=CREDS, client=httpx.AsyncClient(transport=transport), **kwargs
    )


def test_the_scope_is_readonly_and_says_so() -> None:
    """A widened scope is a code change somebody has to type here."""
    assert DRIVE_READONLY_SCOPE.endswith("/auth/drive.readonly")


@pytest.mark.asyncio
async def test_one_exchange_serves_every_call_inside_the_window() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})

    provider = _provider(handler)
    assert await provider.bearer_token(now=0.0) == "at-1"
    assert await provider.bearer_token(now=100.0) == "at-1"
    assert len(calls) == 1
    assert b"grant_type=refresh_token" in calls[0].content


@pytest.mark.asyncio
async def test_it_refreshes_before_expiry_not_after() -> None:
    """A token that dies mid-walk advances a cursor past unindexed rows."""
    issued = iter(["at-1", "at-2"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": next(issued), "expires_in": 3600})

    provider = _provider(handler)
    assert await provider.bearer_token(now=0.0) == "at-1"
    # Inside the window but within the skew: refreshed early, deliberately.
    assert await provider.bearer_token(now=3600.0 - 30.0) == "at-2"


@pytest.mark.asyncio
async def test_a_refusal_names_the_cause_and_never_the_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
                "echoed_request": CREDS.refresh_token,
            },
        )

    provider = _provider(handler)
    with pytest.raises(DriveAuthError) as caught:
        await provider.bearer_token(now=0.0)
    message = str(caught.value)
    assert "invalid_grant" in message
    assert "Testing" in message, "the seven-day trap must be in the message"
    assert CREDS.refresh_token not in message
    assert CREDS.client_secret not in message


@pytest.mark.asyncio
async def test_forget_is_the_way_out_of_an_unexpired_invalid_token() -> None:
    issued = iter(["at-1", "at-2"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": next(issued), "expires_in": 3600})

    provider = _provider(handler)
    assert await provider.bearer_token(now=0.0) == "at-1"
    provider.forget()
    assert await provider.bearer_token(now=1.0) == "at-2"


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_is_an_error_not_a_hang() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    provider = _provider(handler)
    with pytest.raises(DriveAuthError) as caught:
        await provider.bearer_token(now=0.0)
    assert "token endpoint" in str(caught.value)


def test_redacted_carries_neither_secret_nor_token() -> None:
    shown = CREDS.redacted()
    assert CREDS.refresh_token not in shown
    assert CREDS.client_secret not in shown
    assert "chars" in shown
