"""The file source refuses rather than skips, because a skipped item is an item
a person wrote, expected to be worked, and never heard about again."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agent_control_dispatcher.sources.file import (
    FileTaskSource,
    SourceParseError,
    resolve_source,
)

GOOD = """
- ref: t1
  title: Summarise the Q3 incident reports
  body: |
    Read the three reports and list the common causes.
- ref: t2
  title: Draft the release note
  body: one paragraph
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "tasks.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _poll(source: FileTaskSource, cursor: str | None = None) -> list[str]:
    return [item.ref for item in asyncio.run(source.poll(cursor=cursor))]


def test_reads_items_in_file_order(tmp_path: Path) -> None:
    source = FileTaskSource(_write(tmp_path, GOOD))
    items = asyncio.run(source.poll(cursor=None))
    assert [item.ref for item in items] == ["t1", "t2"]
    assert items[0].title == "Summarise the Q3 incident reports"
    assert items[0].body.startswith("Read the three reports")


def test_a_cursor_resumes_after_the_named_ref(tmp_path: Path) -> None:
    source = FileTaskSource(_write(tmp_path, GOOD))
    assert _poll(source, cursor="t1") == ["t2"]
    assert _poll(source, cursor="unknown") == ["t1", "t2"]


def test_only_the_file_scheme_is_understood() -> None:
    assert isinstance(resolve_source("file://tasks.yaml"), FileTaskSource)
    assert isinstance(resolve_source("tasks.yaml"), FileTaskSource)
    with pytest.raises(SourceParseError, match="linear"):
        resolve_source("linear://ENG")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("- ref: t1\n  title: a\n  agent: researcher\n", "unknown keys"),
        ("- ref: t1\n  title: a\n- ref: t1\n  title: b\n", "duplicate ref"),
        ("- title: a\n", "'ref' is required"),
        ("- ref: t1\n", "neither a title nor a body"),
        ("ref: t1\n", "must be a YAML list"),
        ("- ref: t1\n  title: [a, b]\n", "'title' must be a string"),
    ],
)
def test_a_malformed_file_is_refused_not_skipped(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(SourceParseError, match=match):
        asyncio.run(FileTaskSource(_write(tmp_path, text)).poll(cursor=None))


def test_every_refusal_names_the_file_it_came_from(tmp_path: Path) -> None:
    """An operator gets one line on stderr. It has to say which file and which
    item, because the next thing they do is open it."""

    path = _write(tmp_path, "- ref: t1\n  title: a\n- ref: t2\n  title: [a]\n")
    with pytest.raises(SourceParseError) as caught:
        asyncio.run(FileTaskSource(path).poll(cursor=None))

    assert str(path) in str(caught.value)
    assert "item 1" in str(caught.value)


def test_broken_yaml_is_a_parse_error_rather_than_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(SourceParseError, match="not valid YAML"):
        asyncio.run(FileTaskSource(_write(tmp_path, "- ref: [t1\n  title: a\n")).poll(cursor=None))


def test_a_file_of_only_comments_is_no_work(tmp_path: Path) -> None:
    assert _poll(FileTaskSource(_write(tmp_path, "# nothing here yet\n"))) == []


def test_a_directory_where_a_file_should_be_says_so(tmp_path: Path) -> None:
    with pytest.raises(SourceParseError, match="Cannot read source file"):
        asyncio.run(FileTaskSource(tmp_path).poll(cursor=None))


def test_an_enormous_body_is_the_envelope_s_problem_not_the_source_s(tmp_path: Path) -> None:
    """The source hands the text over whole. Bounding it is section 9.2's job and
    doing it twice would mean two places to get the truncation notice wrong."""

    path = _write(tmp_path, "- ref: t1\n  title: Long\n  body: " + "x" * 100_000 + "\n")
    items = asyncio.run(FileTaskSource(path).poll(cursor=None))
    assert len(items[0].body) == 100_000


def test_an_item_may_have_a_body_and_no_title(tmp_path: Path) -> None:
    source = FileTaskSource(_write(tmp_path, "- ref: t1\n  body: do it\n"))
    items = asyncio.run(source.poll(cursor=None))
    assert (items[0].title, items[0].body) == ("", "do it")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("- ref: 12\n  title: a\n", "'ref' is required"),
        ("- ref: '  '\n  title: a\n", "'ref' is required"),
        ("- ref: t1\n  title: a\n  url: 7\n", "'url' must be a string"),
        ("- ref: t1\n  title: a\n  updated_at: soon\n", "'updated_at' must be a date"),
        ("- t1\n", "expected a mapping"),
    ],
)
def test_a_field_of_the_wrong_shape_is_refused_with_the_field_named(
    tmp_path: Path, text: str, match: str
) -> None:
    with pytest.raises(SourceParseError, match=match):
        asyncio.run(FileTaskSource(_write(tmp_path, text)).poll(cursor=None))


def test_a_fully_dated_file_orders_by_date_across_mixed_offsets(tmp_path: Path) -> None:
    text = (
        "- ref: late\n  title: a\n  updated_at: 2026-01-02T00:00:00+02:00\n"
        "- ref: early\n  title: b\n  updated_at: 2026-01-01 09:00:00\n"
    )
    assert _poll(FileTaskSource(_write(tmp_path, text))) == ["early", "late"]


def test_a_half_dated_file_is_read_top_to_bottom(tmp_path: Path) -> None:
    text = "- ref: b\n  title: b\n  updated_at: 2026-01-09\n- ref: a\n  title: a\n"
    assert _poll(FileTaskSource(_write(tmp_path, text))) == ["b", "a"]


def test_an_empty_file_is_no_work_rather_than_an_error(tmp_path: Path) -> None:
    assert _poll(FileTaskSource(_write(tmp_path, ""))) == []


def test_a_missing_file_names_itself(tmp_path: Path) -> None:
    with pytest.raises(SourceParseError, match="Cannot read source file"):
        asyncio.run(FileTaskSource(tmp_path / "absent.yaml").poll(cursor=None))


def test_write_back_refuses_rather_than_reporting_a_write_that_did_not_happen(
    tmp_path: Path,
) -> None:
    source = FileTaskSource(_write(tmp_path, GOOD))
    with pytest.raises(NotImplementedError):
        asyncio.run(source.write_back(item_ref="t1", body="x", idempotency_marker="m"))
