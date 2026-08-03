"""Chat sessions between a human and an agent, and their wire models.

Agent Control does not own a conversation. The executor does: it holds the
events, the model calls and the tool results. What Agent Control owns is the
*identity* of a session - which namespace it belongs to, which agent it talks
to, which team it was opened under, and which executor coordinates it maps to.
That mapping table is the only boundary between one namespace's transcripts and
another's, because the executor's own session store has no namespace concept.

Two consequences run through every model below.

The executor coordinates never appear in a response. A browser sees
``session_key`` and nothing else; ``executor_app_name``, ``executor_user_id``
and ``executor_session_id`` are minted server-side, are not accepted on any
request, and are not serialized. Requests declare ``extra="forbid"`` so a
client cannot smuggle one in.

Message content is a separate sensitivity class from session metadata, and the
two are read through different operations (``agent_sessions.read`` and
``agent_sessions.content_read``). Nothing that carries model output, tool
results or human prompts appears on a summary.
"""

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
    """Lifecycle of the local mapping row.

    ``orphaned`` and ``orphaned_pending_delete`` describe disagreement with the
    executor rather than anything a human did. A session whose executor-side
    state has vanished is ``orphaned``: it reads as an empty transcript with a
    banner, not as an error. A session whose local row was deleted but whose
    executor-side delete failed is ``orphaned_pending_delete``: the row is kept
    precisely so the delete can be retried, because a mapping that is silently
    dropped leaves an executor session nothing can ever address again.
    """

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
    """What one piece of a message is.

    ``UNSUPPORTED`` is deliberate. Executors emit part types this schema does
    not model (inline binary data, for one), and dropping them would render a
    transcript that quietly disagrees with what the model saw. A placeholder
    part says "something was here" without inventing a shape for it.
    """

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    UNSUPPORTED = "unsupported"


class SessionMessagePart(BaseModel):
    """One piece of a message.

    A single model turn routinely mixes prose with a tool call, so a message is
    a list of these rather than a string.
    """

    kind: SessionMessagePartKind = Field(..., description="What this part is.")
    text: str | None = Field(
        default=None, description="Verbatim text, for text parts."
    )
    tool_name: str | None = Field(
        default=None, description="Tool invoked or responded to."
    )
    tool_call_id: str | None = Field(
        default=None,
        description=(
            "Identifier linking a tool result back to its call, when the "
            "executor supplies one."
        ),
    )
    arguments: JSONObject | None = Field(
        default=None, description="Arguments the tool was called with."
    )
    result: JSONObject | None = Field(
        default=None, description="What the tool returned."
    )


