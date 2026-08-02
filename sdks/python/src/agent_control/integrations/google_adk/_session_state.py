"""Reading a session's identity out of ADK state.

Agent Control seeds this at session creation and refreshes it with every turn.
It is the one channel that carries which session a callback is running in and
what credential it may write back with, and it is read through public ADK
surface - ``CallbackContext.state`` and ``ToolContext.state`` - rather than by
reaching into an invocation context.

Nothing here ever raises. State that cannot be read is state that is absent,
and absent means no claim is made: guidance not arriving is a degradation,
while a model call failing because a state read threw is an outage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The server seeds a nested block at session creation and a second one with
# every turn; both shapes are read, and the dotted spelling is read too,
# because ADK state is a flat map that tolerates dotted keys and neither
# spelling is worth a coin toss on the model path.
SESSION_STATE_KEY = "agent_control"
TURN_STATE_KEY = "agent_control_turn"

_SESSION_KEY_FIELD = "session_key"
_TOKEN_FIELD = "runtime_token"
_TRACE_FIELD = "trace_id"


def _read_block(adk_state: Any, key: str) -> dict[str, Any]:
    """Read one seeded block out of ADK session state, never raising.

    State that cannot be read is state that is absent, and absent means no
    claim happens. Guidance not arriving is a degradation; a model call failing
    because a state read threw is an outage.
    """
    if adk_state is None:
        return {}
    block = _state_get(adk_state, key)
    if isinstance(block, dict):
        return block

    # Dotted fallback: a flat map with ``agent_control.session_key`` in it.
    flat: dict[str, Any] = {}
    for field_name in (_SESSION_KEY_FIELD, _TOKEN_FIELD, _TRACE_FIELD):
        value = _state_get(adk_state, f"{key}.{field_name}")
        if value is not None:
            flat[field_name] = value
    return flat


def _state_get(adk_state: Any, key: str) -> Any:
    try:
        getter = getattr(adk_state, "get", None)
        if callable(getter):
            return getter(key)
        return adk_state[key]
    except Exception:
        logger.debug("Could not read Google ADK session state", exc_info=True)
        return None


@dataclass(frozen=True)
class SessionIdentity:
    """Which session this callback is running in, and what it may write.

    ``token`` is preferred from the per-turn block. A token seeded at session
    creation is bound to the runtime TTL - five minutes by default - while an
    ADK session lives for hours, and it cannot renew itself. The server mints a
    fresh one with every turn for exactly this reason, so the newer block wins.
    """

    session_key: str
    token: str | None
    trace_id: str | None

    @classmethod
    def read(cls, adk_state: Any) -> SessionIdentity | None:
        session_block = _read_block(adk_state, SESSION_STATE_KEY)
        turn_block = _read_block(adk_state, TURN_STATE_KEY)

        session_key = session_block.get(_SESSION_KEY_FIELD) or turn_block.get(
            _SESSION_KEY_FIELD
        )
        if not isinstance(session_key, str) or not session_key:
            return None

        token = turn_block.get(_TOKEN_FIELD) or session_block.get(_TOKEN_FIELD)
        trace_id = turn_block.get(_TRACE_FIELD) or session_block.get(_TRACE_FIELD)
        return cls(
            session_key=session_key,
            token=token if isinstance(token, str) and token else None,
            trace_id=trace_id if isinstance(trace_id, str) and trace_id else None,
        )
