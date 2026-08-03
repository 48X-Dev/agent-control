"""Bytes in, text out, and an honest name for how honest the text is.

This module is a library and nothing else. It takes bytes and a declared MIME
type, runs at most two converters over them, and returns the text with a status
that says which converter produced it. It touches no database, opens no socket,
reads no setting and knows nothing about sessions, turns or namespaces. Storing
the result, scheduling the work and deciding what a status means for delivery
all belong to the caller.

**Why there is no ``ok``.** The plan's sidecar sketch had one and it would be
the most dangerous value in the enum. The measured failure mode on this
deployment's endpoint is an HTTP 200 with the file silently dropped: an agent
is told a spec is attached, receives nothing, and answers confidently from the
title. A text-layer extraction has the same shape. MarkItDown reading 733
characters out of a carousel PDF has not read the carousel, and calling that
``ok`` invites exactly one bug, once, in the one place nobody looks. So the
status is :attr:`ConversionStatus.TEXT_LAYER_EXTRACTED`, it is never called a
clean read, and :attr:`ConversionStatus.OCR_EXTRACTED` is a different value
because it was a different act.

**Why escalation is the normal path.** Section 8A measured this workspace's six
real attachments: one PDF with 733 characters of text layer, five PNGs with
zero each. MarkItDown returns nothing on all five and Docling's OCR returns
roughly 552 characters apiece in about twenty seconds. Five of six escalate.
The gigabyte is the main path, which is also why it is an optional extra whose
absence is a *stated status* rather than a crash: a deployment that has not
paid for it should be told what it is missing on every file, not surprised by
an ``ImportError`` at the first PNG.

**Why this cannot run inline.** One issue with five visuals is roughly a
hundred seconds of OCR against a twenty-five second per-step budget. The caller
runs this out of band, keyed by content hash, and a cache miss yields a stated
"not yet converted" descriptor rather than a wait. ``attachment_converter_cache``
holds the key that makes that possible; the store behind it is the caller's.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace

from agent_control_models.files import is_mime_mismatch, sniff_mime

from .attachment_converter_backends import (
    FAILURE_CONVERTER_ERROR,
    ConverterBackend,
    ConverterFailedError,
    ConverterKind,
    ConverterUnavailableError,
    ConverterUnsupportedError,
    EncryptedDocumentError,
    default_backends,
)
from .attachment_converter_types import (
    DEFAULT_CONVERTIBLE_MIMES,
    DEFAULT_OPTIONS,
    DEFAULT_TEXT_MAX_CHARS,
    FAILURE_EMPTY_INPUT,
    FAILURE_NO_CONVERTER_INSTALLED,
    FAILURE_OCR_CONVERTER_ABSENT,
    FAILURE_SOURCE_ECHOED,
    FAILURE_TYPE_NOT_CONVERTIBLE,
    LOW_TEXT_THRESHOLD_CHARS,
    AttemptOutcome,
    ConversionOptions,
    ConversionResult,
    ConversionStatus,
    ConverterAttempt,
    content_sha256,
    meaningful_chars,
)

__all__ = [
    "DEFAULT_CONVERTIBLE_MIMES",
    "DEFAULT_OPTIONS",
    "DEFAULT_TEXT_MAX_CHARS",
    "FAILURE_EMPTY_INPUT",
    "FAILURE_NO_CONVERTER_INSTALLED",
    "FAILURE_OCR_CONVERTER_ABSENT",
    "FAILURE_SOURCE_ECHOED",
    "FAILURE_TYPE_NOT_CONVERTIBLE",
    "LOW_TEXT_THRESHOLD_CHARS",
    "AttemptOutcome",
    "ConversionOptions",
    "ConversionResult",
    "ConversionStatus",
    "ConverterAttempt",
    "content_sha256",
    "convert_attachment",
    "convert_attachment_async",
    "meaningful_chars",
]

_ECHO_PROBE_BYTES = 8
"""Enough characters to carry the magic number of every type this library accepts."""

_ECHO_SOURCE_WINDOW = 256
"""How far into the file the echo probe is gathered from."""

_ECHO_TEXT_WINDOW = 1024
"""How much of a converter's output is examined. Each character of it came from
at least one source byte, so this window always covers the probe's window."""


