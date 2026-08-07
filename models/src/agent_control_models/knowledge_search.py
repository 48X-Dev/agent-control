"""The retrieval contract: what an agent may ask the corpus, and what comes back.

Three verbs exist and no more: ranked search, bounded recency, and the corpus
counters that ride along with both. What is missing is the design. There is no
list request, no cursor, no offset and no wildcard, because wholesale export is
exfiltration shaped like a feature: a list call plus a loop is the whole corpus
in a transcript in an afternoon, defeating every per-call ceiling one page at a
time. The parameter that would enable it is the one that does not exist, in
this module and in ``agent_control_server.knowledge.store``.

``result_count`` and ``external_author_count`` are required on every response
including refusals. That is not tidiness. The shipped deny control selects the
whole result object into the ``json`` evaluator with a ``field_constraints``
key, so a missing field is a "field not found" error, which the evaluator
reports as a match, which denies. A response that omitted them on the refusal
path would fail **open** on the ordinary path and closed on the refusal path,
which is precisely backwards.
"""

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
    """One result, carrying the provenance that makes it checkable by a human.

    Every string here has been neutralized server-side: a filename, a heading
    and a source name are all attacker-choosable, and all three are rendered
    inside the fence header the model reads.
    """

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
    """What the mirror says about itself, on every response including refusals.

    An agent told "no results" deserves to know whether it searched four
    hundred documents or none, and an operator debugging "search finds
    nothing" needs the same fact to tell an empty corpus from a broken query.

    ``measured`` is what stops that helpfulness inverting. The counters carry
    defaults so the block is present on every response, refusals included, for
    the deny control's sake - which means a refusal raised before the store was
    opened carries zeros nobody counted. "These were never measured" and "these
    were measured and the corpus is empty" are otherwise byte-identical on the
    wire, and a reader that cannot tell them apart prints a broken sync under a
    message about a working one.
    """

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
    """Ask what moved, inside a capped window, newest first.

    Not the enumeration search refuses: the window and the k-cap bound it to
    one page of a fortnight, there is no cursor, and asking again returns the
    same page - a loop gains nothing.
    """

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
    """One answer, whether it found something, found nothing, or could not look.

    The same model for all three, so a caller has one shape to read and the
    two counters are present unconditionally. ``refusal_code`` distinguishes
    "could not look" from "looked and found nothing", which are different
    facts and lead to different sentences.
    """

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
