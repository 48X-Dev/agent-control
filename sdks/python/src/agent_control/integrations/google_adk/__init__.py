"""Google ADK integration for Agent Control."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .plugin import AgentControlPlugin

# Only the plugin is re-exported here, because only the plugin needs the lazy
# import: it fails at import time without google-adk installed. The progress
# tools import no ADK surface at module level, so they are imported from
# ``agent_control.integrations.google_adk.progress_tools`` directly, with no
# machinery in between.
__all__ = ["AgentControlPlugin"]


def __getattr__(name: str) -> type:
    """Lazy import to avoid import errors when google-adk is not installed."""
    if name == "AgentControlPlugin":
        from .plugin import AgentControlPlugin

        return AgentControlPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
