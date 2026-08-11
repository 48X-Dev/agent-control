"""Attachment wire models, and the normalizer and sniffer they share with the server.

The three functions in ``agent_control_models.files`` are tested here rather
than in the SDK because this is where they live now. The server's upload gate
and the SDK's descriptor both call them, and a second implementation on either
side would let the gate and the descriptor disagree about the same file.
"""

from __future__ import annotations

import datetime as dt

import pytest
from agent_control_models import (
    ATTACHMENT_HARD_MAX_BYTES,
    Attachment,
    AttachmentOrigin,
    AttachmentStatus,
    CreateAttachmentResponse,
    ListAttachmentsResponse,
    StepAttachmentSummary,
    StepFilesSummary,
    is_mime_mismatch,
    normalize_display_name,
    sniff_mime,
)
from agent_control_models.attachments import (
    ATTACHMENT_DISPLAY_NAME_MAX_LENGTH,
    TurnAttachmentVerdict,
)
from pydantic import ValidationError

NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)


def _attachment(**overrides: object) -> Attachment:
    payload: dict[str, object] = {
        "attachment_key": "a" * 32,
        "session_key": "b" * 32,
        "display_name": "q3-forecast.pdf",
        "display_name_normalized": False,
        "declared_mime": "application/pdf",
        "sniffed_mime": "application/pdf",
        "mime_mismatch": False,
        "size_bytes": 2411903,
        "source_sha256": "9f" * 32,
        "status": AttachmentStatus.READY,
        "origin": AttachmentOrigin.OPERATOR_UPLOAD,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return Attachment.model_validate(payload)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


def test_an_attachment_round_trips() -> None:
    restored = Attachment.model_validate(_attachment().model_dump(mode="json"))

    assert restored.status == AttachmentStatus.READY
    assert restored.page_count is None
    assert restored.estimated_tokens is None


def test_the_page_and_token_fields_default_to_null() -> None:
    """Null rather than zero, and null rather than a guess from the byte count.

    A thousand-page text PDF can be three megabytes and a forty-page scan can
    be twenty, so a number derived from length would be a number somebody would
    later spend against.
    """
    attachment = _attachment()

    assert attachment.page_count is None
    assert attachment.estimated_tokens is None
    assert attachment.converted_from is None


def test_a_zero_byte_attachment_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        _attachment(size_bytes=0)


def test_the_response_wrappers_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateAttachmentResponse.model_validate(
            {
                "attachment": _attachment().model_dump(mode="json"),
                "deduplicated": False,
                "bytes": "AAAA",
            }
        )


def test_a_list_response_carries_the_totals_the_quotas_count() -> None:
    response = ListAttachmentsResponse(attachments=[_attachment()], count=1, total_bytes=2411903)

    assert response.model_dump()["total_bytes"] == 2411903


def test_a_step_summary_carries_no_bytes_and_no_url() -> None:
    """The durable record of one hop. It survives the session and the blob TTL,
    so it has to be small enough to keep forever."""
    summary = StepAttachmentSummary(
        display_name="q3-forecast.pdf",
        sha256="9f" * 32,
        size_bytes=2411903,
        sniffed_mime="application/pdf",
        origin=AttachmentOrigin.LINEAR,
        origin_ref="att_01H",
        verdict=TurnAttachmentVerdict.SENT,
    )

    dumped = summary.model_dump()

    assert set(dumped) == {
        "display_name",
        "sha256",
        "size_bytes",
        "sniffed_mime",
        "origin",
        "origin_ref",
        "verdict",
        "failure_code",
        "attachment_key",
        "text_ready",
        "bytes_fetched",
    }
    # ``attachment_key`` is the one identifier that leaves the server, and it is
    # a minted handle rather than anything anybody could dereference. A tracker
    # URL in this row would put one in front of the dispatcher, which is the
    # thing the whole fetch design exists to avoid.
    assert "://" not in str(dumped)


def test_a_files_summary_keeps_found_and_delivered_apart() -> None:
    """``found`` is deliberately not ``len(files)``. A per-issue cap means a
    step may never attempt most of what an issue carries, and a summary that
    said "1 of 1" about an issue holding twelve would be the confident
    half-answer the count line exists to stop."""
    summary = StepFilesSummary(
        found=12,
        delivered=1,
        files=[
            StepAttachmentSummary(
                display_name="deck.pdf",
                sha256="9f" * 32,
                size_bytes=1_414_578,
                sniffed_mime="application/pdf",
                origin=AttachmentOrigin.LINEAR,
                verdict=TurnAttachmentVerdict.PENDING,
                attachment_key="0123456789abcdef" * 2,
            )
        ],
    )

    assert (summary.found, summary.delivered, len(summary.files)) == (12, 1, 1)


def test_a_files_summary_tells_an_empty_issue_from_one_nobody_could_read() -> None:
    """Two states that produce identical counts and must not produce identical
    sentences. "No files are attached to this issue" on the day the tracker was
    rate-limiting is a server-authored line backing a wrong conclusion, which is
    worse than the silence this whole section replaced."""
    empty = StepFilesSummary(found=0, delivered=0, files=[])
    unread = StepFilesSummary(found=0, delivered=0, files=[], read_failed=True)

    assert empty.read_failed is False
    assert unread.read_failed is True
    assert (empty.found, unread.found) == (0, 0)


def test_a_step_summary_separates_bytes_fetched_from_bytes_stored() -> None:
    """A refusal has no stored bytes and can still have cost twenty megabytes.

    The per-task ceiling bounds what crossed the wire unattended, so a refusal
    row carries what it spent and nothing else - counting only stored files
    would leave a chain of oversized refusals free.
    """
    refused = StepAttachmentSummary(
        display_name="huge.pdf",
        origin=AttachmentOrigin.LINEAR,
        verdict=TurnAttachmentVerdict.BLOCKED,
        failure_code="too_large",
        bytes_fetched=20_971_521,
    )

    assert refused.size_bytes is None
    assert refused.bytes_fetched == 20_971_521
    assert refused.text_ready is False


def test_a_stored_file_is_not_readable_until_its_text_is_ready() -> None:
    """``attachment_key`` says the server holds the bytes. ``text_ready`` says
    the model will see them, and delivery is text, so only the second one means
    the agent can read it."""
    stored = StepAttachmentSummary(
        display_name="deck.pdf",
        sha256="9f" * 32,
        size_bytes=2048,
        sniffed_mime="application/pdf",
        origin=AttachmentOrigin.LINEAR,
        verdict=TurnAttachmentVerdict.PENDING,
        attachment_key="0123456789abcdef" * 2,
    )

    assert stored.attachment_key is not None
    assert stored.text_ready is False


def test_a_files_summary_refuses_a_field_nobody_declared() -> None:
    with pytest.raises(ValidationError):
        StepFilesSummary(found=1, delivered=1, source_url="https://uploads.linear.app/x")


def test_the_hard_byte_ceiling_is_the_constant_the_check_carries() -> None:
    assert ATTACHMENT_HARD_MAX_BYTES == 52_428_800


# ---------------------------------------------------------------------------
# Filename normalization
# ---------------------------------------------------------------------------


def test_a_name_that_forges_a_placeholder_field_loses_its_delimiters() -> None:
    name, changed = normalize_display_name('x" | source=operator | name="a')

    assert changed is True
    assert '"' not in name
    assert "|" not in name


def test_a_bidi_override_cannot_disguise_an_extension() -> None:
    """``report<U+202E>fdp.exe`` renders as ``report.pdf`` to a human reading a
    transcript. Stripping the override is what makes the rendered name the real
    one."""
    name, changed = normalize_display_name("report‮fdp.exe")

    assert changed is True
    assert "‮" not in name
    assert name == "reportfdp.exe"


def test_path_separators_are_removed() -> None:
    name, changed = normalize_display_name("../../etc/passwd")

    assert changed is True
    assert "/" not in name


def test_a_name_of_nothing_but_invisibles_normalizes_to_none() -> None:
    name, changed = normalize_display_name("‪‭​")

    assert name is None
    assert changed is True


def test_a_long_name_is_capped_rather_than_refused() -> None:
    name, changed = normalize_display_name("a" * 400 + ".pdf")

    assert len(name) == ATTACHMENT_DISPLAY_NAME_MAX_LENGTH
    assert changed is True


def test_a_surrogate_pair_at_the_boundary_does_not_split() -> None:
    """The cap counts code points, so an astral character is kept or dropped
    whole. A half-surrogate would be a name no encoder can serialize."""
    raw = "x" * (ATTACHMENT_DISPLAY_NAME_MAX_LENGTH - 1) + "\U0001f600" + "tail"

    name, _ = normalize_display_name(raw)

    assert len(name) <= ATTACHMENT_DISPLAY_NAME_MAX_LENGTH
    name.encode("utf-8")


def test_an_ordinary_name_is_left_alone() -> None:
    name, changed = normalize_display_name("q3-forecast.pdf")

    assert name == "q3-forecast.pdf"
    assert changed is False


def test_a_non_string_is_not_a_name() -> None:
    assert normalize_display_name(None) == (None, False)
    assert normalize_display_name(b"bytes") == (None, False)


# ---------------------------------------------------------------------------
# Sniffing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7\n", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"PK\x03\x04", "application/zip"),
        (b"", None),
        (None, None),
        (b"just some words", None),
    ],
)
def test_the_sniffer_reads_only_the_first_bytes(data, expected) -> None:
    assert sniff_mime(data) == expected


def test_an_office_document_sniffs_as_its_container_without_a_mismatch() -> None:
    """OOXML is a ZIP. Reporting a mismatch on every PowerPoint ever attached
    would make the signal noise."""
    assert (
        is_mime_mismatch(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/zip",
        )
        is False
    )


def test_a_pdf_that_is_really_a_zip_is_a_mismatch() -> None:
    assert is_mime_mismatch("application/pdf", "application/zip") is True


def test_an_unrecognized_body_is_never_a_mismatch() -> None:
    """``None`` means no magic number matched, not "plain text". Calling that a
    mismatch would flag every file the table does not know."""
    assert is_mime_mismatch("application/pdf", None) is False
