"""Structured data to OOXML bytes: three typed builders, no generic write.

The caller chooses content, these choose encoding. Nothing here touches disk,
the network or a filename. See docs/plans/agent-file-outputs.md section 4.6.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from io import BytesIO
from typing import Any

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

BUILDER_MODULES = ("openpyxl", "docx", "pptx")
"""Import names of the three libraries, in the order the tools are listed."""

# Excel reads a leading =, +, - or @ as a formula, so a model's cell text would
# execute in a spreadsheet a person opens. Those cells are forced to text.
_FORMULA_LEADERS = ("=", "+", "-", "@")

_SHEET_TITLE_MAX = 31
_SHEET_TITLE_BANNED = str.maketrans({character: "-" for character in "[]:*?/\\"})

# A plain decimal only. "007" and "1-2" stay text rather than being rewritten.
_NUMERIC = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


class BuilderUnavailableError(RuntimeError):
    """A builder library is not installed in this executor image."""


def missing_libraries() -> list[str]:
    """Which of the three libraries this process cannot import."""
    from importlib.util import find_spec

    return [name for name in BUILDER_MODULES if find_spec(name) is None]


def build_xlsx(*, sheet_name: str, header: list[str], rows: list[list[str]]) -> bytes:
    """One worksheet: a bold header row, then the data rows."""
    openpyxl = _load("openpyxl")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = _sheet_title(sheet_name)

    if header:
        sheet.append([str(name) for name in header])
        for cell in sheet[1]:
            cell.font = openpyxl.styles.Font(bold=True)
        _defuse_formulas(sheet[1])

    for row in rows:
        sheet.append([_cell_value(value) for value in row])
        _defuse_formulas(sheet[sheet.max_row])

    return _to_bytes(workbook)


def build_docx(*, title: str, sections: list[list[str]]) -> bytes:
    """A titled document; each section is a heading followed by its paragraphs."""
    docx = _load("docx")

    document = docx.Document()
    if title.strip():
        document.add_heading(title.strip(), level=0)

    for section in sections:
        if not section:
            continue
        heading, *paragraphs = (str(part) for part in section)
        if heading.strip():
            document.add_heading(heading.strip(), level=1)
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)

    return _to_bytes(document)


def build_pptx(*, title: str, slides: list[list[str]]) -> bytes:
    """A title slide, then one bulleted slide per entry."""
    pptx = _load("pptx")

    presentation = pptx.Presentation()
    if title.strip():
        opening = presentation.slides.add_slide(presentation.slide_layouts[0])
        opening.shapes.title.text = title.strip()

    for slide in slides:
        if not slide:
            continue
        heading, *bullets = (str(part) for part in slide)
        added = presentation.slides.add_slide(presentation.slide_layouts[1])
        added.shapes.title.text = heading
        frame = added.placeholders[1].text_frame
        frame.text = bullets[0] if bullets else ""
        for bullet in bullets[1:]:
            frame.add_paragraph().text = bullet

    return _to_bytes(presentation)


def _load(module_name: str) -> Any:
    """Import a builder library on use, naming it when it is absent."""
    from importlib import import_module

    try:
        if module_name == "openpyxl":
            import_module("openpyxl.styles")
        return import_module(module_name)
    except ImportError as exc:
        raise BuilderUnavailableError(module_name) from exc


def _to_bytes(saveable: Any) -> bytes:
    """Serialize a workbook, document or presentation into memory."""
    buffer = BytesIO()
    saveable.save(buffer)
    return buffer.getvalue()


def _sheet_title(name: str) -> str:
    """A worksheet title Excel accepts, or the fallback when nothing is left."""
    cleaned = name.translate(_SHEET_TITLE_BANNED).strip()[:_SHEET_TITLE_MAX]
    return cleaned or "Data"


def _defuse_formulas(cells: Iterable[Any]) -> None:
    """Force every cell Excel would read as a formula back to text."""
    for cell in cells:
        if isinstance(cell.value, str) and cell.value.startswith(_FORMULA_LEADERS):
            cell.data_type = "s"


def _cell_value(value: object) -> object:
    """Numbers stay numbers so the column sorts; everything else is text."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return value
    text = str(value)
    stripped = text.strip()
    if not _NUMERIC.match(stripped):
        return text
    return float(stripped) if "." in stripped else int(stripped)
