"""Write-back to Linear: the one tier-1-shaped action this design permits.

Plan section 5.6. Two mutations exist in this module and nowhere else in the
server: ``commentCreate`` and ``issueUpdate``. Both sit behind
``AGENT_CONTROL_LINEAR_WRITE_ENABLED``, default false, because the first
deployment should be able to read and reason without gaining the ability to
edit anybody's tracker.

Same conventions as :mod:`.linear_client`: the API key is passed to the
constructor, held in one attribute and written into exactly one request
header; error text raised from here is written by hand rather than lifted from
the upstream response.

The escape in :func:`sanitize_agent_text` is rule 1 of the five mitigations,
and it is an escape rather than a strip because the fence is not containment:
an injected agent that emits a closing fence followed by an image embed
escapes the fenced block, Linear renders the image, and every viewer plus
every Slack unfurl performs an outbound GET carrying whatever the agent
encoded in the path. E8 proves this rule against the live renderer, and
write-back does not ship until it passes.

``resolve_completed_state`` lives here rather than in :mod:`.linear_issues`
because that module's own docstring promises it only reads and contains no
code path that could become a write. Every Linear call the accept path makes
is in this one file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from agent_control_models.tasks import WRITEBACK_BODY_MAX_LENGTH

from ..config import linear_settings
from .linear_client import LinearError

_logger = logging.getLogger(__name__)

WRITEBACK_STEP_NAME = "dispatch.writeback"
"""The tool-step name the body is evaluated under before it is posted. An
explicit step, so a control written against the write-back matches nothing
else and nothing else matches it."""

_COMPLETED_STATE_TTL_SECONDS = 300.0
"""How long one team's resolved completed-state id is reused. Workflow states
move on the timescale of team process changes, not requests."""

_MARKER_COMMENT_PAGE = 100
"""Comments read per dedupe check. Beyond this the check degrades to posting,
whose residual is a duplicate comment - the mildest failure in the plan."""

_TRUNCATION_NOTICE = "\n[output truncated by agent control]"

_BACKTICK_RUN = re.compile(r"`{3,}")
_BARE_URL = re.compile(r"https?://[^\s`]+")
_MENTION = re.compile(r"@(?=\w)")
_IMAGE_OPEN = re.compile(r"!(?=\[)")


def sanitize_agent_text(text: str) -> str:
    """Escape agent output so no markdown construct survives insertion.

    Plan 5.6 rule 1, in order:

    * the 4000-character cap, applied to the raw text first so an escape pair
      is never split by the cut;
    * backtick runs of length three or more are neutralised by spacing the
      backticks apart, so no run can close the fence the composer wraps this
      text in;
    * bare URLs become inert code spans, with any backtick inside the URL
      dropped so the span cannot be ended early;
    * ``@``-mentions are wrapped the same way, so the text still reads but
      Linear has no mention to notify;
    * ``!`` before ``[``, every ``[`` and every ``<`` are escaped, which is
      what leaves no image syntax, no markdown link syntax and no raw HTML
      standing even if the fence were somehow lost;
    * the run neutralisation runs once more, last, because the URL and
      mention spans insert backticks of their own: a pair already adjacent
      in the input ("\\`\\`@alice") extends into a fresh run of three *after*
      the first pass, and rule 1 is a property of the output.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if len(text) > WRITEBACK_BODY_MAX_LENGTH:
        text = text[:WRITEBACK_BODY_MAX_LENGTH] + _TRUNCATION_NOTICE

    text = _BACKTICK_RUN.sub(lambda m: " ".join("`" * len(m.group(0))), text)
    text = _BARE_URL.sub(lambda m: "`" + m.group(0).replace("`", "") + "`", text)
    text = _MENTION.sub("`@`", text)
    text = _IMAGE_OPEN.sub("\\!", text)
    text = text.replace("[", "\\[").replace("<", "\\<")
    # Only spaces are inserted here, so this pass cannot build what it removes.
    text = _BACKTICK_RUN.sub(lambda m: " ".join("`" * len(m.group(0))), text)
    return text


def comment_marker(task_key: str, step_index: int) -> str:
    """The idempotency marker, in the body because Linear has no request key."""
    return f"<!-- agent-control:task:{task_key}:step:{step_index} -->"


def compose_comment_body(
    *,
    task_key: str,
    step_index: int,
    total_steps: int,
    agent_name: str,
    output_text: str,
) -> str:
    """The exact comment 5.6 specifies: marker, attribution, fence, chain link.

    The fence is for legibility; :func:`sanitize_agent_text` is what makes it
    hold. The chain link is appended only when the deployment names a console
    origin, because a relative link in a tracker is a link that 404s.
    """
    sanitized = sanitize_agent_text(output_text)
    quoted = "\n".join(f"> {line}" for line in sanitized.split("\n"))
    lines = [
        comment_marker(task_key, step_index),
        (
            f"**Agent `{agent_name}` finished step {step_index + 1} of "
            f"{total_steps}.** Written by an agent, not reviewed by a human."
        ),
        "> ```",
        quoted,
        "> ```",
    ]
    base_url = linear_settings.console_base_url.strip().rstrip("/")
    if base_url:
        lines.append(f"[Chain]({base_url}/agent-tasks/{task_key})")
    return "\n".join(lines)


def decision_digest(output_text: str, source_ref: str, target_state_id: str) -> str:
    """Bind an accept to the text, the target and the state it moves to.

    Over all three, because a reviewer is accountable for the mutation they
    authorised and not only for the text they read. NUL-joined: none of the
    parts can carry a NUL (the text is a Postgres column, the other two are
    Linear ids), so the boundary cannot be shifted by crafted output.
    """
    joined = "\x00".join((output_text, source_ref, target_state_id)).encode("utf-8")
    return f"sha256:{hashlib.sha256(joined).hexdigest()}"


@dataclass(frozen=True)
class IssueReviewState:
    """The target issue as Linear reports it at render or accept time."""

    ref: str
    identifier: str | None
    title: str | None
    state_id: str | None
    state_name: str | None
    state_type: str | None
    team_key: str | None
    milestone_id: str | None


class LinearWritebackClient(Protocol):
    """The narrow write surface, so tests substitute an object."""

    async def create_comment(self, *, issue_id: str, body: str) -> str:
        """Post one comment. Returns the created comment's id."""
        ...

    async def issue_has_marker(self, *, issue_id: str, marker: str) -> bool:
        """Whether any recent comment on the issue *opens with* the marker.

        First-line equality, never a substring search. The escape prefixes a
        backslash and keeps the marker text itself intact, so a marker spoofed
        inside sanitized agent output still contains the marker as a
        substring - and a substring match would let step 0's output suppress
        step 1's comment. The genuine marker is only ever the composer's own
        first line.
        """
        ...

    async def update_issue_state(self, *, issue_id: str, state_id: str) -> None:
        """Move the issue to the given workflow state."""
        ...

    async def fetch_completed_state_id(self, *, team_key: str) -> str:
        """The team's default completed state. Raises on a team without one."""
        ...

    async def fetch_issue_review_state(self, *, issue_id: str) -> IssueReviewState:
        """Identifier, title, state, team and milestone, read live."""
        ...

    async def aclose(self) -> None: ...


_CREATE_COMMENT = """
mutation AgentControlWritebackComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id }
  }
}
"""

_ISSUE_COMMENTS = """
query AgentControlWritebackMarkers($issueId: String!, $first: Int!) {
  issue(id: $issueId) {
    comments(first: $first) { nodes { body } }
  }
}
"""

_UPDATE_ISSUE_STATE = """
mutation AgentControlWritebackClose($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) { success }
}
"""

_TEAM_STATES = """
query AgentControlTeamCompletedState($key: String!) {
  teams(filter: { key: { eq: $key } }, first: 1) {
    nodes { states { nodes { id type position } } }
  }
}
"""

_ISSUE_REVIEW_STATE = """
query AgentControlIssueReviewState($issueId: String!) {
  issue(id: $issueId) {
    id identifier title
    state { id name type }
    team { key }
    projectMilestone { id }
  }
}
"""

_UNREACHABLE_MESSAGE = "Linear could not be reached."
_UNEXPECTED_SHAPE_MESSAGE = "Linear returned a response this server could not read."
_REJECTED_MESSAGE = "Linear rejected the request."
_UNAUTHORIZED_MESSAGE = "Linear rejected the configured API key."
_RATE_LIMITED_MESSAGE = "Linear is rate-limiting this server."
_UPSTREAM_FAILURE_MESSAGE = "Linear reported an internal error."
_NO_COMPLETED_STATE_MESSAGE = "The Linear team has no completed workflow state."


class HttpLinearWritebackClient:
    """:class:`LinearWritebackClient` over Linear's public GraphQL endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._api_url = api_url
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def create_comment(self, *, issue_id: str, body: str) -> str:
        data = await self._post(_CREATE_COMMENT, {"issueId": issue_id, "body": body})
        payload = _mapping(data, "commentCreate")
        comment_id = _optional_str(_mapping(payload, "comment").get("id"))
        if not payload.get("success") or comment_id is None:
            raise LinearError(_REJECTED_MESSAGE)
        return comment_id

    async def issue_has_marker(self, *, issue_id: str, marker: str) -> bool:
        data = await self._post(
            _ISSUE_COMMENTS, {"issueId": issue_id, "first": _MARKER_COMMENT_PAGE}
        )
        nodes = _nodes(_mapping(_mapping(data, "issue"), "comments"))
        return any(
            _first_line(node.get("body")) == marker for node in nodes
        )

    async def update_issue_state(self, *, issue_id: str, state_id: str) -> None:
        data = await self._post(
            _UPDATE_ISSUE_STATE, {"issueId": issue_id, "stateId": state_id}
        )
        if not _mapping(data, "issueUpdate").get("success"):
            raise LinearError(_REJECTED_MESSAGE)

    async def fetch_completed_state_id(self, *, team_key: str) -> str:
        data = await self._post(_TEAM_STATES, {"key": team_key})
        teams = _nodes(_mapping(data, "teams"))
        if not teams:
            raise LinearError(f"Linear has no team with key '{team_key}'.")
        states = _nodes(_mapping(teams[0], "states"))
        completed = [
            (state.get("position"), _optional_str(state.get("id")))
            for state in states
            if state.get("type") == "completed"
        ]
        # Linear's own UI puts the team's default done state first by
        # position, and Team exposes no more direct field for it.
        ranked = sorted(
            (p if isinstance(p, (int, float)) else float("inf"), sid)
            for p, sid in completed
            if sid is not None
        )
        if not ranked:
            raise LinearError(_NO_COMPLETED_STATE_MESSAGE)
        return ranked[0][1]

    async def fetch_issue_review_state(self, *, issue_id: str) -> IssueReviewState:
        data = await self._post(_ISSUE_REVIEW_STATE, {"issueId": issue_id})
        issue = _mapping(data, "issue")
        ref = _optional_str(issue.get("id"))
        if ref is None:
            raise LinearError("Linear has no issue with that id.")
        state = _mapping(issue, "state")
        return IssueReviewState(
            ref=ref,
            identifier=_optional_str(issue.get("identifier")),
            title=_optional_str(issue.get("title")),
            state_id=_optional_str(state.get("id")),
            state_name=_optional_str(state.get("name")),
            state_type=_optional_str(state.get("type")),
            team_key=_optional_str(_mapping(issue, "team").get("key")),
            milestone_id=_optional_str(_mapping(issue, "projectMilestone").get("id")),
        )

    async def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._api_url,
                json={"query": query, "variables": variables},
                headers={
                    # Personal API keys go in Authorization with no scheme
                    # prefix, the same contract linear_client.py documents.
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # Only the exception class: httpx puts the request URL in
            # str(exc), and upstream text stays out of our logs.
            _logger.warning("Linear write-back request failed: %s", type(exc).__name__)
            raise LinearError(_UNREACHABLE_MESSAGE) from exc

        if response.status_code == 429:
            raise LinearError(
                _RATE_LIMITED_MESSAGE,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code in (401, 403):
            _logger.warning(
                "Linear rejected the configured API key (%s).", response.status_code
            )
            raise LinearError(_UNAUTHORIZED_MESSAGE)
        if response.status_code >= 500:
            raise LinearError(_UPSTREAM_FAILURE_MESSAGE)
        if response.status_code >= 400:
            _logger.warning(
                "Linear returned HTTP %s for a write-back.", response.status_code
            )
            raise LinearError(_REJECTED_MESSAGE)

        try:
            body = response.json()
        except ValueError as exc:
            raise LinearError(_UNEXPECTED_SHAPE_MESSAGE) from exc
        if not isinstance(body, dict):
            raise LinearError(_UNEXPECTED_SHAPE_MESSAGE)
        errors = body.get("errors")
        if errors:
            if isinstance(errors, list):
                _logger.warning(
                    "Linear returned %d GraphQL error(s) on a write-back.", len(errors)
                )
            raise LinearError(_REJECTED_MESSAGE)
        data = body.get("data")
        return data if isinstance(data, dict) else {}


class CompletedStateResolver:
    """Per-team cache over ``fetch_completed_state_id``.

    Plan 5.7 step 4: the target state is read from the team's workflow,
    **never client-supplied and never model-derived**. An agent that writes
    "move this to Done in the ENG workflow" is ignored, and so is a request
    body that says the same thing - there is no field to say it in.
    """

    def __init__(
        self,
        client: LinearWritebackClient,
        *,
        ttl_seconds: float = _COMPLETED_STATE_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    async def resolve_completed_state(self, team_key: str) -> str:
        cached = self._cache.get(team_key)
        if cached is not None and time.monotonic() - cached[1] <= self._ttl_seconds:
            return cached[0]
        state_id = await self._client.fetch_completed_state_id(team_key=team_key)
        self._cache[team_key] = (state_id, time.monotonic())
        return state_id

    def invalidate(self, team_key: str) -> None:
        self._cache.pop(team_key, None)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            return max(0, int(float(raw.strip())))
        except ValueError:
            pass
    reset = response.headers.get("X-RateLimit-Requests-Reset")
    if reset is not None:
        try:
            reset_at = dt.datetime.fromtimestamp(float(reset) / 1000, tz=dt.UTC)
        except (ValueError, OSError, OverflowError):
            return None
        return max(0, int((reset_at - dt.datetime.now(tz=dt.UTC)).total_seconds()))
    return None


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, dict) else {}


def _nodes(connection: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_line(body: Any) -> str | None:
    """The line the composer reserves for the marker. See the protocol note:
    matching anywhere in the body would make the marker forgeable from
    sanitized agent output, and a forged marker suppresses the next report."""
    if not isinstance(body, str):
        return None
    return body.split("\n", 1)[0].strip()
