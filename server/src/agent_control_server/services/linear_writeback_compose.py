"""Composing a write-back comment, and defusing the agent text inside it.

Split from :mod:`.linear_writeback`, which holds the client and the queue's
mechanics. Nothing here reaches the network or the database, which is what
makes the escaping rules testable as pure functions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from agent_control_models.tasks import WRITEBACK_BODY_MAX_LENGTH

from ..config import linear_settings

_TRUNCATION_NOTICE = "\n[output truncated by agent control]"

FILE_LINES_MAX_CHARS = 800
"""The pointer block's own budget inside the body cap.

Server-authored from display names, but the count of an agent's finals is not
bounded, so without this the pointers could crowd the output out entirely."""

_OUTPUT_TEXT_MIN_BUDGET = 200
"""What the agent's own words keep even when everything else is at its cap."""

_BACKTICK_RUN = re.compile(r"`{3,}")
_BARE_URL = re.compile(r"https?://[^\s`]+")
_MENTION = re.compile(r"@(?=\w)")
_IMAGE_OPEN = re.compile(r"!(?=\[)")


def sanitize_agent_text(text: str, *, max_length: int = WRITEBACK_BODY_MAX_LENGTH) -> str:
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
    if len(text) > max_length:
        text = text[: max(max_length, 0)] + _TRUNCATION_NOTICE

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
    file_lines: Sequence[str] = (),
) -> str:
    """The exact comment 5.6 specifies: marker, attribution, fence, chain link.

    The fence is for legibility; :func:`sanitize_agent_text` is what makes it
    hold, and ``file_lines`` point at a file rather than pasting one in. The
    chain link is appended only when the deployment names a console origin,
    because a relative link in a tracker is a link that 404s.
    """
    marker = comment_marker(task_key, step_index)
    attribution = (
        f"**Agent `{agent_name}` finished step {step_index + 1} of "
        f"{total_steps}.** Written by an agent, not reviewed by a human."
    )
    base_url = linear_settings.console_base_url.strip().rstrip("/")
    chain = [f"[Chain]({base_url}/agent-tasks/{task_key})"] if base_url else []
    pointers = _bounded_file_lines(file_lines)

    # The cap belongs to the whole body, not to the agent's text alone. Every
    # part but the quoted output is server-authored and bounded, so the output
    # is what gives way; the quoting adds two characters a line, which is why
    # the budget is halved rather than subtracted exactly.
    overhead = len("\n".join([marker, attribution, "> ```", "> ```", *pointers, *chain]))
    budget = max((WRITEBACK_BODY_MAX_LENGTH - overhead) // 2, _OUTPUT_TEXT_MIN_BUDGET)
    sanitized = sanitize_agent_text(output_text, max_length=budget)
    quoted = "\n".join(f"> {line}" for line in sanitized.split("\n"))
    return "\n".join([marker, attribution, "> ```", quoted, "> ```", *pointers, *chain])


def _bounded_file_lines(file_lines: Sequence[str]) -> list[str]:
    """Keep the pointers inside their own budget, and say what was dropped."""
    kept: list[str] = []
    used = 0
    for index, line in enumerate(file_lines):
        if used + len(line) > FILE_LINES_MAX_CHARS:
            kept.append(f"- and {len(file_lines) - index} more, not listed here.")
            break
        kept.append(line)
        used += len(line) + 1
    return kept


def compose_agent_comment_body(*, task_key: str, agent_name: str, text: str) -> str:
    """A comment an agent was asked to save mid-session, not one a step produced.

    Same sanitize-and-fence treatment as :func:`compose_comment_body`, and a
    deliberately different attribution line: this text was saved on a person's
    instruction during a conversation, and describing it as a finished step
    would misstate both what it is and when it was written.

    No marker, because there is nothing to deduplicate against. The step
    comment's marker exists so a retried queue row cannot post twice; this one
    is sent inline and its outcome is reported to the caller, and two saves are
    two comments because that is what asking twice means.
    """
    sanitized = sanitize_agent_text(text)
    quoted = "\n".join(f"> {line}" for line in sanitized.split("\n"))
    lines = [
        (
            f"**Agent `{agent_name}` saved this from a chat, when asked to.** "
            "Written by an agent, not reviewed by a human."
        ),
        "> ```",
        quoted,
        "> ```",
    ]
    base_url = linear_settings.console_base_url.strip().rstrip("/")
    if base_url:
        lines.append(f"[Chain]({base_url}/agent-tasks/{task_key})")
    return "\n".join(lines)


