"""Section 11: agent output enters the corpus through a human hand, never through the walk.

An agent writes a speculation into its deliverables tree, the sync indexes it,
and a week later a different agent cites it as company knowledge that nobody
agreed to. The refusal is ancestry, not equality, because the realistic accident
is one deliverables folder shared three levels down rather than the root itself.

Its limit is stated rather than hidden: the reader account can walk only as high
as its own visibility reaches, so a chain that truncates above the shared node is
only partially checked. `agent-drive.md` 4.4.1's outbound permission canary is
the enforced backstop for what this cannot see.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from .drive_transport import DriveTransport

__all__ = [
    "AGENT_OUTPUT_REFUSAL",
    "AgentOutputGuard",
    "DriveAncestry",
    "ParentLookup",
]

AGENT_OUTPUT_REFUSAL = "agent_output"

# Matches drive_client's own parent-walk ceiling: the same shape of loop needs
# the same bound, and a Drive tree deeper than this is not a corpus.
_WALK_LIMIT = 32

logger = logging.getLogger(__name__)


class ParentLookup(Protocol):
    """The parents of one Drive node, as far as the reader account can see them."""

    async def parents(self, node_id: str) -> Sequence[str]: ...


class DriveAncestry:
    """A lookup over the transport the rest of the sync already uses."""

    def __init__(self, transport: DriveTransport) -> None:
        self._transport = transport

    async def parents(self, node_id: str) -> Sequence[str]:
        """A node's parents; an unreadable node has none this account can name."""
        response = await self._transport.request(f"/files/{node_id}", {"fields": "id,parents"})
        if response.status_code != 200:
            return ()
        payload = response.json()
        if not isinstance(payload, dict):
            return ()
        return tuple(str(parent) for parent in payload.get("parents") or ())


class AgentOutputGuard:
    """Refuses any node whose visible ancestor chain reaches the executor's Drive root."""

    def __init__(self, executor_root_id: str | None, ancestry: ParentLookup | None = None) -> None:
        self._root_id = (executor_root_id or "").strip()
        self._ancestry = ancestry
        self._verdicts: dict[str, bool] = {}
        self.refused = 0

    @property
    def enabled(self) -> bool:
        """A guard with no root to compare against, or no way to walk, refuses nothing."""
        return bool(self._root_id) and self._ancestry is not None

    async def refuses(self, node_id: str) -> bool:
        """Whether this document or folder is agent output, counted and named when it is."""
        ancestry = self._ancestry
        if not self._root_id or ancestry is None or not node_id:
            return False
        if not await self._reaches_root(node_id, ancestry):
            return False
        self.refused += 1
        logger.warning(
            "agent-output ingest guard refused %s: its ancestry reaches executor Drive root %s",
            node_id,
            self._root_id,
        )
        return True

    async def _reaches_root(self, node_id: str, ancestry: ParentLookup) -> bool:
        """One parent walk, memoised, so ten thousand files are not ten thousand walks."""
        walked: list[str] = []
        current = node_id
        verdict = False
        for _ in range(_WALK_LIMIT):
            if current == self._root_id:
                verdict = True
                break
            if current in self._verdicts:
                verdict = self._verdicts[current]
                break
            walked.append(current)
            parents = await ancestry.parents(current)
            if not parents:
                break
            # One parent, the resolution drive_client uses for subtree filtering.
            current = str(parents[0])
        for seen in walked:
            self._verdicts[seen] = verdict
        return verdict
