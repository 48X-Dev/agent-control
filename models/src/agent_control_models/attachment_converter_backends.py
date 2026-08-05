"""The two converters, behind one interface, each importable or absent.

Neither is a hard dependency: imported inside the call, and absence is a stated status.
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

A type absent here is refused before any parser is constructed."""

_ENCRYPTION_HINTS = ("password", "encrypted", "decrypt")


class ConverterKind(StrEnum):
    """What a converter is able to read. Decides escalation order and status."""

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
    """Password-protected, so escalating to OCR would learn nothing new."""

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
    """Classify a parser error as an encryption refusal. Only a boolean escapes."""
    text = f"{type(error).__name__} {error}".lower()
    return any(hint in text for hint in _ENCRYPTION_HINTS)


class MarkItDownBackend:
    """MarkItDown 0.1.7 (MIT), the text-layer pass; plugins are disabled explicitly."""

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
    """Docling (MIT), the OCR pass; serialized on a module lock, its thread safety undocumented."""

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
    """Build the shared Docling converter on first use, or repeat why it failed."""
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

    For tests, and nothing else."""
    global _docling_converter_instance, _docling_build_failure
    with _docling_build_lock:
        _docling_converter_instance = None
        _docling_build_failure = None


_MARKITDOWN_FORMAT_MODULES = ("mammoth", "openpyxl", "pandas", "pdfminer", "pptx")
"""The libraries that decide which formats an *installed* MarkItDown can open.

``available()`` answers for the package and says nothing about its per-format
extras: MarkItDown without ``pptx`` imports cleanly and then raises on the
first deck, which leaves this module as ``format_support_missing``. Anything
deciding whether a stored capability-absent failure still holds has to see
these modules too, or a rebuild that adds a parser inside an already-installed
converter changes nothing it can observe. The names are markitdown 0.1.7's own
lazy imports for the formats this server accepts - ``mammoth`` for docx,
``pandas`` for xlsx, ``pdfminer`` for pdf, ``pptx`` for pptx - plus
``openpyxl``, which ships in the same xlsx extra and breaks the same format one
import later."""


def installed_format_support() -> tuple[str, ...]:
    """The format-support modules present right now. Spec lookups, importing nothing."""
    return tuple(module for module in _MARKITDOWN_FORMAT_MODULES if _module_installed(module))


def default_backends() -> tuple[ConverterBackend, ...]:
    """The shipped pair, text layer first."""
    return (MarkItDownBackend(), DoclingBackend())
