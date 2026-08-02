"""Builds executor clients and owns the one connection pool they share.

Bindings are per-agent, so there is no single executor and no single client to
hold. What there *is* is one outbound connection pool, with explicit limits, so
a namespace with forty agents does not open forty pools and an unreachable
executor cannot accumulate sockets. ``HttpLinearClient`` sets no limits at all;
this one does, because the fan-out here is per-agent rather than per-service.

The process-wide instance is built on first use and closed by the FastAPI
lifespan, mirroring ``services/linear_milestones.py``. A server that never
opens a chat session never opens an HTTP client.
"""

from __future__ import annotations

import threading

import httpx

from ..config import ExecutorSettings, executor_settings
from .adk_executor_client import AdkExecutorClient
from .executor_client import (
    EXECUTOR_KIND_UNSUPPORTED_MESSAGE,
    ExecutorClient,
    ExecutorUnavailableError,
)

EXECUTOR_KIND_GOOGLE_ADK = "google_adk"


class HttpExecutorClientFactory:
    """Hands out per-binding clients over one shared transport."""

    def __init__(self, *, settings: ExecutorSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds),
            limits=httpx.Limits(
                max_connections=settings.max_connections,
                max_keepalive_connections=settings.max_keepalive_connections,
            ),
        )

    def client_for(self, *, executor_kind: str, base_url: str) -> ExecutorClient:
        """Return a client for one binding.

        An unknown kind means the row was written by a version of this server
        that knows an executor this one does not. That is a deployment fault,
        not a caller fault, so it surfaces as the executor being unavailable
        rather than as a validation error on a request that is perfectly valid.
        """
        if executor_kind != EXECUTOR_KIND_GOOGLE_ADK:
            raise ExecutorUnavailableError(EXECUTOR_KIND_UNSUPPORTED_MESSAGE)
        return AdkExecutorClient(
            base_url=base_url,
            client=self._client,
            shared_secret=self._settings.get_shared_secret(),
            shared_secret_header=self._settings.shared_secret_header,
        )

    async def aclose(self) -> None:
        """Close the shared transport."""
        await self._client.aclose()


_factory: HttpExecutorClientFactory | None = None
_factory_lock = threading.Lock()


def build_executor_client_factory(
    settings: ExecutorSettings | None = None,
) -> HttpExecutorClientFactory:
    """Construct a factory from the process settings."""
    return HttpExecutorClientFactory(settings=settings or executor_settings)


def get_executor_client_factory() -> HttpExecutorClientFactory:
    """FastAPI dependency returning the process-wide factory.

    FastAPI runs a sync dependency in a worker thread, so two first requests
    can land here at once; the lock keeps the second from building a second
    connection pool that nothing would ever close.
    """
    global _factory
    with _factory_lock:
        if _factory is None:
            _factory = build_executor_client_factory()
        return _factory


async def shutdown_executor_clients() -> None:
    """Close the process-wide factory, if one was ever built."""
    global _factory
    with _factory_lock:
        factory, _factory = _factory, None
    if factory is not None:
        await factory.aclose()
