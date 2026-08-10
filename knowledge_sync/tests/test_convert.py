"""What the chunker is handed, on the shapes real files actually convert to."""

from __future__ import annotations

import re
import textwrap

from agent_control_knowledge_sync.convert import (
    STATUS_CONVERTER_UNAVAILABLE,
    STATUS_EMPTY,
    STATUS_EXPORTED,
    Converted,
    convert_document,
    normalize_for_chunking,
    shipped_converter,
)
from agent_control_models.knowledge import chunk_and_scrub

WRAPPED_PROSE = "\n".join(
    textwrap.wrap(
        " ".join(
            f"Point {index} of the handbook is written out at enough length that it "
            f"survives a wrap and still reads as one sentence of company policy."
            for index in range(1, 121)
        ),
        80,
    )
)

SLIDE_DECK = """<!-- Slide number: 1 -->

Laptops are ordered on the first day.

### Notes:

Say this slowly.

<!-- Slide number: 2 -->

Badges take a week.
"""


def _refusing_converter(data: bytes, *, declared_mime: str | None) -> Converted:
    return Converted(text="", status="failed", error_code="converter_error")


def test_empty_bytes_are_a_stated_status_not_an_exception() -> None:
    result = convert_document(b"", declared_mime="application/pdf")
    assert result.status == STATUS_EMPTY
    assert not result.indexable


def test_markdown_passes_through_without_reaching_a_converter() -> None:
    """A Docs export is already the chunker's input; re-encoding it buys nothing."""

    def explode(data: bytes, *, declared_mime: str | None) -> Converted:
        raise AssertionError("the converter must not run on markdown")

    result = convert_document(
        b"# Onboarding\n\nLaptops are ordered on the first day.",
        declared_mime="text/markdown",
        converter=explode,
    )
    assert result.status == STATUS_EXPORTED
    assert "# Onboarding" in result.text


def test_a_failed_conversion_is_returned_untouched() -> None:
    result = convert_document(
        b"%PDF-1.7", declared_mime="application/pdf", converter=_refusing_converter
    )
    assert result.status == "failed"
    assert result.error_code == "converter_error"
    assert not result.indexable


def test_an_uninstalled_parser_says_so_rather_than_raising(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The converter module is always importable now; the parsers still may not be."""
    monkeypatch.setattr(
        "agent_control_models.attachment_converter_backends._module_installed",
        lambda module: False,
    )
    result = shipped_converter(b"%PDF-1.7", declared_mime="application/pdf")
    assert result.status == STATUS_CONVERTER_UNAVAILABLE
    assert result.error_code == "ocr_converter_absent"


def test_the_converter_is_a_static_import_not_a_lookup() -> None:
    """The sync container ships without the server, so a dynamic import would degrade."""
    from agent_control_knowledge_sync import convert

    assert convert.convert_attachment.__module__ == "agent_control_models.attachment_converter"


def test_slide_markers_become_the_headings_the_chunker_reads() -> None:
    normalized = normalize_for_chunking(SLIDE_DECK)
    assert "# Slide 1" in normalized
    assert "<!-- Slide number:" not in normalized

    paths = {chunk.heading_path for chunk in chunk_and_scrub(normalized).chunks}
    assert paths <= {"Slide 1", "Slide 2", "Slide 1 > Notes:", "Slide 2 > Notes:"}
    assert any(path is not None and path.startswith("Slide 1") for path in paths)


def test_siblings_do_not_nest_under_each_other() -> None:
    """Slide 2 follows slide 1; it is not inside it."""
    body = "Laptops are ordered on the first day. " * 12
    deck = f"<!-- Slide number: 1 -->\n\n{body}\n\n<!-- Slide number: 2 -->\n\n{body}\n"
    paths = [chunk.heading_path for chunk in chunk_and_scrub(normalize_for_chunking(deck)).chunks]
    assert paths == ["Slide 1", "Slide 2"]


def test_speaker_notes_stay_under_their_own_slide() -> None:
    normalized = normalize_for_chunking(SLIDE_DECK)
    assert "### Notes:" in normalized


def test_a_single_newline_is_soft_and_no_chunk_is_cut_mid_word() -> None:
    """The measured PDF shape: no headings, no blank lines, 80-column wrapping."""

    before = chunk_and_scrub(WRAPPED_PROSE).chunks
    after = chunk_and_scrub(normalize_for_chunking(WRAPPED_PROSE)).chunks

    assert _mid_word_cuts(before) > 0
    assert _mid_word_cuts(after) == 0
    assert all(re.search(r"[.!?][\"')\]]*$", chunk.body.rstrip()) for chunk in after)


def test_wrapping_that_split_a_word_is_healed() -> None:
    wrapped = "The reader account holds no contribu-\ntion rights at all."
    assert "contribution" in normalize_for_chunking(wrapped)


def test_line_breaks_the_author_meant_are_left_alone() -> None:
    """Every line ends a sentence, so the breaks are the author's, not the page's."""
    text = "Laptops arrive on day one.\nBadges arrive in a week.\nParking never arrives."
    assert normalize_for_chunking(text) == text


def test_pipe_tables_survive_reflow() -> None:
    table = "| Item | Days |\n| --- | --- |\n| Laptop | 1 |\n| Badge | 7 |"
    assert normalize_for_chunking(table) == table


def test_fenced_code_survives_reflow() -> None:
    fenced = "```\nselect 1\nfrom dual\nwhere x\n```"
    assert normalize_for_chunking(fenced) == fenced


def test_headings_survive_reflow() -> None:
    document = "# Onboarding\n\n## Laptops\n\nThey arrive on day one and are already enrolled."
    assert normalize_for_chunking(document) == document


def _mid_word_cuts(written: list) -> int:  # type: ignore[type-arg]
    return sum(
        1
        for left, right in zip(written, written[1:], strict=False)
        if re.search(r"[A-Za-z]$", left.body) and re.match(r"[a-z]", right.body)
    )
