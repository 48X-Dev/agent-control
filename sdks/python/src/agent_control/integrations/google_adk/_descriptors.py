"""The descriptor and summary a control actually sees for a file part.

Split out of ``_attachments.py`` so the data model a control reads sits apart
from the walker that builds it. Nothing here touches bytes, opens a file or
knows what ADK is: it is the shape, and the honest limits on that shape are in
``_attachments.py``'s module docstring, which every status string below
inherits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ._sanitize import MARKER_PREFIX

UNKNOWN_SOURCE = "unknown"


@dataclass(frozen=True)
class AttachmentDescriptor:
    """Server-visible facts about one binary part. Never the bytes."""

    content_index: int
    part_index: int
    first_seen: bool = True
    source: str = UNKNOWN_SOURCE
    attachment_id: str | None = None
    display_name: str | None = None
    display_name_normalized: bool = False
    declared_mime: str | None = None
    sniffed_mime: str | None = None
    mime_mismatch: bool = False
    size_bytes: int | None = None
    sha256: str | None = None
    page_count: int | None = None
    estimated_tokens: int | None = None
    extraction_status: str = "not_attempted"
    text_chars: int | None = None
    text_truncated: bool = False
    chunk_count: int | None = None
    pages_with_no_text: int | None = None
    low_text_pages: int | None = None
    max_image_area_ratio: float | None = None
    converted_from: str | None = None
    source_sha256: str | None = None
    uri_scheme: str | None = None
    uri_host: str | None = None
    uri_sha256: str | None = None

    @property
    def is_file_data(self) -> bool:
        """True for a URI-backed part, whose bytes no control can ever read."""

        return self.uri_scheme is not None

    def to_dict(self) -> dict[str, Any]:
        """Render the descriptor for ``context.agent_control.attachments``."""

        return {
            "content_index": self.content_index,
            "part_index": self.part_index,
            "first_seen": self.first_seen,
            "source": self.source,
            "attachment_id": self.attachment_id,
            "display_name": self.display_name,
            "display_name_normalized": self.display_name_normalized,
            "declared_mime": self.declared_mime,
            "sniffed_mime": self.sniffed_mime,
            "mime_mismatch": self.mime_mismatch,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "page_count": self.page_count,
            "estimated_tokens": self.estimated_tokens,
            "extraction_status": self.extraction_status,
            "text_chars": self.text_chars,
            "text_truncated": self.text_truncated,
            "chunk_count": self.chunk_count,
            "pages_with_no_text": self.pages_with_no_text,
            "low_text_pages": self.low_text_pages,
            "max_image_area_ratio": self.max_image_area_ratio,
            "converted_from": self.converted_from,
            "source_sha256": self.source_sha256,
            "uri_scheme": self.uri_scheme,
            "uri_host": self.uri_host,
            "uri_sha256": self.uri_sha256,
        }

    def log_summary(self) -> dict[str, Any]:
        """A logging-safe view: no filename, no URI, no bytes.

        Filenames and URIs are content. A signed URL is a bearer credential.
        Neither belongs in a log line above DEBUG, so neither is here at all.
        """

        return {
            "content_index": self.content_index,
            "part_index": self.part_index,
            "source": self.source,
            "declared_mime": self.declared_mime,
            "sniffed_mime": self.sniffed_mime,
            "size_bytes": self.size_bytes,
            "sha256_prefix": self.sha256[:16] if self.sha256 else None,
            "uri_scheme": self.uri_scheme,
        }

    def placeholder_line(self, index: int, total: int) -> str:
        """Build the transcript marker for this part.

        Decoration for text controls and a human-readable transcript marker.
        Not a security boundary: its contents are forgeable, controls must key
        on ``context.agent_control.*`` instead.
        """

        name = self.display_name or "<unnamed>"
        size = str(self.size_bytes) if self.size_bytes is not None else "unknown"
        digest = self.sha256[:16] if self.sha256 else "unknown"
        return (
            f"{MARKER_PREFIX} attachment {index} of {total} | name=\"{name}\" | "
            f"type={self.declared_mime or 'unknown'} | bytes={size} | "
            f"sha256={digest} | source={self.source}]"
        )


def build_attachment_summary(descriptors: Sequence[AttachmentDescriptor]) -> dict[str, Any]:
    """Pre-aggregate the descriptor list into scalars a selector can reach.

    ``select_data`` walks a dotted path and has no list-index syntax, so
    ``context.agent_control.attachments.0.size_bytes`` resolves to ``None``.
    Every threshold rule an operator wants to write is therefore a scalar here,
    and per-file rules go through the ``json`` evaluator's schema over the array.
    """

    ratios = [d.max_image_area_ratio for d in descriptors if d.max_image_area_ratio is not None]
    return {
        "count": len(descriptors),
        "new_count": sum(1 for d in descriptors if d.first_seen),
        "carried_over_count": sum(1 for d in descriptors if not d.first_seen),
        "total_bytes": sum(d.size_bytes or 0 for d in descriptors),
        "total_pages": sum(d.page_count or 0 for d in descriptors),
        "estimated_tokens": sum(d.estimated_tokens or 0 for d in descriptors),
        "unminted_count": sum(1 for d in descriptors if d.source != "operator"),
        "file_data_count": sum(1 for d in descriptors if d.is_file_data),
        "mismatch_count": sum(1 for d in descriptors if d.mime_mismatch),
        "unextracted_count": sum(
            1 for d in descriptors if d.extraction_status != "text_layer_extracted"
        ),
        "truncated_count": sum(1 for d in descriptors if d.text_truncated),
        "pages_with_no_text": sum(d.pages_with_no_text or 0 for d in descriptors),
        "max_image_area_ratio": max(ratios) if ratios else None,
    }
