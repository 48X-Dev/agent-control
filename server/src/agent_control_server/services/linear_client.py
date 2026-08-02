"""Adapter over Linear's GraphQL API.

This module is the only place in the server that knows Linear exists as an
HTTP service. Everything above it depends on :class:`LinearClient`, which is a
two-method protocol, so tests substitute a fake object instead of a fake
transport.

The API key never leaves this module: it is passed to the constructor, held in
one attribute, and written into exactly one request header. Nothing here logs
it, formats it into a message, or copies it into an exception. Error text
raised from here is written by hand rather than lifted from the upstream
response, so a body echoing the request cannot travel back to a browser.

Linear models milestones on projects, not on teams, so a team's milestones are
the union of the milestones of the projects that team owns. One GraphQL
request walks that in a single round trip.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

LINEAR_API_URL = "https://api.linear.app/graphql"

_logger = logging.getLogger(__name__)

_MILESTONES_QUERY = """
query AgentControlTeamMilestones(
  $key: String!
  $projectLimit: Int!
  $milestoneLimit: Int!
) {
  teams(filter: { key: { eq: $key } }, first: 1) {
    nodes {
      id
      key
      projects(first: $projectLimit) {
        nodes {
          id
          name
          url
          projectMilestones(first: $milestoneLimit) {
            nodes {
              id
              name
              description
              targetDate
              status
              progress
            }
          }
        }
      }
    }
  }
}
"""

_UNREACHABLE_MESSAGE = "Linear could not be reached."
_UNEXPECTED_SHAPE_MESSAGE = "Linear returned a response this server could not read."
_REJECTED_MESSAGE = "Linear rejected the request."
_UNAUTHORIZED_MESSAGE = "Linear rejected the configured API key."
_RATE_LIMITED_MESSAGE = "Linear is rate-limiting this server."
_UPSTREAM_FAILURE_MESSAGE = "Linear reported an internal error."


class LinearError(Exception):
    """A milestone read could not be completed.

    ``message`` is written for a client to display: it names what failed
    without quoting the upstream response and never carries a credential.
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class LinearTeamNotFoundError(LinearError):
    """Linear answered, and has no team with the requested key.

    Distinct from a transport failure: the mapping on the Agent Control team is
    wrong, and retrying will not fix it.
    """


@dataclass(frozen=True)
class LinearMilestone:
    """One milestone, flattened onto the project that owns it."""

    id: str
    name: str
    description: str | None
    target_date: dt.date | None
    status: str | None
    progress: float | None
    project_id: str | None
    project_name: str | None
    project_url: str | None


class LinearClient(Protocol):
    """The narrow surface the rest of the server depends on."""

    async def fetch_milestones(self, team_key: str) -> list[LinearMilestone]:
        """Return every milestone across the Linear team's projects.

        Raises :class:`LinearError` when the read fails for any reason. An
        empty list means the team exists and has no milestones.
        """
        ...

    async def aclose(self) -> None:
        """Release any transport the client owns."""
        ...


