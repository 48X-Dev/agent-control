"""What a conversion result *is*, separated from the machinery that fills it in.

Nothing here makes a decision. Import from ``attachment_converter``, not from
here: every name below is re-exported there.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from .attachment_converter_backends import ConverterKind

LOW_TEXT_THRESHOLD_CHARS = 40
"""Below this many meaningful characters, a converter's read triggers the next one.

An escalation trigger and not a delivery floor: it stops mattering once there
is nothing left to escalate to."""

DEFAULT_TEXT_MAX_CHARS = 2_560_000
"""The plan's ``attachment_text_max_chars``. A cap, not a policy: exceeding it
sets ``text_truncated`` and the caller decides what that is worth."""

DEFAULT_CONVERTIBLE_MIMES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        # OOXML. Sniffing sees application/zip; the server resolves the real type
        # structurally in attachment_containers before anything reaches here.
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
"""The types this library will hand to a parser.

Must change together with ``settings.attachment_accepted_mimes`` and the
``text-extraction`` extras: admitting a type without a parser stores a file and
then refuses to read it."""

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

    The losing attempt is what explains a twenty-second conversion.
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

    ``attempts`` still carries the pre-cut count per converter."""

    @property
    def text_chars(self) -> int:
        return len(self.text)

    @property
    def has_text(self) -> bool:
        """Whether any readable text came back. Not a synonym for success.

        Answered from the text rather than the status, because every status
        carries the best text any converter produced."""
        return self.meaningful_chars > 0


def content_sha256(data: bytes) -> str:
    """Hash the bytes. The identity every cache key is built on."""
    return hashlib.sha256(data).hexdigest()


def meaningful_chars(text: str) -> int:
    """Count characters an agent could actually read.

    HTML comments go first: Docling collapses every unreadable visual into
    ``<!-- image -->``, and counting those calls an empty document extracted.
    """
    stripped = _HTML_COMMENT.sub(" ", text)
    return len(_WHITESPACE.sub("", stripped))
