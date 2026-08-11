"""Index-time logic for the company-knowledge corpus: chunking, scrubbing, naming."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .files import normalize_display_name


class KnowledgeRefusalCode(StrEnum):
    """Every way a knowledge request can come back with no results."""

    QUERY_TOO_SHORT = "query_too_short"
    QUERY_TOO_LONG = "query_too_long"
    RATE_LIMITED = "rate_limited"
    KNOWLEDGE_UNAVAILABLE = "knowledge_unavailable"
    KNOWLEDGE_DISABLED = "knowledge_disabled"
    CORPUS_EMPTY = "corpus_empty"


# The splitting target, and the floor a fragment has to clear to stand alone.
#
# 200 is ``attachment_delivery.MIN_TEXT_BLOCK_CHARS``, borrowed with its
# reasoning intact: two hundred characters of a policy is not a policy, it is a
# fragment an agent would answer from as if it were the whole.
CHUNK_MAX_CHARS = 2000
CHUNK_MIN_CHARS = 200

# Headings deeper than this are body text. Level 4 and beyond are usually
# within-section structure, and promoting them would shred a section into
# fragments that each clear no floor.
MAX_HEADING_LEVEL = 3

HEADING_PATH_SEPARATOR = " > "

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# The credential shapes the shipped memory control already names, character for
# character, plus the entropy rule this corpus needs. Keeping the first four in
# the deny-list's own words means a string this scrub admits is not one that
# control would have caught.
_CREDENTIAL_SHAPES: tuple[tuple[str, str], ...] = (
    ("api_key", r"sk-[A-Za-z0-9]{16,}"),
    ("aws_access_key_id", r"AKIA[0-9A-Z]{16}"),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("password_assignment", r"\bpassword\s*[:=]"),
    # A long unbroken hex or base64 run on the right-hand side of an
    # assignment, which is 5.6's rule and deliberately not narrowed to keys
    # whose name reads as credential-ish: a secret is regularly assigned to a
    # name nobody would have guessed, and a deny-list that only catches the
    # obvious names catches the secrets that were never the risk.
    #
    # The cost is real and is paid in the open: `commit: <sha>` and
    # `sha256: <digest>` match, so a release-note chunk carrying either is
    # dropped and counted. A digest in running prose with no assignment beside
    # it is not touched. `test_knowledge.py` pins both directions so the trade
    # is a decision a reader can find rather than an accident.
    ("high_entropy_assignment", r"[:=]\s*[\"']?[A-Za-z0-9+/_-]{32,}={0,2}[\"']?"),
)

_SCRUB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _CREDENTIAL_SHAPES
)

# Whole files that are credentials by name. A document matching one of these is
# never chunked at all; the sync tombstones it with reason 'secret_file'.
_SECRET_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\.env(\..+)?$",
        r"^.+\.pem$",
        r"^id_rsa(\..+)?$",
        r"^id_(dsa|ecdsa|ed25519)(\..+)?$",
        r"^credentials\.json$",
        r"^.+\.key$",
        r"^.+\.p12$",
        r"^.+\.pfx$",
    )
)


@dataclass(frozen=True)
class Chunk:
    """One indexable unit of a converted document."""

    ordinal: int
    heading_path: str | None
    body: str

    @property
    def chars(self) -> int:
        return len(self.body)


@dataclass(frozen=True)
class ScrubResult:
    """What the deny-list decided about one chunk's body."""

    clean: bool
    matched: str | None = None


@dataclass(frozen=True)
class ScrubbedChunks:
    """The chunks a writer may index, and how many the deny-list took."""

    chunks: list[Chunk]
    secrets_skipped: int


def chunk_markdown(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    min_chars: int = CHUNK_MIN_CHARS,
) -> list[Chunk]:
    """Split converted markdown into heading-bounded chunks."""

    sections = _split_into_sections(text)
    merged = _merge_short_sections(sections, min_chars=min_chars)
    pieces: list[tuple[str | None, str]] = []
    for heading_path, body in merged:
        pieces.extend(
            (heading_path, piece)
            for piece in _split_oversized(body, max_chars=max_chars, min_chars=min_chars)
        )
    return [
        Chunk(ordinal=index, heading_path=heading_path, body=body)
        for index, (heading_path, body) in enumerate(pieces)
    ]


def scrub_chunk(body: str) -> ScrubResult:
    """Report whether a chunk body carries a known credential shape."""

    for name, pattern in _SCRUB_PATTERNS:
        if pattern.search(body):
            return ScrubResult(clean=False, matched=name)
    return ScrubResult(clean=True)


