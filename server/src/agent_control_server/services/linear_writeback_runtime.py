"""The write path's configuration, resolved once per process.

Split from :mod:`.linear_writeback` so that module holds only mechanics - the
escape, the marker, the client - while this one holds what a deployment's
settings make of them. The dependency points one way: this module imports the
mechanics, never the reverse.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import linear_settings
from .linear_writeback import (
    CompletedStateResolver,
    HttpLinearWritebackClient,
    LinearWritebackClient,
)
from .linear_writeback_files import (
    ATTACHMENTS_DISABLED_MESSAGE,
    AgentFile,
    AgentFileDelivery,
    push_agent_file,
)


@dataclass(frozen=True)
class WritebackRuntime:
    """What the write path needs from configuration, resolved once.

    ``client`` is ``None`` when no API key is configured, and the two flags
    gate comments and files apart. Key and flag are separate because a
    deployment with a key and the flag off must queue rows and send nothing.
    """

    client: LinearWritebackClient | None
    resolver: CompletedStateResolver | None
    write_enabled: bool
    attachments_write_enabled: bool = False

    @property
    def can_write(self) -> bool:
        return self.write_enabled and self.client is not None

    @property
    def can_write_attachments(self) -> bool:
        """Its own flag: accepting a comment is not accepting an upload."""
        return self.attachments_write_enabled and self.client is not None

    async def deliver_agent_file(
        self, *, issue_id: str, file: AgentFile
    ) -> AgentFileDelivery:
        """Push one agent-authored file, or name the flag that stopped it."""
        if self.client is None or not self.attachments_write_enabled:
            return AgentFileDelivery(
                file.title, file.asset_url, None, ATTACHMENTS_DISABLED_MESSAGE
            )
        return await push_agent_file(self.client, issue_id=issue_id, file=file)

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def build_writeback_runtime() -> WritebackRuntime:
    """Construct the write path from process settings.

    An absent API key yields no client rather than a failure, the same shape
    as every other Linear service here.
    """
    api_key = linear_settings.get_api_key()
    client = (
        HttpLinearWritebackClient(
            api_key=api_key,
            api_url=linear_settings.api_url,
            timeout_seconds=linear_settings.timeout_seconds,
        )
        if api_key is not None
        else None
    )
    return WritebackRuntime(
        client=client,
        resolver=CompletedStateResolver(client) if client is not None else None,
        write_enabled=bool(linear_settings.write_enabled),
        attachments_write_enabled=bool(linear_settings.attachments_write_enabled),
    )


_runtime: WritebackRuntime | None = None
_runtime_lock = threading.Lock()


def get_writeback_runtime() -> WritebackRuntime:
    """Process-wide write path, built on first use.

    FastAPI runs dependencies in worker threads, so two first requests can
    land here at once; the lock keeps the second from building a second HTTP
    client nothing would close. Tests override the dependency.
    """
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = build_writeback_runtime()
        return _runtime


async def shutdown_writeback_runtime() -> None:
    """Close the process-wide write path, if one was ever built."""
    global _runtime
    with _runtime_lock:
        runtime, _runtime = _runtime, None
    if runtime is not None:
        await runtime.aclose()
