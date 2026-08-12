"""Chat sessions between a human and an agent, and their wire models."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from .agent import JSONObject
from .agent_runtimes import AgentName, ExecutorKind
from .attachments import ATTACHMENT_MAX_PER_TURN, AttachmentKey
from .base import BaseModel
from .server import PaginationInfo
from .teams import TeamSlug

SESSION_KEY_LENGTH = 32
"""``uuid4().hex``. The only session identifier a client ever sees."""

SESSION_TITLE_MAX_LENGTH = 255

MESSAGE_PAGE_DEFAULT_LIMIT = 200
MESSAGE_PAGE_MAX_LIMIT = 500
"""Transcript reads are capped. A conversation grows without bound and a
browser rendering ten thousand messages is a hang, not a feature."""

TURN_MESSAGE_MAX_LENGTH = 16000
"""Ceiling on one turn's user text.

Not a safety control - the model sees whatever is under it either way - but a
cost and denial-of-service ceiling. Every character here is billed twice, once
on the way in and again on every subsequent turn that carries the history, so
an unbounded field is an unbounded bill reachable by any authenticated caller.
Roughly four thousand tokens, which is far more than a person types and far
less than a pasted corpus."""

SessionKey = Annotated[
    str,
    StringConstraints(
        min_length=SESSION_KEY_LENGTH,
        max_length=SESSION_KEY_LENGTH,
        pattern=r"^[0-9a-f]{32}$",
    ),
]

SessionTitle = Annotated[
    str,
    StringConstraints(min_length=1, max_length=SESSION_TITLE_MAX_LENGTH),
]


class AgentSessionStatus(StrEnum):
    """Lifecycle of the local mapping row."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    ORPHANED = "orphaned"
    ORPHANED_PENDING_DELETE = "orphaned_pending_delete"


SETTABLE_SESSION_STATUSES: frozenset[AgentSessionStatus] = frozenset(
    {AgentSessionStatus.ACTIVE, AgentSessionStatus.ARCHIVED}
)
"""The statuses a caller may set. The orphaned pair is set by the server when
it observes the executor disagreeing, and claiming one by hand would assert
something the server has not checked."""