def convert_attachment(
    data: bytes,
    *,
    declared_mime: str | None = None,
    options: ConversionOptions = DEFAULT_OPTIONS,
    backends: tuple[ConverterBackend, ...] | None = None,
) -> ConversionResult:
    """Convert one attachment's bytes to text.

    The declared type is advisory throughout. The magic bytes decide which
    parser runs and whether one runs at all, and the disagreement is reported
    rather than resolved silently.

    Never raises for a bad document. Every failure is a status and a code.
    """
    active = default_backends() if backends is None else backends
    sniffed = sniff_mime(data)
    base = ConversionResult(
        status=ConversionStatus.FAILED,
        sniffed_mime=sniffed,
        declared_mime=declared_mime,
        mime_mismatch=is_mime_mismatch(declared_mime, sniffed),
        source_sha256=content_sha256(data),
    )

    if not data:
        return replace(base, failure_code=FAILURE_EMPTY_INPUT)
    if sniffed is None or sniffed not in options.accepted_mimes:
        return replace(
            base,
            status=ConversionStatus.UNSUPPORTED_TYPE,
            failure_code=FAILURE_TYPE_NOT_CONVERTIBLE,
        )

    return _run_pipeline(data, mime=sniffed, options=options, backends=active, base=base)


async def convert_attachment_async(
    data: bytes,
    *,
    declared_mime: str | None = None,
    options: ConversionOptions = DEFAULT_OPTIONS,
    backends: tuple[ConverterBackend, ...] | None = None,
) -> ConversionResult:
    """Run :func:`convert_attachment` off the event loop.

    Conversion is CPU-bound for tens of seconds. Calling the synchronous
    function from a request handler would stall every other request in the
    process for the length of an OCR run, which is the defect the plan spends
    two paragraphs on for LibreOffice. A thread is not isolation and is not
    claimed to be; it is the difference between a slow conversion and a stopped
    server.
    """
    return await asyncio.to_thread(
        convert_attachment,
        data,
        declared_mime=declared_mime,
        options=options,
        backends=backends,
    )


@dataclass(slots=True)
class _Pass:
    """One converter's run and its text, while the pipeline is still deciding."""

    attempt: ConverterAttempt
    text: str = ""
    invoked: bool = False
    """Whether the converter's ``extract`` was entered at all.

    A backend reporting ``available() is False`` produces an attempt without
    ever being asked to read anything, and that difference is what
    ``escalated`` has to be computed from: a result flagged as escalated when
    the only OCR backend was absent overcounts every cost read off it."""


def _run_pipeline(
    data: bytes,
    *,
    mime: str,
    options: ConversionOptions,
    backends: tuple[ConverterBackend, ...],
    base: ConversionResult,
) -> ConversionResult:
    text_layer = [b for b in backends if b.kind is ConverterKind.TEXT_LAYER]
    ocr = [b for b in backends if b.kind is ConverterKind.OCR] if options.allow_ocr else []

    passes: list[_Pass] = []
    for backend in text_layer:
        result = _attempt(backend, data, mime=mime, options=options)
        passes.append(result)
        if result.attempt.outcome is AttemptOutcome.ENCRYPTED:
            return _finish(base, passes, options, status=ConversionStatus.ENCRYPTED)
        if result.attempt.outcome is AttemptOutcome.EXTRACTED:
            return _finish(base, passes, options, status=ConversionStatus.TEXT_LAYER_EXTRACTED)

    for backend in ocr:
        result = _attempt(backend, data, mime=mime, options=options)
        passes.append(result)
        if result.attempt.outcome is AttemptOutcome.ENCRYPTED:
            return _finish(base, passes, options, status=ConversionStatus.ENCRYPTED)
        if result.attempt.outcome is AttemptOutcome.EXTRACTED:
            return _finish(base, passes, options, status=ConversionStatus.OCR_EXTRACTED)

    return _finish(base, passes, options, status=_degraded_status(passes, _winner(passes)))