class SessionMessage(BaseModel):
    """One message in a transcript.

    ``index`` is this server's own dense, 0-based position within the
    transcript as read, and is what ``after_index`` pages on. It is not an
    executor identifier and is not stable across a transcript that the executor
    rewrites.
    """

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
    """Detail view of one session.

    Adds only live turn state. There is deliberately nothing here that a summary
    does not have beyond that: the executor coordinates stay server-side, and the
    transcript is a separate, differently-authorized read.

    The two live-turn fields are **not** synonyms and clear on different events,
    which is the single most confusable thing in this schema:

    * ``in_flight_since`` is the *lock*. While it is set, a second turn on this
      session is refused. It clears whenever this server stops waiting, which
      includes the cases where the executor is still working.
    * ``in_flight_trace_id`` is the *liveness marker*. It clears only when a turn
      genuinely ended. A turn that timed out at the server, or whose client hung
      up, leaves this set precisely because the invocation is still burning
      tokens and a human may well want to do something about it.

    So ``in_flight_since IS NULL`` with ``in_flight_trace_id`` set is a real and
    expected state: "you may start another turn, and the previous one has not
    finished".
    """

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
    """Open a chat session with one agent.

    Carries no executor fields, by design and by construction: the executor
    triple is minted server-side, and ``extra="forbid"`` rejects a body that
    tries to supply one. A client that could choose its own executor
    coordinates could point a row in its own namespace at another namespace's
    conversation.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: AgentName = Field(
        ..., description="Agent to talk to. Must already be registered in this namespace."
    )
    title: SessionTitle | None = Field(
        default=None, description="Optional human-set title."
    )
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
    """The session that was opened.

    Note what is absent: the executor coordinates, and the session-bound
    runtime token. The token is seeded into the executor's own session state
    and never travels back through this response, because a caller holding it
    could write to the session as if it were the agent.
    """

    session: AgentSessionDetail = Field(..., description="The created session.")


class ListAgentSessionsResponse(BaseModel):
    """Paginated list of sessions."""

    sessions: list[AgentSessionSummary] = Field(default_factory=list)
    pagination: PaginationInfo = Field(
        ..., description="Cursor-based pagination metadata."
    )


class GetAgentSessionResponse(BaseModel):
    """Detail view of one session."""

    session: AgentSessionDetail = Field(..., description="The requested session.")


class PatchAgentSessionRequest(BaseModel):
    """Update the mutable fields of a session.

    Omitted fields are left alone, so an explicit ``null`` is the only way to
    clear a title or unset a team.
    """

    model_config = ConfigDict(extra="forbid")

    title: SessionTitle | None = Field(
        default=None, description="New title, or null to clear it."
    )
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
    """Result of deleting a session.

    ``deleted`` is only true when both sides are gone. A failed executor-side
    delete is never reported as success; it leaves the row in
    ``orphaned_pending_delete`` and surfaces as an error so the caller knows to
    retry.
    """

    deleted: bool = Field(..., description="Whether both sides of the session were removed.")


class ListSessionMessagesResponse(BaseModel):
    """One page of a transcript.

    An orphaned session is not an error here. The executor lost the
    conversation, which is worth saying plainly and once, next to an empty
    transcript, rather than turning a chat panel into an error page.
    """

    session_key: str = Field(..., description="Session the transcript belongs to.")
    status: AgentSessionStatus = Field(..., description="Lifecycle of the session.")
    messages: list[SessionMessage] = Field(default_factory=list)
    next_index: int | None = Field(
        default=None,
        description=(
            "Pass as ``after_index`` to read the next page; null when the page "
            "is the last."
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
    """Say something to the agent and wait for it to finish answering.

    Two fields, and the second one is consistent with why there was only ever
    one. Anything that *steers* the agent belongs in a control or in a nudge,
    both of which the control engine evaluates; a per-turn override here would
    be an unevaluated instruction channel opened by the cheapest possible
    route. An attachment key is not that. It names content this server already
    stored, typed and evaluated, it carries no free text, and the bytes are
    resolved server-side from a row the caller had to be authorized to create.
    A caller cannot supply an inline file here, and naming a key they do not
    own is a 404 rather than a delivery.
    """

    model_config = ConfigDict(extra="forbid")

    message: Annotated[
        str,
        StringConstraints(min_length=1, max_length=TURN_MESSAGE_MAX_LENGTH),
    ] = Field(..., description="What to say to the agent, as a user turn.")

    attachment_keys: Annotated[
        list[AttachmentKey], Field(max_length=ATTACHMENT_MAX_PER_TURN)
    ] = Field(
        default_factory=list,
        description=(
            "Attachments already stored on this session to carry with this "
            "turn. Each must be 'ready'; anything else is refused rather than "
            "quietly dropped, because a turn that ran without its file is the "
            "half-done job this whole path exists to prevent."
        ),
    )


class TurnResponse(BaseModel):
    """The result of one completed turn.

    ``messages`` holds what this turn produced and nothing that came before it,
    so its indexes are **turn-relative**: message 0 is the first message of this
    turn, not of the conversation. ``GET /agent-sessions/{key}/messages`` is the
    authoritative transcript and the only place indexes are absolute. Rendering
    this list is a convenience that saves a round trip; reconciling against the
    transcript is what makes a panel correct.

    A turn the guardrails blocked is a *completed* turn, not a failure. The
    plugin substitutes a blocked response, the executor finishes the turn
    normally, and the block appears in ``messages`` as ordinary model output.
    There is no field distinguishing the two, because the executor does not
    distinguish them either and inventing a flag here would mean guessing.
    """

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
    completed_at: dt.datetime = Field(
        ..., description="When the executor finished answering."
    )
    duration_seconds: float = Field(
        ..., ge=0, description="Wall-clock seconds the turn took."
    )
    messages: list[SessionMessage] = Field(
        default_factory=list,
        description=(
            "Messages this turn produced, indexed from 0 within the turn. Not "
            "transcript positions."
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
    """Whether the executors behind this namespace's agents are answering.

    ``/health`` deliberately checks nothing but the process itself. This is the
    probe for the dependency that chat adds, so the first symptom of an
    executor outage is a dashboard rather than a person hitting send.
    """

    enabled: bool = Field(
        ..., description="Whether the executor integration is switched on at all."
    )
    healthy: bool = Field(
        ...,
        description="True when every enabled binding answered. False when any did not.",
    )
    executors: list[ExecutorHealthEntry] = Field(default_factory=list)
    checked_at: dt.datetime = Field(..., description="When the probes ran.")
