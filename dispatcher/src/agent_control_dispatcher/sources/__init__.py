"""Task sources. Two of them: the file, and one Linear milestone."""

from __future__ import annotations

from .base import (
    ScopedTaskSource,
    ScopeReport,
    SourceItem,
    TaskSource,
    WriteBackOutcome,
    WriteBackStatus,
)
from .file import FileTaskSource, SourceParseError, resolve_source
from .linear import (
    SOURCE_PREFIX,
    LinearMilestoneSource,
    LinearScopeError,
    MilestoneIssueReader,
)
from .resolve import build_source

__all__ = [
    "SOURCE_PREFIX",
    "FileTaskSource",
    "LinearMilestoneSource",
    "LinearScopeError",
    "MilestoneIssueReader",
    "ScopeReport",
    "ScopedTaskSource",
    "SourceItem",
    "SourceParseError",
    "TaskSource",
    "WriteBackOutcome",
    "WriteBackStatus",
    "build_source",
    "resolve_source",
]
