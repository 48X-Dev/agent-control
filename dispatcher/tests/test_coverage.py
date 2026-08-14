"""Reading back the `## Coverage` section, which is what decides a retry."""

from __future__ import annotations

from agent_control_dispatcher.coverage import unmet_items

_REAL = """I reviewed the workbook and prioritized.

## Coverage

- VC introduction email: **partial** - company facts remain placeholders.
- Investor-familiar language: **done** - terminology supplied.
- Draft pitch deck grounding: **not determined** - no deck was found.

## Notes
- not a coverage line
"""


def test_only_the_lines_that_did_not_land_come_back() -> None:
    assert unmet_items(_REAL) == [
        "VC introduction email: partial - company facts remain placeholders.",
        "Draft pitch deck grounding: not determined - no deck was found.",
    ]


def test_a_later_heading_ends_the_section() -> None:
    assert "not a coverage line" not in " ".join(unmet_items(_REAL))


def test_a_clean_report_buys_no_extra_turn() -> None:
    assert unmet_items("## Coverage\n- a: **done**\n- b: **done**") == []


def test_a_report_with_no_section_buys_no_extra_turn() -> None:
    """A step that ignored the format has not asked for another turn."""
    assert unmet_items("just prose, no section at all") == []
    assert unmet_items(None) == []
    assert unmet_items("") == []


def test_an_unreadable_verdict_counts_as_unfinished() -> None:
    """A line that stopped saying whether it covered something has not shown that it did."""
    assert unmet_items("## Coverage\n- the list: who knows") == ["the list: who knows"]