def chunk_and_scrub(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    min_chars: int = CHUNK_MIN_CHARS,
) -> ScrubbedChunks:
    """Chunk a document and drop every piece that carries a credential shape."""

    chunks = chunk_markdown(text, max_chars=max_chars, min_chars=min_chars)
    dropped = {index for index, chunk in enumerate(chunks) if not scrub_chunk(chunk.body).clean}
    for index in range(len(chunks) - 1):
        if index in dropped or index + 1 in dropped:
            continue
        if not scrub_chunk(chunks[index].body + chunks[index + 1].body).clean:
            dropped.update((index, index + 1))
    kept = [chunk for index, chunk in enumerate(chunks) if index not in dropped]
    return ScrubbedChunks(chunks=kept, secrets_skipped=len(dropped))


def is_secret_filename(name: str | None) -> bool:
    """Report whether a filename is itself a credential, whatever it contains."""

    if not name:
        return False
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return any(pattern.match(leaf) for pattern in _SECRET_FILENAME_PATTERNS)


def normalize_index_name(raw: str | None) -> str | None:
    """Normalize one attacker-influenced name for storage."""

    name, _ = normalize_display_name(raw)
    return name


def normalize_index_path(raw: str | None) -> str | None:
    """Normalize a corpus path segment by segment, keeping the separators."""

    if not raw:
        return None
    segments = [normalize_index_name(segment) for segment in raw.split("/")]
    kept = [segment for segment in segments if segment]
    return "/".join(kept) or None


def _split_into_sections(text: str) -> list[tuple[str | None, str]]:
    """Cut markdown at ``#``..``###`` boundaries, ignoring headings inside fences."""

    stack: list[tuple[int, str]] = []
    sections: list[tuple[str | None, list[str]]] = []
    current: tuple[str | None, list[str]] = (None, [])
    in_fence = False
    fence_marker = ""

    for line in text.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            current[1].append(line)
            continue

        heading = None if in_fence else _HEADING_RE.match(line)
        if heading is None or len(heading.group(1)) > MAX_HEADING_LEVEL:
            current[1].append(line)
            continue

        if _has_text(current[1]):
            sections.append(current)
        level = len(heading.group(1))
        title = normalize_index_name(heading.group(2))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title or ""))
        path = HEADING_PATH_SEPARATOR.join(part for _, part in stack if part) or None
        current = (path, [line])

    if _has_text(current[1]):
        sections.append(current)
    return [(path, _tidy("\n".join(lines))) for path, lines in sections]


def _has_text(lines: list[str]) -> bool:
    return any(line.strip() for line in lines)


def _tidy(body: str) -> str:
    return _BLANK_RUN_RE.sub("\n\n", body.strip())


def _merge_short_sections(
    sections: list[tuple[str | None, str]],
    *,
    min_chars: int,
) -> list[tuple[str | None, str]]:
    """Fold sections under the floor into a neighbour, preferring the next one."""

    merged: list[tuple[str | None, str]] = []
    pending: tuple[str | None, str] | None = None

    for heading_path, body in sections:
        if pending is not None:
            heading_path, body = _join(pending, (heading_path, body))
            pending = None
        if len(body) < min_chars:
            pending = (heading_path, body)
            continue
        merged.append((heading_path, body))

    if pending is not None:
        if merged:
            merged[-1] = _join(merged[-1], pending)
        else:
            # The whole document is shorter than the floor. Nothing to merge
            # with, and dropping it would lose the document entirely.
            merged.append(pending)
    return merged


def _join(
    left: tuple[str | None, str],
    right: tuple[str | None, str],
) -> tuple[str | None, str]:
    dominant = left if len(left[1]) >= len(right[1]) else right
    return dominant[0], f"{left[1]}\n\n{right[1]}"


def _split_oversized(body: str, *, max_chars: int, min_chars: int) -> list[str]:
    """Pack paragraphs up to ``max_chars``; hard-split a paragraph that exceeds it."""

    if len(body) <= max_chars:
        return [body]

    pieces: list[str] = []
    buffer = ""
    for paragraph in _paragraphs(body):
        for part in _hard_split(paragraph, max_chars=max_chars):
            candidate = f"{buffer}\n\n{part}" if buffer else part
            if len(candidate) <= max_chars:
                buffer = candidate
                continue
            if buffer:
                pieces.append(buffer)
            buffer = part
    if buffer:
        pieces.append(buffer)

    if len(pieces) > 1 and len(pieces[-1]) < min_chars:
        tail = pieces.pop()
        pieces[-1] = f"{pieces[-1]}\n\n{tail}"
    return pieces


def _paragraphs(body: str) -> list[str]:
    return [block.strip() for block in body.split("\n\n") if block.strip()]


def _hard_split(paragraph: str, *, max_chars: int) -> list[str]:
    """The single-paragraph pathology: no boundary to cut on, so cut evenly."""

    if len(paragraph) <= max_chars:
        return [paragraph]
    count = -(-len(paragraph) // max_chars)
    size = -(-len(paragraph) // count)
    return [paragraph[start : start + size] for start in range(0, len(paragraph), size)]
