"""The seam between Agent Control and the processes that run agents.

Everything above this module depends on :class:`ExecutorClient`, a small
protocol over "create, read and delete a conversation, and tell me you are
alive". Nothing above it knows that the executor is an HTTP service, which
wire format it speaks, or that Google ADK exists at all. That is the whole
point of the seam: a second executor, or an in-process runner, is a new
implementation of this protocol rather than a refactor.

The failure vocabulary is small and deliberate:

* :class:`ExecutorUnavailableError` - could not reach it, it timed out, or it
  answered 5xx. Retrying might work.
* :class:`ExecutorRejectedError` - it answered, and refused. Retrying will not
  help; something is misconfigured.
* :class:`ExecutorSessionNotFoundError` - it answered, and has never heard of
  this session. The local mapping row is an orphan, which is a state to render
  rather than an error to raise.

Every message on those errors is written here, by hand, as a module constant.
An executor runs arbitrary agent code, so its own error bodies can carry
tracebacks, tool exception text, model error responses echoing the prompt, and
internal paths. None of that may reach a browser, so none of it is ever
formatted into an exception message. This follows the standard
``services/linear_client.py`` already sets.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Error text. Written here, never lifted from an executor response.
# ---------------------------------------------------------------------------

EXECUTOR_DISABLED_MESSAGE = (
    "Agent chat is not enabled on this server. Set "
    "AGENT_CONTROL_EXECUTOR_ENABLED=true and bind the agent to an executor."
)
EXECUTOR_UNREACHABLE_MESSAGE = (
    "The executor that runs this agent could not be reached. The agent's "
    "process may be down, restarting, or unreachable from this server."
)
EXECUTOR_TIMEOUT_MESSAGE = (
    "The executor that runs this agent did not answer in time. The request "
    "was abandoned by this server; the executor may still be working."
)
EXECUTOR_UPSTREAM_FAILURE_MESSAGE = (
    "The executor that runs this agent reported an internal error."
)
EXECUTOR_REJECTED_MESSAGE = (
    "The executor that runs this agent refused the request. Check that the "
    "configured executor app name matches the app that process serves."
)
EXECUTOR_UNAUTHORIZED_MESSAGE = (
    "The executor that runs this agent rejected this server's credentials."
)
EXECUTOR_UNREADABLE_MESSAGE = (
    "The executor that runs this agent returned a response this server could "
    "not read."
)
EXECUTOR_SESSION_MISSING_MESSAGE = (
    "The executor no longer holds this conversation."
)
EXECUTOR_KIND_UNSUPPORTED_MESSAGE = (
    "This server has no client for the executor kind configured for this "
    "agent. Rebind the agent, or upgrade the server."
)
EXECUTOR_TURN_TIMEOUT_MESSAGE = (
    "The agent did not finish this turn within the configured time limit. "
    "This server stopped waiting, but the agent is still running: its work "
    "will appear in the transcript when it finishes."
)
"""Deliberately not ``EXECUTOR_TIMEOUT_MESSAGE``.

A session-CRUD timeout means a request was abandoned and probably achieved
nothing. A turn timeout means a model is still being called and money is still
being spent, and a person who reads "the request was abandoned" will conclude
the opposite. The two are different sentences because they call for different
actions."""

EXECUTOR_MODEL_UNAVAILABLE_MESSAGE = (
    "The executor could not call its model. Its model credentials are missing, "
    "rejected, or out of quota. This is executor configuration; retrying will "
    "not help."
)
"""A missing or quota-exhausted model key on the executor.

Reported as a refusal rather than as unavailability on purpose: the executor is
up and answering, and the thing that is wrong is configuration nobody fixes by
waiting. The text names the class of fault without quoting whatever the model
provider said, which routinely echoes the prompt back."""


EXECUTOR_PUBLIC_MESSAGES: tuple[str, ...] = (
    EXECUTOR_DISABLED_MESSAGE,
    EXECUTOR_UNREACHABLE_MESSAGE,
    EXECUTOR_TIMEOUT_MESSAGE,
    EXECUTOR_UPSTREAM_FAILURE_MESSAGE,
    EXECUTOR_REJECTED_MESSAGE,
    EXECUTOR_UNAUTHORIZED_MESSAGE,
    EXECUTOR_UNREADABLE_MESSAGE,
    EXECUTOR_SESSION_MISSING_MESSAGE,
    EXECUTOR_KIND_UNSUPPORTED_MESSAGE,
    EXECUTOR_TURN_TIMEOUT_MESSAGE,
    EXECUTOR_MODEL_UNAVAILABLE_MESSAGE,
)
"""Every message above, as one closed set.

The error sanitizer replaces 5xx detail text wholesale by default, which would
collapse "unreachable" and "did not answer in time, and your turn may still be
running" into one sentence. This tuple is what lets those specific strings
through: it is a list of literals, so it can vouch for them without vouching
for anything computed.

Adding a message here means asserting it contains nothing but text written in
this file. Never add a formatted string.
"""


class ExecutorError(Exception):
    """An executor call could not be completed.

    ``message`` is written for a client to display. It names what failed
    without quoting the executor's response and never carries a credential.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ExecutorUnavailableError(ExecutorError):
    """The executor could not be reached, timed out, or failed internally."""


