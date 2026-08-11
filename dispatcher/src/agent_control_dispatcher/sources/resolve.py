"""Turning a ``--source`` argument into a source, for both of them."""

from __future__ import annotations

import re

from .base import TaskSource
from .file import SourceParseError
from .file import resolve_source as resolve_file_source
from .linear import SOURCE_PREFIX, LinearMilestoneSource, MilestoneIssueReader

_SAFE_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")
"""What a milestone id may contain. Linear's are UUIDs, so this is wider than
it needs to be and still narrow enough to matter: the id becomes a path segment
in the URL the client builds, and httpx resolves ``..`` in a path before it
sends. ``linear-milestone:../../../agent-sessions`` would otherwise be a GET
against a different route entirely, which is a surprising thing for a source
argument to be able to do even when the person typing it is the operator."""

_MALFORMED_MILESTONE_ID = (
    "'{value}' is not a milestone id. It goes into the URL of the scope read, so "
    "it may hold letters, digits, hyphens and underscores only. Read one from "
    "GET /api/v1/teams/<team>/milestones."
)

_TEAM_REQUIRED = (
    "--source {spec} needs --team. The team is what resolves the Linear team key "
    "the read is scoped to, and a milestone in a project shared across teams is "
    "the ordinary case, not the exotic one."
)

_TEAM_NOT_APPLICABLE = (
    "--team is only meaningful for a Linear source. A file has no team scope, and "
    "accepting the flag here would suggest it bounded something."
)

_EMPTY_MILESTONE_ID = (
    "--source {prefix}<id> needs a milestone id. Read one from "
    "GET /api/v1/teams/<team>/milestones."
)


def build_source(
    spec: str, *, team_slug: str | None, reader: MilestoneIssueReader
) -> TaskSource:
    """Build the source named by ``--source``."""

    if spec.startswith(SOURCE_PREFIX):
        milestone_id = spec[len(SOURCE_PREFIX) :].strip()
        if not milestone_id:
            raise SourceParseError(_EMPTY_MILESTONE_ID.format(prefix=SOURCE_PREFIX))
        if _SAFE_ID.match(milestone_id) is None:
            raise SourceParseError(_MALFORMED_MILESTONE_ID.format(value=milestone_id))
        if not team_slug:
            raise SourceParseError(_TEAM_REQUIRED.format(spec=spec))
        return LinearMilestoneSource(
            reader=reader, milestone_id=milestone_id, team_slug=team_slug
        )

    if team_slug:
        raise SourceParseError(_TEAM_NOT_APPLICABLE)
    return resolve_file_source(spec)
