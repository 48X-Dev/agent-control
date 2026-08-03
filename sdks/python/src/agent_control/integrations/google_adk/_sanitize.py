"""Hostile-input handling for Google ADK file parts.

Everything here answers the same question: what can a filename, a MIME type or
a line of user text do to the transcript, to a control, or to the person reading
either. A file part is authored by whoever can talk to the agent, which under
the shipped defaults is everyone, so none of its strings are trusted.

``normalize_display_name``, ``sniff_mime`` and ``is_mime_mismatch`` now live in
``agent_control_models.files`` and are re-exported here. The server enforces its
upload gate with the same three functions, and it cannot import this package.
These names are the same objects, not copies: a second implementation would let
the descriptor a control reads and the gate the server enforces disagree about
the same file.
"""

from __future__ import annotations

import re

from agent_control_models.files import (
    MAX_DISPLAY_NAME_CHARS,
    is_mime_mismatch,
    normalize_display_name,
    sniff_mime,
)

__all__ = [
    "MARKER_PREFIX",
    "MAX_DISPLAY_NAME_CHARS",
    "is_mime_mismatch",
    "neutralize_marker",
    "normalize_display_name",
    "sniff_mime",
]

# The transcript marker. Forgeable by anyone who can put text in front of the
# model, which is why controls key on ``context.agent_control.*`` instead and
# why every occurrence in text we did not author is neutralized below.
MARKER_PREFIX = "[agent-control:"
_NEUTRAL_HYPHEN = "‑"  # non-breaking hyphen: reads the same, matches nothing
_MARKER_RE = re.compile(r"\[agent(-)control:", re.IGNORECASE)


def neutralize_marker(text: str) -> str:
    """Defuse the transcript marker in text this SDK did not author.

    User messages, tool results and extracted document text can all contain the
    literal marker, so without this an attacker forges a "blocked by policy"
    line or a benign-looking descriptor into the model's view. The replacement
    hyphen is U+2011: a human reads the same string, a matcher does not.
    """

    return _MARKER_RE.sub(lambda m: m.group(0)[:6] + _NEUTRAL_HYPHEN + m.group(0)[7:], text)
