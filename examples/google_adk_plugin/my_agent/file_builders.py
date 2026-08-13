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


def build_pptx(*, title: str, slides: list[list[str]], template: str | None = None) -> bytes:
    """A title slide, then one bulleted slide per entry, on the brand template if there is one."""
    pptx = _load("pptx")

    presentation = pptx.Presentation(template) if template else pptx.Presentation()
    layout = _pptx_layout(presentation)

    if title.strip():
        _pptx_slide(pptx, presentation, layout, title.strip(), ())

    for slide in slides:
        if not slide:
            continue
        heading, *bullets = [str(cell) for cell in slide]
        _pptx_slide(pptx, presentation, layout, heading, bullets)

    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _pptx_layout(presentation: Any) -> Any:
    """A layout with a title and a body if the template has one, else its first.

    A designed deck is often one blank layout with hand-placed boxes and no
    placeholders at all, which is what EarlyCore's own template turned out to
    be. Falling back to the first layout keeps the theme, fonts and slide size;
    :func:`_pptx_slide` then places the text itself.
    """
    layouts = list(presentation.slide_layouts)
    if not layouts:
        return None
    for layout in layouts:
        indexes = {placeholder.placeholder_format.idx for placeholder in layout.placeholders}
        if {0, 1} <= indexes:
            return layout
    return layouts[0]


def _pptx_slide(
    pptx: Any, presentation: Any, layout: Any, heading: str, bullets: Iterable[str]
) -> None:
    """One slide, filling placeholders when the layout has them and boxing the text when not."""
    slide = presentation.slides.add_slide(layout)
    body_text = [str(bullet) for bullet in bullets]

    placeholders = {
        placeholder.placeholder_format.idx: placeholder for placeholder in slide.placeholders
    }
    if 0 in placeholders:
        placeholders[0].text = heading
        if 1 in placeholders:
            frame = placeholders[1].text_frame
            frame.text = body_text[0] if body_text else ""
            for bullet in body_text[1:]:
                frame.add_paragraph().text = bullet
        return

    # No placeholders: measure from the slide rather than assume 4:3, so the
    # boxes land inside a widescreen template as well as a default one.
    width, height = presentation.slide_width, presentation.slide_height
    margin = int(width * 0.06)
    title_box = slide.shapes.add_textbox(
        margin, int(height * 0.08), width - 2 * margin, int(height * 0.16)
    )
    title_frame = title_box.text_frame
    title_frame.text = heading
    title_frame.paragraphs[0].runs[0].font.size = pptx.util.Pt(32)
    title_frame.paragraphs[0].runs[0].font.bold = True

    if not body_text:
        return
    body_box = slide.shapes.add_textbox(
        margin, int(height * 0.30), width - 2 * margin, int(height * 0.58)
    )
    body_frame = body_box.text_frame
    body_frame.word_wrap = True
    body_frame.text = body_text[0]
    for bullet in body_text[1:]:
        body_frame.add_paragraph().text = bullet
    for paragraph in body_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = pptx.util.Pt(18)


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
