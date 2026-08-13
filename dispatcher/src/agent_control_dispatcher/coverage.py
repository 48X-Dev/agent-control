"""The `## Coverage` section an agent closes its report with, read back."""

from __future__ import annotations

import re

__all__ = ["HEADING", "unmet_items"]

HEADING = "## Coverage"

# `done` is the only verdict that closes a part. Everything else the footer
# offers - `partial`, `not determined` - is work the step did not finish, and a
# line whose verdict is unreadable counts as unfinished: a step that stopped
# saying whether it covered something has not shown that it did.
_LINE = re.compile(r"^\s*[-*]\s+(?P<body>.+)$")
_DONE = re.compile(r"\bdone\b", re.IGNORECASE)
_MARKUP = re.compile(r"[*_`]+")


def unmet_items(report: str | None) -> list[str]:
    """The coverage lines that did not land, in the agent's own words.

    An empty list means every part was `done`, or that the report carried no
    coverage section at all, which is deliberately not a reason to spend another
    turn: a step that ignored the format has not asked for one.
    """
    if not report:
        return []
    start = report.find(HEADING)
    if start < 0:
        return []
    unmet: list[str] = []
    for raw in report[start + len(HEADING) :].splitlines():
        if raw.startswith("## "):
            break
        match = _LINE.match(raw)
        if match is None:
            continue
        body = _MARKUP.sub("", match.group("body")).strip()
        if body and not _DONE.search(body):
            unmet.append(body)
    return unmet
