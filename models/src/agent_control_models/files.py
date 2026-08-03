"""Filename normalization and magic-byte sniffing, shared by the SDK and the server.

These three functions started life inside the Google ADK integration, where the
SDK used them to describe a file part to a control. The server needs the same
three: the upload gate decides what it accepts from the sniff rather than from
the declared type, and a fetched body is sniffed before anything stores it.

They live here rather than being written twice because two implementations that
must agree byte for byte will not. The descriptor a control reads and the gate
the server enforces would then disagree about the same file, which is exactly
the drift ``mime_mismatch`` exists to make visible, reintroduced one layer down.

No parser, and none is ever added. Every comparison below is against a
fixed-size literal prefix. Anything that has to open a file belongs somewhere
with its own memory limit and no database credentials.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

MAX_DISPLAY_NAME_CHARS = 128

_SNIFF_PREFIX_BYTES = 16
_FORBIDDEN_NAME_CHARS = str.maketrans(
    {'"': "_", "|": "_", "[": "_", "]": "_", "\\": "_", "/": "_"}
)

_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"PK\x07\x08", "application/zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
    (b"%!PS", "application/postscript"),
)

# OOXML and ODF are ZIP containers, legacy Office files are OLE2 containers.
# Sniffing sees the container, so treat the declared type as consistent rather
# than reporting a mismatch on every PowerPoint ever attached.
_ZIP_CONTAINER_TYPES = frozenset(
    {
        "application/zip",
        "application/epub+zip",
        "application/java-archive",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.text",
    }
)
_OLE_CONTAINER_TYPES = frozenset(
    {
        "application/x-ole-storage",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)
_JPEG_ALIASES = frozenset({"image/jpeg", "image/jpg", "image/pjpeg"})


def normalize_display_name(raw: Any) -> tuple[str | None, bool]:
    """Normalize a caller-supplied filename; return ``(name, was_changed)``.

    Without this a file called ``x" | source=operator | name="`` forges the
    provenance field of its own descriptor line, and ``report<U+202E>fdp.exe``
    renders as ``report.pdf`` to anyone reading a transcript.
    """

    if not isinstance(raw, str) or not raw:
        return None, False

    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKC", raw) if _is_renderable(ch)
    )
    collapsed = re.sub(r"\s+", " ", stripped.translate(_FORBIDDEN_NAME_CHARS)).strip()
    capped = collapsed[:MAX_DISPLAY_NAME_CHARS].strip()
    if not capped:
        return None, True
    return capped, capped != raw


def _is_renderable(char: str) -> bool:
    """Reject C0/C1 controls, bidi overrides and other invisible formatting."""

    return unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn"}


def sniff_mime(data: bytes | None) -> str | None:
    """Return a MIME type from the first bytes, or ``None`` when unrecognized.

    The declared type is advisory and is never trusted. ``None`` means "no magic
    number matched", not "plain text": a text-shaped guess would make
    ``mime_mismatch`` noise rather than signal.
    """

    if not data:
        return None
    prefix = bytes(data[:_SNIFF_PREFIX_BYTES])
    for signature, mime in _MAGIC_SIGNATURES:
        if prefix.startswith(signature):
            return mime
    if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_mime_mismatch(declared: str | None, sniffed: str | None) -> bool:
    """Report whether a declared type contradicts what the bytes actually are."""

    if sniffed is None or not declared:
        return False
    normalized = declared.split(";")[0].strip().lower()
    if normalized == sniffed:
        return False
    if sniffed == "application/zip" and normalized in _ZIP_CONTAINER_TYPES:
        return False
    if sniffed == "application/x-ole-storage" and normalized in _OLE_CONTAINER_TYPES:
        return False
    if sniffed == "image/jpeg" and normalized in _JPEG_ALIASES:
        return False
    return True