def _winner(passes: list[_Pass]) -> _Pass | None:
    """The pass that produced the most readable text, if any did.

    Ranked on meaningful characters rather than raw length, so a converter
    returning four hundred bytes of ``<!-- image -->`` never outranks one that
    returned a sentence.
    """
    return max(
        (p for p in passes if p.attempt.meaningful_chars > 0),
        key=lambda p: p.attempt.meaningful_chars,
        default=None,
    )


def _degraded_status(passes: list[_Pass], winner: _Pass | None) -> ConversionStatus:
    """Name what happened once no pass cleared the escalation threshold.

    Ordered so the most actionable answer wins. "Install the OCR extra" is a
    remedy and "this file is empty" is a fact about the file; reporting the fact
    when the remedy applies is how a missing gigabyte gets mistaken for a blank
    document. A converter that broke outranks a converter that read little for
    the same reason.

    **Thin text still counts as text, and this is the row measured rather than
    reasoned.** A real 1300x150 PNG carrying the headline ``BOARD REVIEW 2026``
    was run through both shipped libraries on 2026-08-03: MarkItDown returned
    zero meaningful characters, Docling's OCR returned the headline exactly, in
    34.1 seconds, and that is fifteen meaningful characters. Every word in the
    image was recovered. Reporting that as :attr:`ConversionStatus.EMPTY` would
    be this module's own opening argument made backwards - a status that tells
    the caller nothing was found while the text sits in the result unread. So
    the threshold decides *whether to escalate* and nothing else; once every
    converter has run, any meaningful text is named after the converter that
    found it. Short OCR output is still not a clean read, which is exactly what
    :attr:`ConversionStatus.OCR_EXTRACTED` already says.

    The caller keeps the floor it needs: ``meaningful_chars`` rides the result,
    so "fifteen characters is not worth delivering" stays a delivery decision.
    What a caller cannot do is recover text this function threw away.

    **The remedy still outranks the read, and the read still travels.** On a
    deployment carrying the OCR extra but not the text-layer one, an absent
    converter and a recovered image happen together: the status names the
    absence, because "install the missing extra" is the sentence an operator
    needs and the next file will hit the same hole. The recovered text is not
    lost to that choice - it rides in ``text`` with ``converter`` naming
    Docling and ``has_text`` reading true - which is the whole reason
    :attr:`ConversionResult.has_text` is no longer derived from the status.
    """
    outcomes = {p.attempt.outcome for p in passes}
    if not passes or AttemptOutcome.UNAVAILABLE in outcomes:
        return ConversionStatus.CONVERTER_UNAVAILABLE
    if AttemptOutcome.FAILED in outcomes:
        return ConversionStatus.FAILED
    if winner is not None:
        return (
            ConversionStatus.OCR_EXTRACTED
            if winner.attempt.kind is ConverterKind.OCR
            else ConversionStatus.TEXT_LAYER_EXTRACTED
        )
    if AttemptOutcome.UNSUPPORTED in outcomes:
        return ConversionStatus.UNSUPPORTED_TYPE
    return ConversionStatus.EMPTY


