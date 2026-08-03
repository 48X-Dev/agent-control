"""Task sources. Slice 1 ships one: the file source."""

from __future__ import annotations

from .base import SourceItem, TaskSource, WriteBackOutcome, WriteBackStatus
from .file import FileTaskSource, SourceParseError, resolve_source

__all__ = [
    "FileTaskSource",
    "SourceItem",
    "SourceParseError",
    "TaskSource",
    "WriteBackOutcome",
    "WriteBackStatus",
    "resolve_source",
]
