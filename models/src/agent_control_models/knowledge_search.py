"""The retrieval contract: what an agent may ask the corpus, and what comes back."""

from __future__ import annotations

import datetime as dt

from pydantic import Field

from .base import BaseModel
from .knowledge import KnowledgeRefusalCode

QUERY_MIN_CHARS = 3
"""Below this, full-text rank is noise. Refused with a code the tool turns into
a sentence a model can act on, never with an empty result that reads like an
answer."""

QUERY_MAX_CHARS = 500
"""Above this the caller is not asking a question. Also refused typed."""

QUERY_BODY_MAX_CHARS = 4000
"""The outer bound on the field itself, well above the typed refusal.

Two bounds rather than one, on purpose. 3 to 500 is the contract, and a query
outside it comes back as a stated refusal a model can correct itself from. This
larger number only stops a megabyte of text being parsed and validated on its
way to being refused; a request that trips it gets an ordinary 422, because
nothing at that size is a question."""

MAX_RESULTS_REQUEST_CEILING = 100
"""Bound on the requested k before the server clamps it to its own hard cap.

The clamp, not this number, is the contract (plan 8.3). This exists so the
field cannot carry an absurd integer into the clamp arithmetic."""

RECENT_DAYS_REQUEST_CEILING = 365
"""Bound on the requested window before the server clamps it to
``recent_window_days_max``. Same reasoning as above: the clamp is the
contract, and this stops the arithmetic being handed a decade."""


class KnowledgeSnippet(BaseModel):
    """One result, carrying the provenance that makes it checkable by a human."""

    snippet: str = Field(..., description="Matching text, truncated with a stated marker.")
    path: str = Field(..., description="Full path of the document inside its source.")
    heading_path: str | None = Field(
        default=None,
        description="Heading trail of the matching section, when the document had headings.",
    )
    title: str = Field(..., description="Document title, normalized at index time.")
    source_kind: str = Field(..., description="'drive_folder' or 'github_repo'.")
    source_name: str = Field(..., description="Operator's display name for the source.")
    author_kind: str = Field(
        ...,
        description=(
            "'workspace', 'external' or 'unknown'. What external_author_count "
            "is counted from, and what a post control keys on."
        ),
    )
    modified_at: dt.datetime | None = Field(
        default=None, description="When the source document last changed."
    )
    synced_at: dt.datetime = Field(..., description="When the mirror last copied it.")


class KnowledgeCorpus(BaseModel):
    """What the mirror says about itself, on every response including refusals."""

    documents: int = Field(default=0, ge=0, description="Searchable documents.")
    sources: int = Field(default=0, ge=0, description="Enabled sources.")
    sources_failing: int = Field(
        default=0, ge=0, description="Enabled sources whose last run failed."
    )
    last_sync_at: dt.datetime | None = Field(
        default=None, description="Most recent verification across enabled sources."
    )
    stale_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Age of the *oldest* enabled source's verification. None when it "
            "cannot be computed, never zero: a source that has never verified "
            "is not a fresh source."
        ),
    )
    measured: bool = Field(
        default=False,
        description=(
            "Whether the counters above were read from the store. False on a "
            "refusal raised before it was opened, where they are this model's "
            "defaults rather than a reading."
        ),
    )
    staleness_warn_seconds: int = Field(
        default=86400,
        ge=60,
        description=(
            "This deployment's threshold for calling the mirror behind. Not a "
            "measurement: it is here so a surface rendering the age applies "
            "the operator's number rather than its own copy of the default."
        ),
    )


class KnowledgeSearchRequest(BaseModel):
    """Ask the corpus a question. Two scalars, and nothing that pages."""

    query: str = Field(
        ...,
        max_length=QUERY_BODY_MAX_CHARS,
        description=(
            "What to search for. Between 3 and 500 characters; outside that "
            "the response is a stated refusal, not an empty result."
        ),
    )
    max_results: int | None = Field(
        default=None,
        ge=1,
        le=MAX_RESULTS_REQUEST_CEILING,
        description="How many results to return. Clamped to the server's hard cap.",
    )


class KnowledgeRecentRequest(BaseModel):
    """Ask what moved, inside a capped window, newest first."""

    days: int | None = Field(
        default=None,
        ge=1,
        le=RECENT_DAYS_REQUEST_CEILING,
        description="How far back to look. Clamped to the server's window ceiling.",
    )
    max_results: int | None = Field(
        default=None,
        ge=1,
        le=MAX_RESULTS_REQUEST_CEILING,
        description="How many results to return. Clamped to the server's hard cap.",
    )


class KnowledgeSearchResponse(BaseModel):
    """One answer, whether it found something, found nothing, or could not look."""

    results: list[KnowledgeSnippet] = Field(default_factory=list)
    result_count: int = Field(..., ge=0, description="How many results are in this response.")
    external_author_count: int = Field(
        ...,
        ge=0,
        description=(
            "Results not authored inside the workspace. 'unknown' authorship "
            "counts here: a deny control keying on this must not read "
            "'we could not tell' as 'safe'."
        ),
    )
    corpus: KnowledgeCorpus = Field(default_factory=KnowledgeCorpus)
    refusal_code: KnowledgeRefusalCode | None = Field(
        default=None, description="Why the search did not run, from the closed enum."
    )
    retry_after_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Set only on rate_limited: when it is worth asking again.",
    )
