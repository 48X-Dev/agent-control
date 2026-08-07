"""The chunker's stated contract, asserted as invariants over many shapes.

``test_knowledge.py`` pins the named cases one at a time. This file asks the
weaker but broader question: whatever the document, does the output still obey
the three rules section 4.3 of the plan sells the chunker on? Every piece is
big enough to be worth citing, no piece is unboundedly large, and nothing the
author wrote went missing on the way in.

Two of those rules hold everywhere tried. The floor does not, and the shapes
where it fails are marked ``xfail(strict=True)`` rather than deleted or
softened: a fragment reaching the index is the exact failure the floor was
invented to prevent, and a strict marker turns red the moment somebody fixes
it, which is how the note removes itself.
"""

from __future__ import annotations

import re

import pytest
from agent_control_models.knowledge import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    HEADING_PATH_SEPARATOR,
    KnowledgeRefusalCode,
    chunk_markdown,
    normalize_index_name,
    normalize_index_path,
    scrub_chunk,
)


def _paragraph(chars: int, word: str = "policy") -> str:
    unit = f"{word} "
    return (unit * (chars // len(unit) + 1))[:chars].strip()


def _dense(text: str) -> str:
    """Everything the author typed, minus the whitespace the chunker may move."""
    return re.sub(r"\s+", "", text)


_ORPHANED_HEADING = pytest.mark.xfail(
    strict=True,
    reason=(
        "the leading fragment escapes the floor: _split_oversized folds back a "
        "short *tail* but never a short head, so a section whose first "
        "paragraph will not pack beside its heading line emits the heading "
        "alone as a chunk"
    ),
)

# One battery, three invariants, so a shape that breaks one is still checked
# against the other two.
DOCUMENTS: dict[str, str] = {
    "nested_headings": (
        f"# A\n\n{_paragraph(400)}\n\n## B\n\n{_paragraph(400, 'beta')}\n\n"
        f"### C\n\n{_paragraph(400, 'gamma')}\n"
    ),
    "skipped_level": f"# A\n\n{_paragraph(400)}\n\n### C\n\n{_paragraph(400, 'gamma')}\n",
    "no_headings": "\n\n".join(_paragraph(600, f"row{index}") for index in range(6)),
    "one_enormous_paragraph": "x" * (CHUNK_MAX_CHARS * 2 + 500),
    "spreadsheet_dump": "\n\n".join(f"| a{i} | b{i} | c{i} |" for i in range(200)),
    "many_tiny_sections": "\n\n".join(f"# H{i}\n\nshort {i}" for i in range(8)),
    "long_then_tiny_section": f"# Long\n\n{_paragraph(600)}\n\n## Tiny\n\nsee IT.\n",
    "code_fence": (
        f"# Runbook\n\n{_paragraph(300, 'step')}\n\n```sh\n# not a heading\n"
        f"echo hi\n```\n\n{_paragraph(300, 'more')}\n"
    ),
    "windows_newlines": f"# A\r\n\r\n{_paragraph(400)}\r\n",
    "mixed": (
        f"{_paragraph(300, 'intro')}\n\n# One\n\n{_paragraph(900, 'alpha')}\n\n"
        f"## Two\n\nshort\n\n### Three\n\n{_paragraph(2600, 'gamma')}\n"
    ),
    "heading_then_long_paragraph": f"## Laptops\n\n{_paragraph(1990, 'laptop')}\n",
    "intro_line_then_long_paragraph": f"# A\n\nSee below.\n\n{_paragraph(1990, 'laptop')}\n",
}

FLOOR_CASES = [
    pytest.param(
        name,
        marks=_ORPHANED_HEADING
        if name in {"heading_then_long_paragraph", "intro_line_then_long_paragraph"}
        else (),
    )
    for name in DOCUMENTS
]


@pytest.mark.parametrize("name", FLOOR_CASES)
def test_every_chunk_is_big_enough_to_answer_from(name: str) -> None:
    """The floor, as a rule about the whole output rather than one merge step.

    A document shorter than the floor is the one admitted exception: there is
    no neighbour to fold it into and dropping it would lose the document.
    """
    chunks = chunk_markdown(DOCUMENTS[name])
    if len(chunks) <= 1:
        return

    undersized = [
        (chunk.ordinal, chunk.chars, chunk.body[:40])
        for chunk in chunks
        if chunk.chars < CHUNK_MIN_CHARS
    ]
    assert not undersized, undersized


@pytest.mark.parametrize("name", DOCUMENTS)
def test_no_chunk_exceeds_the_ceiling_by_more_than_the_floor(name: str) -> None:
    """The ceiling is a splitting target, and the overshoot it allows is bounded.

    A merge that clears the floor may push a chunk past ``max_chars``, which
    the design accepts. What it must not do is accept it without a bound, or
    the snippet ceiling downstream becomes the only thing between an agent and
    a whole document.
    """
    chunks = chunk_markdown(DOCUMENTS[name])

    oversized = [chunk.chars for chunk in chunks if chunk.chars > CHUNK_MAX_CHARS + CHUNK_MIN_CHARS]
    assert not oversized, oversized


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_non_whitespace_character_survives_in_order(name: str) -> None:
    """Nothing is added, dropped or reordered; only whitespace may move.

    Stronger than checking that a few words came through, and it is the claim
    that makes a citation checkable: a human opening the cited file has to find
    the snippet in it.
    """
    text = DOCUMENTS[name]
    joined = "".join(chunk.body for chunk in chunk_markdown(text))

    assert _dense(joined) == _dense(text)


@pytest.mark.parametrize("name", DOCUMENTS)
def test_ordinals_are_dense_and_start_at_zero(name: str) -> None:
    """``UNIQUE (document_id, ordinal)`` and the dedupe key both rely on this."""
    chunks = chunk_markdown(DOCUMENTS[name])

    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


@_ORPHANED_HEADING
def test_a_heading_is_never_orphaned_into_its_own_chunk() -> None:
    """The floor failure, minimally.

    ``## Laptops`` followed by a paragraph that will not fit beside it yields
    two chunks: the heading alone, and the paragraph stripped of the heading
    line that made its words searchable. The first is ten characters of
    indexable text that matches ``laptops``, spends one of the five result
    slots, and shows a model a citation with no policy under it.
    """
    chunks = chunk_markdown(f"## Laptops\n\n{_paragraph(1990, 'laptop')}\n")

    assert [chunk.body for chunk in chunks] != ["## Laptops", _paragraph(1990, "laptop")]


def test_a_merge_may_pass_the_ceiling_and_by_less_than_the_floor() -> None:
    """The accepted overshoot, with its arithmetic written down."""
    text = f"{_paragraph(CHUNK_MAX_CHARS)}\n\nSee the appendix for the rest of it."
    (chunk,) = chunk_markdown(text)

    assert chunk.chars > CHUNK_MAX_CHARS
    assert chunk.chars - CHUNK_MAX_CHARS < CHUNK_MIN_CHARS


def test_a_merge_keeps_the_earlier_heading_when_the_earlier_section_dominates() -> None:
    """The mirror of the merge case already pinned: dominance decides, not order."""
    text = f"# Long\n\n{_paragraph(600)}\n\n## Tiny\n\nsee IT.\n"
    (chunk,) = chunk_markdown(text)

    assert chunk.heading_path == "Long"
    assert "## Tiny" in chunk.body


def test_a_skipped_heading_level_keeps_the_path_it_can_prove() -> None:
    """``#`` then ``###`` names two levels, not three, and invents no middle one."""
    text = f"# A\n\n{_paragraph(400)}\n\n### C\n\n{_paragraph(400, 'gamma')}\n"
    paths = [chunk.heading_path for chunk in chunk_markdown(text)]

    assert paths == ["A", f"A{HEADING_PATH_SEPARATOR}C"]


def test_a_closing_hash_run_is_not_part_of_the_title() -> None:
    text = f"## Laptops ##\n\n{_paragraph(400)}\n"
    (chunk,) = chunk_markdown(text)

    assert chunk.heading_path == "Laptops"


def test_the_heading_line_stays_in_the_body_where_search_can_reach_it() -> None:
    """The path is for the citation; the words have to be in the tsvector."""
    text = f"# Onboarding\n\n## Laptops\n\n{_paragraph(400, 'laptop')}\n"
    (chunk,) = chunk_markdown(text)

    assert "## Laptops" in chunk.body
    assert chunk.heading_path == f"Onboarding{HEADING_PATH_SEPARATOR}Laptops"


def test_a_backtick_line_inside_a_tilde_fence_does_not_close_it() -> None:
    """Only the marker that opened a fence closes it."""
    text = f"# A\n\n{_paragraph(300)}\n\n~~~\n```\n# heading?\n~~~\n\n{_paragraph(300, 'beta')}\n"
    chunks = chunk_markdown(text)

    assert [chunk.heading_path for chunk in chunks] == ["A"]
    assert "# heading?" in chunks[0].body


def test_an_unclosed_fence_swallows_every_heading_after_it() -> None:
    """A degradation worth knowing about rather than discovering in a citation.

    A converter that emits an unbalanced fence costs the rest of the document
    its heading boundaries. The content is all still there and still
    searchable, so this is a provenance cost, not a loss.
    """
    text = f"# A\n\n{_paragraph(300)}\n\n```sh\necho hi\n\n# B\n\n{_paragraph(300, 'beta')}\n"
    chunks = chunk_markdown(text)

    assert [chunk.heading_path for chunk in chunks] == ["A"]
    assert "# B" in chunks[0].body


def test_carriage_returns_do_not_defeat_the_heading_boundary() -> None:
    """Converted markdown arrives from whatever wrote it, newlines included."""
    (chunk,) = chunk_markdown(f"# A\r\n\r\n{_paragraph(400)}\r\n")

    assert chunk.heading_path == "A"


def test_chunking_a_chunk_again_returns_it_unchanged() -> None:
    """Re-indexing an unchanged document must not shuffle its ordinals."""
    (once,) = chunk_markdown(f"# Onboarding\n\n{_paragraph(500, 'laptop')}\n")
    (twice,) = chunk_markdown(once.body)

    assert twice.body == once.body
    assert twice.heading_path == once.heading_path


# --- The scrub's edges ------------------------------------------------------


def test_a_commit_sha_quoted_in_prose_is_not_an_assignment() -> None:
    """The adjacency rule, which is the whole reason the entropy shape is usable."""
    body = "The fix landed in commit 3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a and shipped Thursday."

    assert scrub_chunk(body).clean


def test_a_checksum_named_in_prose_is_not_an_assignment() -> None:
    body = (
        "The checksum is sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08."
    )

    assert scrub_chunk(body).clean


def test_an_ordinary_link_is_not_a_credential() -> None:
    body = "See https://intranet.example.com/handbook/onboarding/laptops for the full text."

    assert scrub_chunk(body).clean


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the entropy shape reads an internal link as a secret: '/' is in its "
        "character class and a dotless host leaves a 32-character run straight "
        "after the scheme's colon, so the chunk is dropped and counted as a "
        "credential"
    ),
)
def test_an_internal_link_without_a_dot_in_the_host_is_not_a_credential() -> None:
    body = "See https://intranet/handbook/onboarding/laptops-policy-v2 for the full text."

    assert scrub_chunk(body).clean