class HttpLinearClient:
    """:class:`LinearClient` backed by Linear's public GraphQL endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str = LINEAR_API_URL,
        timeout_seconds: float = 10.0,
        max_projects: int = 50,
        max_milestones_per_project: int = 50,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._api_url = api_url
        self._max_projects = max_projects
        self._max_milestones_per_project = max_milestones_per_project
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    async def fetch_milestones(self, team_key: str) -> list[LinearMilestone]:
        """Read one Linear team's milestones, ordered by target date.

        Undated milestones sort last, then by project name and milestone name,
        so repeated reads of unchanged data render identically.
        """
        payload = {
            "query": _MILESTONES_QUERY,
            "variables": {
                "key": team_key,
                "projectLimit": self._max_projects,
                "milestoneLimit": self._max_milestones_per_project,
            },
        }
        response = await self._post(payload)
        body = self._decode(response)
        self._raise_for_graphql_errors(body)
        return _sort_milestones(_parse_milestones(body, team_key=team_key))

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._client.post(
                self._api_url,
                json=payload,
                headers={
                    # Personal API keys go in Authorization with no scheme
                    # prefix; OAuth tokens would use "Bearer <token>".
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            # Only the exception class is logged. httpx puts the request URL in
            # str(exc), and while the key travels in a header rather than the
            # URL, keeping upstream text out of our logs entirely is the rule
            # that stays true if that ever changes.
            _logger.warning("Linear request failed: %s", type(exc).__name__)
            raise LinearError(_UNREACHABLE_MESSAGE) from exc

        if response.status_code == 429:
            raise LinearError(
                _RATE_LIMITED_MESSAGE,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code in (401, 403):
            _logger.warning("Linear rejected the configured API key (%s).", response.status_code)
            raise LinearError(_UNAUTHORIZED_MESSAGE)
        if response.status_code >= 500:
            raise LinearError(_UPSTREAM_FAILURE_MESSAGE)
        if response.status_code >= 400:
            _logger.warning("Linear returned HTTP %s for a milestone read.", response.status_code)
            raise LinearError(_REJECTED_MESSAGE)
        return response

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise LinearError(_UNEXPECTED_SHAPE_MESSAGE) from exc
        if not isinstance(body, dict):
            raise LinearError(_UNEXPECTED_SHAPE_MESSAGE)
        return body

    @staticmethod
    def _raise_for_graphql_errors(body: dict[str, Any]) -> None:
        """Turn a GraphQL ``errors`` array into a :class:`LinearError`.

        GraphQL reports failures with HTTP 200, so this check is what catches a
        malformed query or a key without the scope to read projects. The
        upstream messages are logged for an operator and replaced with fixed
        text in the raised error, because they can quote parts of the request.
        """
        errors = body.get("errors")
        if not errors:
            return
        if isinstance(errors, list):
            _logger.warning("Linear returned %d GraphQL error(s).", len(errors))
        raise LinearError(_REJECTED_MESSAGE)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    """Read how long Linear wants the server to wait, in seconds.

    Prefers ``Retry-After``; falls back to Linear's own reset header, which
    carries an absolute epoch timestamp in milliseconds.
    """
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
        remaining = (reset_at - dt.datetime.now(tz=dt.UTC)).total_seconds()
        return max(0, int(remaining))
    return None


def _parse_milestones(body: dict[str, Any], *, team_key: str) -> list[LinearMilestone]:
    """Flatten the nested GraphQL response into milestone rows.

    Unreadable individual rows are skipped rather than failing the whole read:
    one milestone with a surprising shape should not blank a team's board.
    """
    teams = _nodes(_mapping(_mapping(body, "data"), "teams"))
    if not teams:
        raise LinearTeamNotFoundError(f"Linear has no team with key '{team_key}'.")

    milestones: list[LinearMilestone] = []
    for project in _nodes(_mapping(teams[0], "projects")):
        project_id = _optional_str(project.get("id"))
        project_name = _optional_str(project.get("name"))
        project_url = _optional_str(project.get("url"))
        for node in _nodes(_mapping(project, "projectMilestones")):
            milestone_id = _optional_str(node.get("id"))
            name = _optional_str(node.get("name"))
            if milestone_id is None or name is None:
                continue
            milestones.append(
                LinearMilestone(
                    id=milestone_id,
                    name=name,
                    description=_optional_str(node.get("description")),
                    target_date=_parse_date(node.get("targetDate")),
                    status=_optional_str(node.get("status")),
                    progress=_parse_progress(node.get("progress")),
                    project_id=project_id,
                    project_name=project_name,
                    project_url=project_url,
                )
            )
    return milestones


def _sort_milestones(milestones: list[LinearMilestone]) -> list[LinearMilestone]:
    return sorted(
        milestones,
        key=lambda m: (
            m.target_date is None,
            m.target_date or dt.date.min,
            m.project_name or "",
            m.name,
        ),
    )


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


def _parse_date(value: Any) -> dt.date | None:
    """Read Linear's ``TimelessDate``, tolerating a full timestamp.

    The scalar is documented as ``YYYY-MM-DD``, but the same field has been
    seen carrying an ISO datetime, so both are accepted and anything else is
    dropped rather than failing the read.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_progress(value: Any) -> float | None:
    """Coerce Linear's progress to the 0-1 range the response model allows."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(1.0, max(0.0, float(value)))
