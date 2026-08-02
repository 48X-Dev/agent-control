"""
Global state management for Agent Control SDK.

This module holds global state in a container object to avoid circular imports
between __init__.py and other modules. Both modules can import and modify
the same state object.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .runtime_auth import RuntimeTokenCache

if TYPE_CHECKING:
    from agent_control_models import Agent

    from .agent_config import AgentConfigSnapshot
    from .client import AgentControlClient


class _StateContainer:
    """Container for global SDK state."""

    def __init__(self) -> None:
        self.current_agent: Agent | None = None
        self.control_engine: Any = None
        self.client: AgentControlClient | None = None
        self.server_controls: list[dict[str, Any]] | None = None
        self.server_url: str | None = None
        self.api_key: str | None = None
        self.api_key_header: str | None = None
        self.runtime_token_cache = RuntimeTokenCache()
        # Optional target context fixed at init() time; both fields are set
        # together or both remain None.
        self.target_type: str | None = None
        self.target_id: str | None = None
        # Server-managed runtime configuration: the system prompt and the model,
        # fetched together on the refresh loop. ``None`` until the first
        # successful fetch, which is also the state a process stays in when the
        # control plane was unreachable at start - a control-plane outage must
        # not become an agent outage, so the agent runs what its code declares.
        self.agent_config: AgentConfigSnapshot | None = None
        # How long a managed *model* may survive without a successful refresh
        # before the SDK drops it and restores the code-declared one. The prompt
        # is deliberately not subject to this: stale text is a behaviour issue
        # and the fallback is a working agent, whereas an indefinitely retained
        # managed model is unbounded spend the control plane cannot revoke,
        # because the process that would pick up a clear is the one that cannot
        # reach the server.
        self.model_max_staleness_seconds: float | None = None
        self.on_config_change_callbacks: list[Callable[[AgentConfigSnapshot], None]] = []


# Singleton state instance
state = _StateContainer()
