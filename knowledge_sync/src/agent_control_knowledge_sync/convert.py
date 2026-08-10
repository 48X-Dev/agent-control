"""Bytes to markdown the chunker can section, with an honest status attached.

Conversion itself is the shipped ``agent_control_models.attachment_converter``
library. What this module adds is the step between the converter and the chunker:
real corpora do not arrive with the heading structure heading-bounded chunking
wants, and measurement decides both fixes.

Measured 2026-08-10 against MarkItDown 0.1.x. An 89,806-character PDF came back
with zero ATX headings, 803 lines and eight blank-line paragraph breaks: the
page's hard wrapping survives, so every newline is soft and the whole document
is a handful of enormous blocks that the chunker would cut mid-word. A ``.pptx``
came back with slide boundaries as ``<!-- Slide number: N -->`` HTML comments,
speaker notes under ``### Notes:`` and nothing else at heading level 1 or 2, so
its structure is present but invisible to a splitter that reads ``#``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from agent_control_models.attachment_converter import convert_attachment
from agent_control_models.knowledge import CHUNK_MAX_CHARS

__all__ = [
    "INDEXABLE_STATUSES",
    "PASSTHROUGH_MIMES",
    "REFLOW_TARGET_CHARS",
    "Converted",
    "RawConverter",
    "convert_document",
    "normalize_for_chunking",
    "shipped_converter",
]

STATUS_EXPORTED = "exported"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_CONVERTER_UNAVAILABLE = "converter_unavailable"

FAILURE_EMPTY_INPUT = "empty_input"

# The converter's own enum values that carry text worth indexing, plus the
# 'exported' status Drive-native exports arrive under.
INDEXABLE_STATUSES = frozenset({STATUS_EXPORTED, "text_layer_extracted", "ocr_extracted"})

# Types that are already the chunker's input. Running them through a converter
# would only re-encode them, and a Docs export lands here.
PASSTHROUGH_MIMES = frozenset(
    {"text/markdown", "text/x-markdown", "text/plain", "text/csv", "application/x-markdown"}
)

# One reflowed paragraph is meant to become exactly one chunk, so the target is
# the chunker's ceiling: the packer then takes a paragraph whole and cuts where
# a sentence ended rather than where the character count ran out.
REFLOW_TARGET_CHARS = CHUNK_MAX_CHARS

_SLIDE_MARKER_RE = re.compile(r"(?m)^[ \t]*<!--[ \t]*Slide number:[ \t]*(\d+)[ \t]*-->[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_STRUCTURAL_RE = re.compile(r"^[ \t]*(?:\||#{1,6}[ \t]|[-*+][ \t]|\d+[.)][ \t]|>|<!--|!\[)")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[ \t]+(?=[\"'(\[]?[A-Z0-9])")
_HYPHEN_WRAP_RE = re.compile(r"[A-Za-z]-$")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

_MIN_WRAPPED_LINES = 2


@dataclass(frozen=True, slots=True)
class Converted:
    """Markdown ready for chunking, and the status that says how it got there."""

    text: str
    status: str
    error_code: str | None = None

    @property
    def indexable(self) -> bool:
        return self.status in INDEXABLE_STATUSES and bool(self.text.strip())


class RawConverter(Protocol):
    """Bytes and a declared MIME in, text and a status out."""

    def __call__(self, data: bytes, *, declared_mime: str | None) -> Converted: ...


def convert_document(
    data: bytes,
    *,
    declared_mime: str | None,
    converter: RawConverter | None = None,
) -> Converted:
    """Convert one document's bytes and normalize the result for the chunker."""

    if not data:
        return Converted(text="", status=STATUS_EMPTY, error_code=FAILURE_EMPTY_INPUT)

    if declared_mime in PASSTHROUGH_MIMES:
        text = data.decode("utf-8", errors="replace")
        return Converted(text=normalize_for_chunking(text), status=STATUS_EXPORTED)

    raw = (converter or shipped_converter)(data, declared_mime=declared_mime)
    if not raw.indexable:
        return raw
    return Converted(
        text=normalize_for_chunking(raw.text),
        status=raw.status,
        error_code=raw.error_code,
    )


def shipped_converter(data: bytes, *, declared_mime: str | None) -> Converted:
    """Run the shipped converter.

    A container without the office extras gets ``converter_unavailable`` on
    every file naming the missing parser, because the library says so itself.
    """

    result = convert_attachment(data, declared_mime=declared_mime)
    return Converted(
        text=str(result.text),
        status=str(result.status),
        error_code=result.failure_code,
    )


def normalize_for_chunking(text: str) -> str:
    """Give the chunker boundaries it can see: slide headings, real paragraphs."""

    promoted = _SLIDE_MARKER_RE.sub(r"# Slide \1", text)
    return _BLANK_RUN_RE.sub("\n\n", _reflow(promoted)).strip()


def _reflow(text: str) -> str:
    """Rewrap prose runs, leaving fenced code exactly as it arrived."""

    out: list[str] = []
    run: list[str] = []
    fence = ""

    for line in text.splitlines():
        marker = _FENCE_RE.match(line)
        if fence:
            out.append(line)
            if marker and marker.group(1) == fence:
                fence = ""
            continue
        if marker:
            out.extend(_reflow_run(run))
            run = []
            out.append(line)
            fence = marker.group(1)
            continue
        if not line.strip() or _STRUCTURAL_RE.match(line):
            out.extend(_reflow_run(run))
            run = []
            out.append(line)
            continue
        run.append(line)

    out.extend(_reflow_run(run))
    return "\n".join(out)


def _reflow_run(lines: list[str]) -> list[str]:
    """Unwrap one hard-wrapped prose run into sentence-aligned paragraphs."""

    if not _is_hard_wrapped(lines):
        return lines
    paragraphs = _group_sentences(_unwrap(lines))
    out: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        if index:
            out.append("")
        out.append(paragraph)
    return out


def _is_hard_wrapped(lines: list[str]) -> bool:
    """Decide whether these line breaks are the page's or the author's."""

    if len(lines) < _MIN_WRAPPED_LINES:
        return False
    ends = sum(1 for line in lines[:-1] if _SENTENCE_END_RE.search(line.rstrip()))
    return ends * 2 < len(lines) - 1


def _unwrap(lines: list[str]) -> str:
    """Join wrapped lines into one string, healing words the wrap split."""

    text = lines[0].strip()
    for line in lines[1:]:
        stripped = line.strip()
        # "contribu-\ntion" is one word and the hyphen is the page's. The trade,
        # stated: a compound that happens to wrap at its own hyphen loses it.
        if _HYPHEN_WRAP_RE.search(text) and stripped[:1].islower():
            text = f"{text[:-1]}{stripped}"
        else:
            text = f"{text} {stripped}"
    return text


def _group_sentences(text: str) -> list[str]:
    """Pack sentences up to the target, breaking before it rather than after."""

    paragraphs: list[str] = []
    buffer = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        candidate = f"{buffer} {sentence}" if buffer else sentence
        if buffer and len(candidate) > REFLOW_TARGET_CHARS:
            paragraphs.append(buffer)
            buffer = sentence
            continue
        buffer = candidate
    if buffer:
        paragraphs.append(buffer)
    return paragraphs
