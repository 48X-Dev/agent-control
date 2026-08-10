"""Bytes in, text out, and an honest name for how honest the text is.

A library and nothing else: no database, no socket, no settings, and no ``ok`` status.
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
from .attachment_converter_containers import refine_container_mime
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
"""How much of a converter's output is examined."""


def convert_attachment(
    data: bytes,
    *,
    declared_mime: str | None = None,
    options: ConversionOptions = DEFAULT_OPTIONS,
    backends: tuple[ConverterBackend, ...] | None = None,
) -> ConversionResult:
    """Convert one attachment's bytes to text; the magic bytes decide, never the declared type."""
    active = default_backends() if backends is None else backends
    sniffed = refine_container_mime(data, sniff_mime(data))
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
    """Run :func:`convert_attachment` off the event loop; it is CPU-bound for tens of seconds."""
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

    What ``escalated`` is computed from: an absent OCR backend never ran."""


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
    """The pass that produced the most readable text, ranked on meaningful chars."""
    return max(
        (p for p in passes if p.attempt.meaningful_chars > 0),
        key=lambda p: p.attempt.meaningful_chars,
        default=None,
    )


def _degraded_status(passes: list[_Pass], winner: _Pass | None) -> ConversionStatus:
    """Name what happened once no pass cleared the escalation threshold, remedy first."""
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
    """Run one converter and describe what it did, without ever raising."""
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
    """Whether a converter echoed its input, compared on the printable ASCII skeleton."""
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
    """Assemble the result, carrying the most meaningful characters any pass produced."""
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
    """The code belonging to the attempt the status was decided on, never the last one."""
    wanted = _STATUS_OUTCOME.get(status)
    if wanted is None:
        return None
    codes = [
        p.attempt.failure_code
        for p in passes
        if p.attempt.outcome is wanted and p.attempt.failure_code
    ]
    return codes[-1] if codes else None