class SessionMessageRole(StrEnum):
    """Who produced a message."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class SessionMessagePartKind(StrEnum):
    """What one piece of a message is."""

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    UNSUPPORTED = "unsupported"


class SessionMessagePart(BaseModel):
    """One piece of a message."""

    kind: SessionMessagePartKind = Field(..., description="What this part is.")
    text: str | None = Field(default=None, description="Verbatim text, for text parts.")
    tool_name: str | None = Field(default=None, description="Tool invoked or responded to.")
    tool_call_id: str | None = Field(
        default=None,
        description=(
            "Identifier linking a tool result back to its call, when the executor supplies one."
        ),
    )
    arguments: JSONObject | None = Field(
        default=None, description="Arguments the tool was called with."
    )
    result: JSONObject | None = Field(default=None, description="What the tool returned.")


class SessionMessage(BaseModel):
    """One message in a transcript."""

    index: int = Field(..., ge=0, description="0-based position in the transcript.")
    role: SessionMessageRole = Field(..., description="Who produced the message.")
    author: str | None = Field(
        default=None,
        description="Executor-reported author, usually the agent name or 'user'.",
    )
    timestamp: dt.datetime | None = Field(
        default=None, description="When the executor recorded the message."
    )
    parts: list[SessionMessagePart] = Field(
        default_factory=list, description="Ordered pieces of the message."
    )


class AgentSessionSummary(BaseModel):
    """List view of a session. Metadata only: no message content."""

    session_key: str = Field(
        ..., description="Stable identifier for the session; the only id a client sees."
    )
    namespace_key: str = Field(..., description="Namespace the session belongs to.")
    agent_name: str = Field(..., description="Agent this session talks to.")
    team_slug: str | None = Field(
        default=None,
        description=(
            "Team the session was opened under, or null. Deleting the team "
            "clears this and leaves the session intact."
        ),
    )
    title: str | None = Field(default=None, description="Human-set title, if any.")
    status: AgentSessionStatus = Field(..., description="Lifecycle of the session.")
    executor_kind: ExecutorKind = Field(
        ..., description="Which executor implementation serves this session."
    )
    last_trace_id: str | None = Field(
        default=None,
        description=(
            "Trace of the most recent turn that ended, whether it answered or "
            "failed. A turn this server stopped waiting for does not set it, "
            "because it has not ended."
        ),
    )
    last_activity_at: dt.datetime = Field(
        ..., description="When the session was last created, run or modified."
    )
    created_at: dt.datetime = Field(..., description="When the session was created.")
    updated_at: dt.datetime = Field(..., description="When the row last changed.")


class AgentSessionDetail(AgentSessionSummary):
    """Detail view of one session."""

    in_flight_since: dt.datetime | None = Field(
        default=None,
        description=(
            "Set while this server is waiting on a turn. A session with a turn "
            "in flight refuses a second one. Cleared when the server stops "
            "waiting, which is not necessarily when the turn ends."
        ),
    )
    in_flight_trace_id: str | None = Field(
        default=None,
        description=(
            "Trace of an invocation the executor may still be running. Cleared "
            "only when a turn genuinely ended, so it can outlive "
            "in_flight_since after a timeout or a client that hung up."
        ),
    )


# =============================================================================
# Requests / responses
# =============================================================================


class CreateAgentSessionRequest(BaseModel):
    """Open a chat session with one agent."""

    model_config = ConfigDict(extra="forbid")

    agent_name: AgentName = Field(
        ..., description="Agent to talk to. Must already be registered in this namespace."
    )
    title: SessionTitle | None = Field(default=None, description="Optional human-set title.")
    team_slug: TeamSlug | None = Field(
        default=None,
        description=(
            "Team to open the session under. Must be a team in this namespace; "
            "the session survives that team being deleted."
        ),
    )
    task_key: str | None = Field(
        default=None,
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
        description=(
            "Bind this session to one step of one dispatch task. It is what "
            "lets the turn path tell a fleet turn from a human chat turn, "
            "which is how a namespace budget, a dispatch pause and an executor "
            "kill switch can be refusals on the turn itself rather than checks "
            "inside the process being budgeted. It also opens the session to "
            "oversight: a task's session has no human owner, so anyone holding "
            "agent_tasks.read in the namespace may read, halt and nudge it."
        ),
    )


class CreateAgentSessionResponse(BaseModel):
    """The session that was opened."""

    session: AgentSessionDetail = Field(..., description="The created session.")


class ListAgentSessionsResponse(BaseModel):
    """Paginated list of sessions."""

    sessions: list[AgentSessionSummary] = Field(default_factory=list)
    pagination: PaginationInfo = Field(..., description="Cursor-based pagination metadata.")


class GetAgentSessionResponse(BaseModel):
    """Detail view of one session."""

    session: AgentSessionDetail = Field(..., description="The requested session.")


class PatchAgentSessionRequest(BaseModel):
    """Update the mutable fields of a session."""

    model_config = ConfigDict(extra="forbid")

    title: SessionTitle | None = Field(default=None, description="New title, or null to clear it.")
    team_slug: TeamSlug | None = Field(
        default=None, description="New team, or null to detach the session from its team."
    )
    status: AgentSessionStatus | None = Field(
        default=None,
        description=(
            "New status. Only 'active' and 'archived' may be set; the orphaned "
            "statuses are observations the server makes, not assertions a "
            "client can make."
        ),
    )

    @field_validator("status")
    @classmethod
    def reject_server_owned_status(
        cls, value: AgentSessionStatus | None
    ) -> AgentSessionStatus | None:
        if value is not None and value not in SETTABLE_SESSION_STATUSES:
            allowed = ", ".join(sorted(s.value for s in SETTABLE_SESSION_STATUSES))
            raise ValueError(f"status must be one of: {allowed}")
        return value


class PatchAgentSessionResponse(BaseModel):
    """The session after the update."""

    session: AgentSessionDetail = Field(..., description="The updated session.")


class DeleteAgentSessionResponse(BaseModel):
    """Result of deleting a session."""

    deleted: bool = Field(..., description="Whether both sides of the session were removed.")


class ListSessionMessagesResponse(BaseModel):
    """One page of a transcript."""

    session_key: str = Field(..., description="Session the transcript belongs to.")
    status: AgentSessionStatus = Field(..., description="Lifecycle of the session.")
    messages: list[SessionMessage] = Field(default_factory=list)
    next_index: int | None = Field(
        default=None,
        description=(
            "Pass as ``after_index`` to read the next page; null when the page is the last."
        ),
    )
    has_more: bool = Field(..., description="Whether more messages follow this page.")
    total: int = Field(..., ge=0, description="Messages the transcript holds in total.")
    notice: str | None = Field(
        default=None,
        description=(
            "Set when the transcript could not be read as-is, e.g. the "
            "executor no longer holds this session. Rendered as a banner "
            "above an empty transcript rather than as an error."
        ),
    )


class StartTurnRequest(BaseModel):
    """Say something to the agent and wait for it to finish answering."""

    model_config = ConfigDict(extra="forbid")

    message: Annotated[
        str,
        StringConstraints(min_length=1, max_length=TURN_MESSAGE_MAX_LENGTH),
    ] = Field(..., description="What to say to the agent, as a user turn.")

    attachment_keys: Annotated[list[AttachmentKey], Field(max_length=ATTACHMENT_MAX_PER_TURN)] = (
        Field(
            default_factory=list,
            description=(
                "Attachments already stored on this session to carry with this "
                "turn. Each must be 'ready'; anything else is refused rather than "
                "quietly dropped, because a turn that ran without its file is the "
                "half-done job this whole path exists to prevent."
            ),
        )
    )


class TurnResponse(BaseModel):
    """The result of one completed turn."""

    session_key: str = Field(..., description="Session the turn ran in.")
    trace_id: str = Field(
        ...,
        description=(
            "Trace minted for this turn. Recorded on the session as "
            "last_trace_id. Whether the agent's own guardrail decisions carry "
            "this same trace depends on the executor picking it up from the "
            "state seeded with the turn, which is unverified; treat it as the "
            "server's identifier for the turn until that is confirmed."
        ),
    )
    started_at: dt.datetime = Field(..., description="When this server began the turn.")
    completed_at: dt.datetime = Field(..., description="When the executor finished answering.")
    duration_seconds: float = Field(..., ge=0, description="Wall-clock seconds the turn took.")
    messages: list[SessionMessage] = Field(
        default_factory=list,
        description=(
            "Messages this turn produced, indexed from 0 within the turn. Not transcript positions."
        ),
    )


class ExecutorHealthEntry(BaseModel):
    """Reachability of one executor binding."""

    agent_name: str = Field(..., description="Agent the binding serves.")
    executor_kind: ExecutorKind = Field(..., description="Executor implementation.")
    enabled: bool = Field(..., description="Whether the binding accepts new sessions.")
    reachable: bool = Field(..., description="Whether the executor answered a probe.")
    error: str | None = Field(
        default=None,
        description=(
            "Short, server-authored reason the probe failed. Never quotes the "
            "executor's own response body."
        ),
    )


class ExecutorHealthResponse(BaseModel):
    """Whether the executors behind this namespace's agents are answering."""

    enabled: bool = Field(
        ..., description="Whether the executor integration is switched on at all."
    )
    healthy: bool = Field(
        ...,
        description="True when every enabled binding answered. False when any did not.",
    )
    executors: list[ExecutorHealthEntry] = Field(default_factory=list)
    checked_at: dt.datetime = Field(..., description="When the probes ran.")


# The tracker's own comment ceiling, applied at the boundary so an oversized
# body is a 422 naming the field rather than a silent truncation downstream.
TRACKER_COMMENT_MAX_LENGTH = 4000


class SaveTrackerCommentRequest(BaseModel):
    """Text an agent was told to save onto the issue its task came from.

    There is no issue field and there will not be one. The server resolves the
    target from the session, so an agent cannot name a ticket, and text that
    reaches the model from a fetched page cannot redirect the write. The reach
    of this whole route is one comment on one issue the session was already
    working on.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        ...,
        min_length=1,
        max_length=TRACKER_COMMENT_MAX_LENGTH,
        description=(
            "What to post. Sanitized and fenced server-side before it is sent, "
            "and truncated at the tracker's own limit."
        ),
    )


class SaveTrackerCommentResponse(BaseModel):
    """What reached the tracker, so the agent can say so rather than guess."""

    model_config = ConfigDict(extra="forbid")

    issue_ref: str = Field(..., description="The issue the comment was posted to.")
    issue_url: str | None = Field(
        default=None, description="Its tracker URL, when the task recorded one."
    )
    comment_id: str = Field(..., description="The tracker's id for the created comment.")