def test_a_section_that_names_the_password_policy_is_refused_with_it() -> None:
    """The cost of keeping the shipped control's words character for character.

    ``\\bpassword\\s*[:=]`` cannot tell a credential from a heading that ends in
    a colon, and narrowing it here would mean a string this scrub admits is one
    the memory control would have caught. The drop is counted, which is what
    makes the cost visible instead of silent.
    """
    verdict = scrub_chunk("## Password: how to reset yours\n\nAsk IT and wait an hour.")

    assert not verdict.clean
    assert verdict.matched == "password_assignment"


@pytest.mark.parametrize(
    "body",
    [
        "rotate sk-abcdefghijklmnopqrstuvwx monthly",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "password = hunter2",
    ],
)
def test_every_shape_the_shipped_memory_control_names_is_caught_here_too(body: str) -> None:
    """One deny-list in two places, kept in step.

    ``memory-controls.md`` 3.1 ships
    ``sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\\bpassword\\s*[:=]``
    as a live control over model output. A string that control would block on
    the way out must not be one this scrub admits on the way in.
    """
    assert not scrub_chunk(body).clean


# --- Naming and the refusal vocabulary --------------------------------------


def test_a_path_whose_every_segment_normalizes_away_leaves_nothing() -> None:
    assert normalize_index_path("​/​") is None


def test_a_name_that_is_only_separators_does_not_become_a_path() -> None:
    assert normalize_index_name("///") == "___"
    assert normalize_index_path("///") is None


def test_the_refusal_vocabulary_is_exactly_the_six_the_plan_names() -> None:
    """A closed enum shared by the store, the endpoint and the tool.

    Every sentence a model reads about a failed search is chosen from this
    list, so a seventh member added quietly is a sentence nobody wrote.
    """
    assert {code.value for code in KnowledgeRefusalCode} == {
        "query_too_short",
        "query_too_long",
        "rate_limited",
        "knowledge_unavailable",
        "knowledge_disabled",
        "corpus_empty",
    }
