"""The three builders produce openable OOXML from structured data."""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from my_agent.file_builders import build_docx, build_pptx, build_xlsx

openpyxl = pytest.importorskip("openpyxl")
docx = pytest.importorskip("docx")
pptx = pytest.importorskip("pptx")


def test_xlsx_round_trips_header_and_rows() -> None:
    payload = build_xlsx(
        sheet_name="Shortlist",
        header=["Fund", "Cheque"],
        rows=[["Index", "12000000"], ["Accel", "8000000"]],
    )

    sheet = openpyxl.load_workbook(BytesIO(payload)).active
    assert sheet.title == "Shortlist"
    assert [cell.value for cell in sheet[1]] == ["Fund", "Cheque"]
    assert [cell.value for cell in sheet[2]] == ["Index", 12000000]


def test_xlsx_keeps_a_formula_cell_as_text() -> None:
    """A model's cell text must not execute in the spreadsheet a person opens."""
    payload = build_xlsx(
        sheet_name="Data",
        header=["Note"],
        rows=[["=HYPERLINK(\"http://evil.example\",\"click\")"]],
    )

    cell = openpyxl.load_workbook(BytesIO(payload)).active["A2"]
    assert cell.data_type == "s"
    assert cell.value.startswith("=HYPERLINK")


def test_xlsx_leaves_ambiguous_digits_as_text() -> None:
    payload = build_xlsx(sheet_name="D", header=["Code"], rows=[["007"], ["1-2"], ["3.50"]])

    column = [row[0].value for row in openpyxl.load_workbook(BytesIO(payload)).active["A2:A4"]]
    assert column == ["007", "1-2", 3.5]


def test_xlsx_sanitises_an_illegal_sheet_name() -> None:
    payload = build_xlsx(sheet_name="Q1/Q2:results", header=["A"], rows=[["1"]])

    assert openpyxl.load_workbook(BytesIO(payload)).active.title == "Q1-Q2-results"


def test_docx_carries_title_headings_and_paragraphs() -> None:
    payload = build_docx(
        title="Vendor review",
        sections=[["Findings", "Two vendors met the bar.", "One did not."]],
    )

    text = [paragraph.text for paragraph in docx.Document(BytesIO(payload)).paragraphs]
    assert text[:4] == ["Vendor review", "Findings", "Two vendors met the bar.", "One did not."]


def test_docx_skips_an_empty_section() -> None:
    payload = build_docx(title="Brief", sections=[[], ["Only heading"]])

    text = [paragraph.text for paragraph in docx.Document(BytesIO(payload)).paragraphs]
    assert text == ["Brief", "Only heading"]


def test_pptx_makes_a_title_slide_and_one_slide_per_entry() -> None:
    payload = build_pptx(
        title="Q3 review",
        slides=[["Wins", "Shipped the store", "Cut latency"], ["Risks", "Image size"]],
    )

    slides = pptx.Presentation(BytesIO(payload)).slides
    assert len(slides) == 3
    assert slides[0].shapes.title.text == "Q3 review"
    assert slides[1].shapes.title.text == "Wins"
    assert slides[1].placeholders[1].text_frame.text == "Shipped the store\nCut latency"
    assert slides[2].shapes.title.text == "Risks"


@pytest.mark.parametrize(
    ("payload", "marker"),
    [
        (build_xlsx(sheet_name="S", header=["A"], rows=[["1"]]), "xl/workbook.xml"),
        (build_docx(title="T", sections=[["H", "P"]]), "word/document.xml"),
        (build_pptx(title="T", slides=[["S", "B"]]), "ppt/presentation.xml"),
    ],
)
def test_every_builder_emits_a_real_ooxml_package(payload: bytes, marker: str) -> None:
    assert marker in zipfile.ZipFile(BytesIO(payload)).namelist()