def _attempt(
    backend: ConverterBackend,
    data: bytes,
    *,
    mime: str,
    options: ConversionOptions,
) -> _Pass:
    """Run one converter and describe what it did, without ever raising.

    ``available()`` is inside the guard too. The two shipped adapters answer it
    from a spec lookup that cannot raise, but this function's contract with the
    pipeline is that no backend escapes it as an exception, and a third-party
    backend is not covered by what the shipped two happen to do.
    """
    started = time.monotonic()
    try:
        ready = backend.available()
    except Exception:
        return _failed_pass(
            backend, AttemptOutcome.FAILED, FAILURE_CONVERTER_ERROR, started, invoked=False
        )

    if not ready:
        return _Pass(
            attempt=ConverterAttempt(
                name=backend.name,
                kind=backend.kind,
                outcome=AttemptOutcome.UNAVAILABLE,
                failure_code=_absent_code(backend),
            )
        )

    try:
        text = backend.extract(data, mime=mime)
    except EncryptedDocumentError as exc:
        return _failed_pass(backend, AttemptOutcome.ENCRYPTED, exc.code, started)
    except ConverterUnavailableError as exc:
        return _failed_pass(backend, AttemptOutcome.UNAVAILABLE, exc.code, started)
    except ConverterUnsupportedError as exc:
        return _failed_pass(backend, AttemptOutcome.UNSUPPORTED, exc.code, started)
    except ConverterFailedError as exc:
        return _failed_pass(backend, AttemptOutcome.FAILED, exc.code, started)
    except Exception:
        return _failed_pass(backend, AttemptOutcome.FAILED, FAILURE_CONVERTER_ERROR, started)

    if text is None:
        text = ""
    if not isinstance(text, str):
        # A backend that breaks its own ``-> str`` contract has not converted
        # anything, and finding out through an AttributeError two lines later
        # would break this function's promise never to raise.
        return _failed_pass(backend, AttemptOutcome.FAILED, FAILURE_CONVERTER_ERROR, started)
    if _is_source_echoed(text, data):
        return _failed_pass(backend, AttemptOutcome.FAILED, FAILURE_SOURCE_ECHOED, started)

    counted = meaningful_chars(text)
    usable = counted >= options.low_text_threshold_chars
    return _Pass(
        attempt=ConverterAttempt(
            name=backend.name,
            kind=backend.kind,
            outcome=AttemptOutcome.EXTRACTED if usable else AttemptOutcome.LOW_TEXT,
            text_chars=len(text),
            meaningful_chars=counted,
            duration_seconds=time.monotonic() - started,
        ),
        text=text,
        invoked=True,
    )


def _ascii_skeleton(raw: bytes) -> bytes:
    """The printable ASCII of some bytes, in order, everything else dropped."""
    return bytes(c for c in raw if 32 <= c < 127)


def _is_source_echoed(text: str, data: bytes) -> bool:
    """Whether a converter handed back its input instead of converting it.

    **Measured, on 2026-08-03, against MarkItDown 0.1.7.** Given
    ``b"%PDF-1.4\\nthis is not a document"`` and a PDF stream info, it does not
    raise: it falls through to its plain-text converter and returns the file's
    own bytes as the extraction, header included. A longer corrupt body returns
    156 characters of the same, which clears every text threshold there is.

    That is the exact failure this library exists to refuse, wearing the
    opposite mask. An agent is told it is reading a specification and receives
    ``%PDF-1.4`` followed by whatever ASCII a malformed - or deliberately
    constructed - file happened to contain, under a status claiming a
    successful text-layer read. Nothing downstream could tell the difference,
    because the status and the character count both look healthy.

    **Why the bytes are not compared directly.** The first version of this
    round-tripped the output back through latin-1 and compared eight bytes,
    which three attacker-chosen bytes defeat. Only ``%PDF-`` is needed for the
    sniffer to route a file as a PDF; bytes five to seven are free, and a
    charset detector decodes ``\\x80\\x91\\x92`` to codepoints outside latin-1
    that ``errors="ignore"`` then silently dropped, so the prefix no longer
    matched and the file's own body arrived as a ``text_layer_extracted`` read
    with a healthy character count and no failure code - and with the sniff
    agreeing with the declared type, nothing else on the result flagged it.
    Reproduced against the real library on both byte triples on 2026-08-03. So
    the comparison is made on the *printable ASCII
    skeleton* of both sides: whatever a decoder did with the bytes it could not
    map, the ASCII it could map survives in order on both sides. A file with
    fewer than eight printable bytes in its first ``_ECHO_SOURCE_WINDOW``
    carries too short a probe to distinguish an echo from a coincidence, so it
    falls back to the exact byte comparison, which is what catches a faithful
    echo of a wholly binary file.

    Narrow on purpose either way: the output must *begin with* the input's own
    opening bytes. Text that merely quotes ``%PDF`` further in is a real
    extraction. A converter that echoes has failed, so it is recorded as a
    failure and its text is discarded rather than delivered.
    """
    if not data:
        return False
    head = text[:_ECHO_TEXT_WINDOW]
    if head.encode("latin-1", errors="ignore").startswith(data[:_ECHO_PROBE_BYTES]):
        return True
    probe = _ascii_skeleton(data[:_ECHO_SOURCE_WINDOW])[:_ECHO_PROBE_BYTES]
    if len(probe) < _ECHO_PROBE_BYTES:
        return False
    return _ascii_skeleton(head.encode("utf-8", errors="ignore")).startswith(probe)


