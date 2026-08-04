"""The two converters, behind one interface, each importable or absent.

Two libraries do the work and they are not interchangeable. MarkItDown reads a
document's *text layer*: fast, cheap, and worth nothing on a picture of words.
Docling runs layout analysis and OCR: it reads the picture, and it costs a
gigabyte of install and tens of seconds per file. Section 8A of the plan
measured both against this workspace's real attachments and five of six carried
no text layer at all, so OCR is the main path here rather than a rare
escalation.

Three rules hold for both adapters.

**Neither is a hard dependency.** Each is imported inside the call that needs
it and each reports ``available()`` from a spec lookup that imports nothing. A
deployment without them converts nothing and says so; it does not fail to
start and it does not raise ``ImportError`` at a caller.

**No upstream text ever leaves this module.** A parser traceback carries
document content, and a converter failure that echoes it into an operator
console would undo the point of running the parser away from everything else.
Failures leave as a code from a hand-written constant, following the same
discipline ``services/executor_client.py`` keeps.

**Routing is by sniffed type, never by the caller's filename.** The extension
handed to a converter is derived from the magic bytes. A file named
``invoice.pdf`` that is really a PNG is converted as a PNG, and the display
name never reaches a parser's format-detection path at all.
"""

from __future__ import annotations

import importlib.util
import io
import threading
from enum import StrEnum
from typing import Any, Protocol

MARKITDOWN_BACKEND_NAME = "markitdown"
DOCLING_BACKEND_NAME = "docling"

FAILURE_CONVERTER_ERROR = "converter_error"
FAILURE_CONVERTER_ABSENT = "converter_absent"
FAILURE_FORMAT_SUPPORT_MISSING = "format_support_missing"
FAILURE_UNSUPPORTED_BY_CONVERTER = "unsupported_by_converter"
FAILURE_ENCRYPTED_DOCUMENT = "encrypted_document"

_EXTENSION_BY_MIME = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
"""Sniffable types this module knows how to hand to a converter.

Deliberately the set ``sniff_mime`` can name and the plan's
``attachment_accepted_mimes`` admits. A type absent here is refused before any
parser is constructed."""

_ENCRYPTION_HINTS = ("password", "encrypted", "decrypt")


class ConverterKind(StrEnum):
    """What a converter is able to read.

    The distinction decides escalation order and it decides the status the
    caller reports, which is why it is a property of the backend rather than a
    fact the pipeline hard-codes about two names.
    """

    TEXT_LAYER = "text_layer"
    OCR = "ocr"


class ConverterUnavailableError(Exception):
    """The library, or the format support inside it, is not installed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ConverterUnsupportedError(Exception):
    """The library is present and refuses this format."""

    def __init__(self, code: str = FAILURE_UNSUPPORTED_BY_CONVERTER) -> None:
        super().__init__(code)
        self.code = code


class ConverterFailedError(Exception):
    """The conversion broke. Carries a code and no upstream text, ever."""

    def __init__(self, code: str = FAILURE_CONVERTER_ERROR) -> None:
        super().__init__(code)
        self.code = code


class EncryptedDocumentError(Exception):
    """The document is password-protected, so no converter will read it.

    Separate from :class:`ConverterFailedError` because it changes what the caller
    does: escalating an encrypted PDF to OCR spends a minute of CPU to learn
    the same thing the text-layer pass already learned.
    """

    def __init__(self, code: str = FAILURE_ENCRYPTED_DOCUMENT) -> None:
        super().__init__(code)
        self.code = code


class ConverterBackend(Protocol):
    """One converter, as the pipeline sees it."""

    name: str
    kind: ConverterKind

    def available(self) -> bool:
        """Report whether this converter can run, importing nothing."""
        ...

    def extract(self, data: bytes, *, mime: str) -> str:
        """Return this document's text, or raise one of the errors above."""
        ...


def _module_installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _looks_encrypted(error: BaseException) -> bool:
    """Classify a parser error as an encryption refusal.

    The message is read here and discarded here. What escapes is a boolean.
    """
    text = f"{type(error).__name__} {error}".lower()
    return any(hint in text for hint in _ENCRYPTION_HINTS)


