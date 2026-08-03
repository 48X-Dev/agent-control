"""What a conversion result *is*, separated from the machinery that fills it in.

Split out of ``attachment_converter`` for one reason: that module carries the
measurements and the reasoning behind every escalation decision, and the two
together outgrew a file anyone wants to read. Nothing here makes a decision.
These are the vocabulary and the shapes, and they are the whole of what a
caller needs to store a result or render a descriptor from one.

Import from ``attachment_converter``, not from here. Every name below is
re-exported there, so the pipeline and its contract stay one import for callers
while remaining two files on disk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from .attachment_converter_backends import ConverterKind

LOW_TEXT_THRESHOLD_CHARS = 40
"""Below this many meaningful characters, a converter's read triggers the next one.

**An escalation trigger and not a delivery floor.** It answers one question -
"is this thin enough to be worth twenty seconds of OCR" - and it stops
mattering once there is nothing left to escalate to. A converter that has run
last and found something is reported as having found it however little it is,
because the alternative is a result that carries text under a status saying
none was found. See :func:`_degraded_status` for the measurement that settled
this.

Not a tuned number. It is the plan's own ``attachment_low_text_page_chars``,
the count under which a page is already defined to carry no usable text, read
here as a whole-document floor. Forty characters is shorter than most filenames
and no agent can act on it.

Against the measured corpus it separates cleanly: the carousel PDF's text layer
is 733 characters, eighteen times the threshold, and every PNG is zero. There
is no near-miss on either side of it, so the exact value is not load-bearing
anywhere in the range 1 to 700 - which is the argument for choosing one that
already means something rather than inventing a constant.

**What it does not catch, stated rather than discovered.** A PDF whose text
layer holds a 200-character title page in front of twenty scanned pages clears
this threshold and never escalates. That is the plan's section 2.5 gap in its
converter form: this module measures text, and text is not coverage. Per-page
counters are the mechanism that would close it and they need a page-aware
converter, which is a later phase."""

DEFAULT_TEXT_MAX_CHARS = 2_560_000
"""The plan's ``attachment_text_max_chars``. A cap, not a policy: exceeding it
sets ``text_truncated`` and the caller decides what that is worth."""

DEFAULT_CONVERTIBLE_MIMES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"})
"""The types this library will hand to a parser.

The same set the plan admits, restated as a default argument rather than read
from configuration, because this module reads no configuration. A caller with a
different accepted set passes it in."""

FAILURE_TYPE_NOT_CONVERTIBLE = "type_not_convertible"
FAILURE_EMPTY_INPUT = "empty_input"
FAILURE_NO_CONVERTER_INSTALLED = "no_converter_installed"
FAILURE_OCR_CONVERTER_ABSENT = "ocr_converter_absent"
FAILURE_SOURCE_ECHOED = "source_echoed"

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


class ConversionStatus(StrEnum):
    """What happened, in the caller's vocabulary rather than a parser's."""

    TEXT_LAYER_EXTRACTED = "text_layer_extracted"
    """A text layer was read. Whether it is the *document* is unknown."""

    OCR_EXTRACTED = "ocr_extracted"
    """Layout analysis and OCR ran and returned usable text."""

    EMPTY = "empty"
    """Every converter the caller allowed ran and found nothing to read.

    "Allowed" rather than "available": with ``allow_ocr=False`` an installed
    OCR pass is never offered the document, so this says nothing about what
    OCR would have found."""

    ENCRYPTED = "encrypted"
    """Password-protected. Not escalated: OCR cannot open it either."""

    UNSUPPORTED_TYPE = "unsupported_type"
    """The sniffed type is outside the convertible set. No parser was built."""

    CONVERTER_UNAVAILABLE = "converter_unavailable"
    """A converter this document needed is not installed. Says which."""

    FAILED = "failed"
    """A converter broke. Carries a code and never an upstream message."""


class AttemptOutcome(StrEnum):
    """What one converter did, recorded per converter rather than summarized."""

    EXTRACTED = "extracted"
    LOW_TEXT = "low_text"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    ENCRYPTED = "encrypted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConverterAttempt:
    """One converter's run, kept whether or not its output was used.

    The losing attempt is the interesting one. "MarkItDown returned zero
    characters and Docling returned 552" is the sentence that explains a
    twenty-second conversion, and a result that reported only the winner would
    make every OCR run look unexplained.
    """

    name: str
    kind: ConverterKind
    outcome: AttemptOutcome
    text_chars: int = 0
    meaningful_chars: int = 0
    duration_seconds: float = 0.0
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class ConversionOptions:
    """Everything tunable, defaulted, and passed in rather than read."""

    accepted_mimes: frozenset[str] = DEFAULT_CONVERTIBLE_MIMES
    low_text_threshold_chars: int = LOW_TEXT_THRESHOLD_CHARS
    text_max_chars: int = DEFAULT_TEXT_MAX_CHARS
    allow_ocr: bool = True


DEFAULT_OPTIONS = ConversionOptions()


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """The whole answer: the text, how it was got, and how much to trust it."""

    status: ConversionStatus
    text: str = ""
    converter: str | None = None
    """Which converter produced ``text``. ``None`` when none of them did."""

    attempts: tuple[ConverterAttempt, ...] = ()
    failure_code: str | None = None
    sniffed_mime: str | None = None
    declared_mime: str | None = None
    mime_mismatch: bool = False
    source_sha256: str = ""
    text_truncated: bool = False

    escalated: bool = False
    """Whether an OCR converter was actually entered, not merely configured."""

    meaningful_chars: int = 0
    """Readable characters in :attr:`text` as delivered, counted after any cut.

    The count a caller's delivery floor is meant to be computed on, which is
    why it describes the string the caller received rather than the one the
    converter produced. ``attempts`` still carries the pre-cut count per
    converter, and ``text_truncated`` says a cut happened."""

    @property
    def text_chars(self) -> int:
        return len(self.text)

    @property
    def has_text(self) -> bool:
        """Whether any readable text came back. Not a synonym for success.

        Answered from the text rather than from the status, because the
        pipeline carries the best text any converter produced under *every*
        status. A thin text-layer read followed by a missing OCR extra is
        ``converter_unavailable`` and still holds the title page it found; the
        same read followed by an OCR pass that broke is ``failed`` and still
        holds it. Deriving this from the status told the caller nothing was
        found while the text sat in :attr:`text` unread, which is the same
        inversion the escalation threshold used to make.

        The division is: ``status`` says how the text was obtained and how far
        to trust it, this says whether there is any. Counted on the delivered
        string, so a cap that cuts everything readable is not called text."""
        return self.meaningful_chars > 0


def content_sha256(data: bytes) -> str:
    """Hash the bytes. The identity every cache key is built on."""
    return hashlib.sha256(data).hexdigest()


def meaningful_chars(text: str) -> int:
    """Count characters an agent could actually read.

    HTML comments go first because Docling collapses every visual it cannot
    read into ``<!-- image -->``. Thirty of those is 450 characters of nothing,
    and a threshold that counted them would call an unreadable document
    extracted. Whitespace goes next, so a document that is mostly line breaks
    is measured on its words.
    """
    stripped = _HTML_COMMENT.sub(" ", text)
    return len(_WHITESPACE.sub("", stripped))
