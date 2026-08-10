"""Resolving a ZIP container to the Office document it actually holds.

Structural, not a guess: reading the central directory decompresses nothing.
"""

from __future__ import annotations

import io
import logging
import zipfile

_logger = logging.getLogger(__name__)

OOXML_PRESENTATION = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
OOXML_DOCUMENT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OOXML_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The root directory that names the kind, and the type it means.
_OOXML_BY_ROOT_DIRECTORY = {
    "ppt": OOXML_PRESENTATION,
    "word": OOXML_DOCUMENT,
    "xl": OOXML_SHEET,
}

_OOXML_MANIFEST = "[Content_Types].xml"
_OOXML_SCAN_ENTRIES = 4096


def refine_container_mime(data: bytes | None, sniffed: str | None) -> str | None:
    """Return the OOXML type a ZIP holds, or ``sniffed`` unchanged; total by construction."""
    if sniffed != "application/zip" or not data:
        return sniffed

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except Exception as exc:
        # A ZIP magic number with no readable directory behind it. The upload
        # gate refuses it as application/zip, which is the honest answer.
        _logger.debug("ZIP container not readable (%s)", type(exc).__name__)
        return sniffed

    if _OOXML_MANIFEST not in names:
        return sniffed

    for name in names[:_OOXML_SCAN_ENTRIES]:
        root, separator, _ = name.partition("/")
        if separator and root in _OOXML_BY_ROOT_DIRECTORY:
            return _OOXML_BY_ROOT_DIRECTORY[root]
    return sniffed
