"""Resolving a ZIP container to the Office document it actually holds.

``agent_control_models.files.sniff_mime`` stops at ``application/zip`` on
purpose, and its docstring forbids going further: it is shared with the SDK, so
a parser added there ships inside the executor process. This module is the
"somewhere with its own memory limit" that docstring points at. It runs
server-side only, after the byte ceiling has already bounded the input.

The refinement is structural, not a guess. An OOXML file is a ZIP carrying
``[Content_Types].xml`` at the root plus exactly one of three well-known top
level directories, and that directory is what names the kind. Reading the
central directory does not decompress anything, so the classic
decompression-bomb shape is not reachable from here.

What bounds this is ``attachment_max_bytes`` upstream, not the entry cap below.
``zipfile`` reads the whole central directory when it opens the archive, so the
cap trims the scan and not the read; it is honest about being a scan bound. A
twenty-megabyte archive cannot carry an unbounded number of entries.
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
    """Return the OOXML type a ZIP holds, or ``sniffed`` unchanged.

    Total by construction. A malformed archive, a ZIP that is genuinely just a
    ZIP, or anything that is not a ZIP at all leaves ``sniffed`` alone, because
    refusing a file is this function failing safe and misnaming one is not.
    """
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