class MarkItDownBackend:
    """MarkItDown 0.1.7 (MIT), the text-layer pass.

    Plugins are disabled explicitly rather than left at their default. A
    converter that loads third-party entry points is a converter whose parser
    set depends on what else is installed in the image, which is not a property
    anyone wants to discover from a changed extraction.
    """

    name = MARKITDOWN_BACKEND_NAME
    kind = ConverterKind.TEXT_LAYER

    def available(self) -> bool:
        return _module_installed("markitdown")

    def extract(self, data: bytes, *, mime: str) -> str:
        extension = _EXTENSION_BY_MIME.get(mime)
        if extension is None:
            raise ConverterUnsupportedError()
        try:
            from markitdown import (  # noqa: PLC0415
                MarkItDown,
                MissingDependencyException,
                StreamInfo,
                UnsupportedFormatException,
            )
        except ImportError as exc:
            raise ConverterUnavailableError(FAILURE_CONVERTER_ABSENT) from exc

        converter = MarkItDown(enable_builtins=True, enable_plugins=False)
        stream_info = StreamInfo(mimetype=mime, extension=extension)
        try:
            result = converter.convert_stream(io.BytesIO(data), stream_info=stream_info)
        except MissingDependencyException as exc:
            raise ConverterUnavailableError(FAILURE_FORMAT_SUPPORT_MISSING) from exc
        except UnsupportedFormatException as exc:
            raise ConverterUnsupportedError() from exc
        except Exception as exc:
            if _looks_encrypted(exc):
                raise EncryptedDocumentError() from exc
            raise ConverterFailedError() from exc
        return result.text_content or ""


class DoclingBackend:
    """Docling (MIT), the OCR and layout pass.

    Two costs are paid here and both are deliberate.

    The converter object loads a layout model, so it is built once per process
    and reused. Building one per document would add that load to every file.

    Extraction is serialized on a module lock. Docling's thread safety is not
    documented and was not verified, and the plan already bounds concurrent
    conversions at two, so serializing costs a queue rather than throughput
    that was ever promised. The caller runs this out of band, which is what
    makes the queue affordable.
    """

    name = DOCLING_BACKEND_NAME
    kind = ConverterKind.OCR

    def available(self) -> bool:
        return _module_installed("docling")

    def extract(self, data: bytes, *, mime: str) -> str:
        extension = _EXTENSION_BY_MIME.get(mime)
        if extension is None:
            raise ConverterUnsupportedError()
        converter = _docling_converter()
        try:
            from docling.datamodel.base_models import DocumentStream  # noqa: PLC0415
        except ImportError as exc:
            raise ConverterUnavailableError(FAILURE_CONVERTER_ABSENT) from exc

        source = DocumentStream(name=f"attachment{extension}", stream=io.BytesIO(data))
        with _docling_lock:
            try:
                result = converter.convert(source)
            except Exception as exc:
                if _looks_encrypted(exc):
                    raise EncryptedDocumentError() from exc
                raise ConverterFailedError() from exc
        document = getattr(result, "document", None)
        if document is None:
            raise ConverterFailedError()
        try:
            return document.export_to_markdown() or ""
        except Exception as exc:
            raise ConverterFailedError() from exc


_docling_lock = threading.Lock()
_docling_build_lock = threading.Lock()
_docling_converter_instance: Any | None = None
_docling_build_failure: str | None = None


def _docling_converter() -> Any:
    """Build the shared Docling converter on first use, or repeat why it failed.

    The failure is remembered as well as the success. Building this loads a
    torch layout model - 59.8 seconds against a cold weight cache, 13.0 for a
    build and a one-line PDF together once the weights are on disk - and it
    happens while every other conversion waits on ``_docling_build_lock``. A
    build that fails for a reason the next document will not change - no room
    for the model weights, a poisoned cache - would otherwise re-attempt that
    per file and serialize the whole queue behind each attempt, on the path
    that is the main path for five of this workspace's six attachments. One
    slow failure, then fast ones. ``reset_backend_caches`` clears both.
    """
    global _docling_converter_instance, _docling_build_failure
    with _docling_build_lock:
        if _docling_build_failure is not None:
            raise _build_failure(_docling_build_failure)
        if _docling_converter_instance is None:
            try:
                from docling.document_converter import DocumentConverter  # noqa: PLC0415
            except ImportError as exc:
                _docling_build_failure = FAILURE_CONVERTER_ABSENT
                raise ConverterUnavailableError(FAILURE_CONVERTER_ABSENT) from exc
            try:
                _docling_converter_instance = DocumentConverter()
            except Exception as exc:
                _docling_build_failure = FAILURE_CONVERTER_ERROR
                raise ConverterFailedError() from exc
        return _docling_converter_instance


def _build_failure(code: str) -> Exception:
    if code == FAILURE_CONVERTER_ABSENT:
        return ConverterUnavailableError(code)
    return ConverterFailedError(code)


def reset_backend_caches() -> None:
    """Forget the shared Docling converter and any remembered build failure.

    For tests, and for nothing else."""
    global _docling_converter_instance, _docling_build_failure
    with _docling_build_lock:
        _docling_converter_instance = None
        _docling_build_failure = None


def default_backends() -> tuple[ConverterBackend, ...]:
    """The shipped pair, text layer first."""
    return (MarkItDownBackend(), DoclingBackend())