def _absent_code(backend: ConverterBackend) -> str:
    if backend.kind is ConverterKind.OCR:
        return FAILURE_OCR_CONVERTER_ABSENT
    return FAILURE_NO_CONVERTER_INSTALLED


def _failed_pass(
    backend: ConverterBackend,
    outcome: AttemptOutcome,
    code: str,
    started: float,
    *,
    invoked: bool = True,
) -> _Pass:
    return _Pass(
        attempt=ConverterAttempt(
            name=backend.name,
            kind=backend.kind,
            outcome=outcome,
            failure_code=code,
            duration_seconds=time.monotonic() - started,
        ),
        invoked=invoked,
    )


_STATUS_OUTCOME = {
    ConversionStatus.CONVERTER_UNAVAILABLE: AttemptOutcome.UNAVAILABLE,
    ConversionStatus.FAILED: AttemptOutcome.FAILED,
    ConversionStatus.ENCRYPTED: AttemptOutcome.ENCRYPTED,
    ConversionStatus.UNSUPPORTED_TYPE: AttemptOutcome.UNSUPPORTED,
}


def _finish(
    base: ConversionResult,
    passes: list[_Pass],
    options: ConversionOptions,
    *,
    status: ConversionStatus,
) -> ConversionResult:
    """Assemble the result, carrying the best text any converter produced.

    "Best" is the most meaningful characters, not the last run. A failed OCR
    escalation must not discard the thin text the first pass did find: a
    hundred characters of title page is worth more to an agent than nothing,
    provided the status says plainly that it is all there is. ``has_text`` is
    then read off the text rather than off the status, so those rows do not
    deny carrying what they carry.

    The reported count is recomputed after the cut. It is the number a caller's
    delivery floor is computed on, and a floor applied to characters the caller
    never received is a floor applied to the wrong string. The per-converter
    counts in ``attempts`` still describe what each parser produced.
    """
    winner = _winner(passes)
    text = winner.text if winner else ""
    truncated = len(text) > options.text_max_chars
    if truncated:
        text = text[: options.text_max_chars]
        counted = meaningful_chars(text)
    else:
        counted = winner.attempt.meaningful_chars if winner else 0

    return replace(
        base,
        status=status,
        text=text,
        converter=winner.attempt.name if winner else None,
        attempts=tuple(p.attempt for p in passes),
        failure_code=_result_failure_code(status, passes),
        text_truncated=truncated,
        escalated=any(p.invoked and p.attempt.kind is ConverterKind.OCR for p in passes),
        meaningful_chars=counted,
    )


def _result_failure_code(status: ConversionStatus, passes: list[_Pass]) -> str | None:
    """The code belonging to the attempt the status was decided on.

    Taking the last code instead would report a text-layer parser's complaint
    as the reason an OCR escalation was unavailable, which sends whoever reads
    it to the wrong remedy.
    """
    wanted = _STATUS_OUTCOME.get(status)
    if wanted is None:
        return None
    codes = [
        p.attempt.failure_code
        for p in passes
        if p.attempt.outcome is wanted and p.attempt.failure_code
    ]
    return codes[-1] if codes else None
