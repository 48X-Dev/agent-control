"""The oversight read: what the knowledge mirror says about itself, per source."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from .base import BaseModel

KnowledgeSourceKind = Literal["drive", "github"]


class KnowledgeSourceStatus(BaseModel):
    """One source: how fresh it is, how much it holds, and whether it works."""

    source_id: str = Field(
        ..., description="Stable id for the source: a Drive folder id, or 'owner/repo'."
    )
    kind: KnowledgeSourceKind = Field(..., description="Which connector reads it.")
    enabled: bool = Field(..., description="Whether the sync is configured to read it.")
    last_verified_at: dt.datetime | None = Field(
        default=None,
        description="When the source last answered a check, changes or not.",
    )
    cursor_advanced_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When the cursor last moved. Diagnostics only: staleness does not "
            "key on it, because a quiet source is not a dead sync."
        ),
    )
    stale_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Age of last_verified_at. None when this source never verified.",
    )
    document_count: int = Field(
        ..., ge=0, description="Searchable documents on this source: live, and chunked."
    )
    failing: bool = Field(
        ...,
        description=(
            "This server's conclusion that somebody has to act, which is a "
            "separate fact from the code below and is never derived from it."
        ),
    )
    last_failure_code: str | None = Field(
        default=None,
        description=(
            "The code the sync recorded on its last run, or None. Never "
            "synthesised: an enabled source holding nothing is document_count "
            "0, not a code, and a code here always came from the sync."
        ),
    )
    refusals_by_code: dict[str, int] = Field(
        default_factory=dict,
        description="Per-item refusals on this source, counted by reason.",
    )


class KnowledgeStatus(BaseModel):
    """The whole mirror, and every source under it."""

    schema_version: int | None = Field(
        default=None,
        description="The corpus schema version, or None when it could not be read.",
    )
    schema_supported: bool = Field(
        ...,
        description=(
            "Whether this server reads rows of that shape. False means nothing "
            "below was gathered, rather than that the corpus is empty."
        ),
    )
    document_count: int = Field(
        ..., ge=0, description="Searchable documents across enabled sources."
    )
    chunk_count: int = Field(..., ge=0, description="Indexed chunks across enabled sources.")
    stale_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Age of the oldest enabled source's verification. None when one of "
            "them has never verified, never zero."
        ),
    )
    staleness_warn_seconds: int = Field(
        ...,
        ge=60,
        description="This deployment's threshold for calling the mirror behind.",
    )
    sources_failing: int = Field(
        ...,
        ge=0,
        description=(
            "How many rows below carry failing. Wider than the search "
            "response's counter of the same name, which counts failed runs only."
        ),
    )
    sources: list[KnowledgeSourceStatus] = Field(
        default_factory=list, description="Every configured source, enabled or not."
    )