class ExecutorRejectedError(ExecutorError):
    """The executor answered and refused the request."""


class ExecutorSessionNotFoundError(ExecutorError):
    """The executor has no such session.

    Distinct from the two above because it is not a failure to report. The
    local mapping row has outlived the conversation it points at, and the
    caller renders that as an empty transcript with a banner.
    """


class ExecutorTurnTimeoutError(ExecutorUnavailableError):
    """A turn outlived the time this server was willing to wait for it.

    A subclass of unavailable rather than a sibling, so any handler that only
    knows the three broad kinds still behaves sensibly. What it adds is the one
    fact that changes what a caller should do: **the invocation did not stop**.
    This server hung up; the executor is still calling models. Everything that
    treats a timeout as "nothing happened" is wrong here, which is why it gets
    its own type instead of a flag on a message.
    """


class ExecutorModelUnavailableError(ExecutorRejectedError):
    """The executor answered, and could not call its model.

    A missing, rejected or quota-exhausted model credential on the executor
    side. Rejection rather than unavailability because the executor is up and
    the fault is in its configuration.
    """


# ---------------------------------------------------------------------------
# Neutral shapes. No executor's vocabulary appears here.
# ---------------------------------------------------------------------------

PART_KIND_TEXT = "text"
PART_KIND_TOOL_CALL = "tool_call"
PART_KIND_TOOL_RESULT = "tool_result"
PART_KIND_UNSUPPORTED = "unsupported"

ROLE_USER = "user"
ROLE_AGENT = "agent"
ROLE_SYSTEM = "system"


@dataclass(frozen=True)
class ExecutorMessagePart:
    """One piece of a message: prose, a tool call, or a tool result.

    ``kind`` is one of the ``PART_KIND_*`` constants. A part an implementation
    cannot map keeps ``PART_KIND_UNSUPPORTED`` rather than being dropped: a
    transcript that silently omits what the model saw is worse than one that
    says "something was here".
    """

    kind: str
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutorMessage:
    """One message in a conversation, in executor order."""

    role: str
    author: str | None = None
    timestamp: dt.datetime | None = None
    parts: tuple[ExecutorMessagePart, ...] = ()


@dataclass(frozen=True)
class ExecutorSession:
    """A conversation as the executor holds it."""

    app_name: str
    user_id: str
    session_id: str
    messages: tuple[ExecutorMessage, ...] = ()
    state: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutorTurn:
    """What one completed turn produced.

    Only the new messages. The executor holds the whole conversation and the
    transcript route reads it back, so returning history here would mean two
    sources of truth for the same bytes disagreeing whenever a turn is retried.
    """

    messages: tuple[ExecutorMessage, ...] = ()


@runtime_checkable
class ExecutorClient(Protocol):
    """The narrow surface the rest of the server depends on.

    ``run_stream`` is still deliberately absent. It arrives with the streaming
    route that uses it, so no implementation has to carry a method nothing
    calls.
    """

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        state: Mapping[str, Any],
    ) -> ExecutorSession:
        """Create a conversation with server-chosen identifiers and seeded state.

        ``state`` is what the agent's own callbacks and tools read back, so it
        carries the session identity and, when runtime auth is configured, the
        session-bound token that authorizes machine-side writes.
        """
        ...

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> ExecutorSession:
        """Read a conversation and its messages.

        Raises :class:`ExecutorSessionNotFoundError` when the executor has no
        such session.
        """
        ...

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        """Delete a conversation. Deleting an absent one is not an error."""
        ...

    async def run(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        message: str,
        state_delta: Mapping[str, Any] | None = None,
        timeout_seconds: float,
    ) -> ExecutorTurn:
        """Say ``message`` to the agent and wait for the turn to finish.

        ``state_delta`` is merged into the executor's session state before the
        invocation runs. It is how per-turn facts - the trace id, today - reach
        the agent's own callbacks, and it is separate from the state seeded at
        session creation because it changes every turn.

        ``timeout_seconds`` is per call rather than per client: a turn is
        allowed minutes, and session CRUD is not, and one client serves both.

        Raises :class:`ExecutorTurnTimeoutError` when the limit passes, which
        means the invocation is still running. Raises
        :class:`ExecutorModelUnavailableError` when the executor could not call
        its model at all.
        """
        ...

    async def health(self) -> None:
        """Probe the executor. Raises :class:`ExecutorError` when it is unwell."""
        ...

    async def aclose(self) -> None:
        """Release any transport this client owns."""
        ...


class ExecutorClientFactory(Protocol):
    """Resolves a client for one agent's executor binding.

    Bindings are per-agent, so there is no single executor to hold a client
    for. Implementations are expected to share one connection pool across the
    clients they hand out rather than opening a pool per agent.
    """

    def client_for(self, *, executor_kind: str, base_url: str) -> ExecutorClient:
        """Return a client for this binding.

        Raises :class:`ExecutorUnavailableError` when no implementation matches
        ``executor_kind``.
        """
        ...

    async def aclose(self) -> None:
        """Release the shared transport."""
        ...
