"""Files attached to a session, and their wire models.

Agent Control stores the bytes and owns their retention. What it deliberately
does not do is open them: nothing in this package parses a document, and the
only thing read out of an uploaded file here is its first sixteen bytes.

Three things run through every model below.

**The bytes are never on the wire in a metadata response.** An attachment is
described by a name, a length and two hashes. Downloading it is a separate
route that streams, forces a download, and is authorized as session content.

**The declared type is advisory.** ``declared_mime`` is whatever the client's
form part said; ``sniffed_mime`` is what the first bytes actually are, and it is
the one the accept gate uses. Both ride the response so a caller can see the
disagreement rather than having it silently resolved.

**A deleted attachment leaves a tombstone rather than a hole.** The bytes go and
the row stays, carrying name, hashes, size and origin, because a transcript that
can no longer answer "what documents did this conversation see" is worse than
one that answers "this one, and its bytes were reclaimed on such a date".
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from .base import BaseModel
from .files import MAX_DISPLAY_NAME_CHARS

ATTACHMENT_KEY_LENGTH = 32
"""``uuid4().hex``. The only attachment identifier a browser ever sees."""

ATTACHMENT_DISPLAY_NAME_MAX_LENGTH = MAX_DISPLAY_NAME_CHARS
"""The same cap the normalizer applies, restated so the column and the wire
model cannot drift apart from it."""

ATTACHMENT_HARD_MAX_BYTES = 52_428_800
"""The ceiling no configuration may exceed, and the ``CHECK`` on both tables.

``attachment_max_bytes`` defaults well below this. This constant is the bound a
direct database write cannot smuggle past, which is the reason it exists as
well as a setting."""

ATTACHMENT_MAX_PER_TURN = 3
"""How many files one turn may carry. Bounds resident memory during delivery
rather than storage: base64 inflates each one by a third inside a process that
is also evaluating policy for every other agent in the deployment.

**A ceiling, not a second source of truth.** ``StartTurnRequest.attachment_keys``
validates its length against this constant and the server enforces its own
``attachment_max_per_turn`` setting, so the setting carries ``le`` against this
number the way ``attachment_max_bytes`` carries ``le`` against
``ATTACHMENT_HARD_MAX_BYTES``. Raising the setting past this without raising
this would refuse the fourth key with a 422 that names no setting and offers no
remedy; the ``le`` makes that a startup refusal instead."""

ATTACHMENT_FAILURE_CODE_MAX_LENGTH = 32
ATTACHMENT_ORIGIN_REF_MAX_LENGTH = 128

AttachmentKey = Annotated[
    str,
    StringConstraints(
        min_length=ATTACHMENT_KEY_LENGTH,
        max_length=ATTACHMENT_KEY_LENGTH,
        pattern=r"^[0-9a-f]{32}$",
    ),
]


class AttachmentStatus(StrEnum):
    """Where one attachment is in its life.

    ``converting`` is reachable only by a deployment running the converter
    sidecar, which is a later phase. It is in the enum now because a status
    value added later means a migration and a client release; an unreached one
    costs nothing.

    ``tombstoned`` is not a soft delete. The metadata row survives on purpose
    and the bytes are gone: a 20MB ``bytea`` behind a ``deleted_at`` would be
    worse than no history, and a 300-byte tombstone is the record anyone
    investigating an injection will want.
    """

    PENDING = "pending"
    CONVERTING = "converting"
    READY = "ready"
    REJECTED = "rejected"
    FAILED = "failed"
    TOMBSTONED = "tombstoned"


class AttachmentOrigin(StrEnum):
    """Who put this file in the conversation.

    Not a heuristic and not derived from anything the file says about itself.
    An operator upload is written by the route the operator called; anything
    fetched from a tracker is written by the fetch path. The difference decides
    whether the file is delivered at all, so it must not be inferable from
    content.
    """

    OPERATOR_UPLOAD = "operator_upload"
    LINEAR = "linear"


class AttachmentVariant(StrEnum):
    """Which artifact of one attachment a blob row holds.

    ``original`` is what was uploaded. The other two are produced by the
    converter and are unreachable until it exists; they are named here so the
    unique constraint on ``(attachment, variant)`` is settled once.
    """

    ORIGINAL = "original"
    DELIVERED_PDF = "delivered_pdf"
    EXTRACTED_TEXT = "extracted_text"


class TurnAttachmentVerdict(StrEnum):
    """What happened to one attachment on one turn.

    The verdict is a fact about one evaluation at one moment, which is why it
    lives on the turn binding rather than on the file. Controls change between
    turns: a ``blocked`` marker on the attachment itself would leave a row
    permanently condemned by a control that may no longer exist.
    """

    PENDING = "pending"
    SENT = "sent"
    BLOCKED = "blocked"


class Attachment(BaseModel):
    """One file attached to one session, described without its bytes."""

    attachment_key: AttachmentKey = Field(
        ..., description="Stable identifier for this attachment within its namespace."
    )
    session_key: str = Field(..., description="The session this file belongs to.")
    display_name: str = Field(
        ...,
        max_length=ATTACHMENT_DISPLAY_NAME_MAX_LENGTH,
        description="Server-normalized filename. Never the string the client sent verbatim.",
    )
    display_name_normalized: bool = Field(
        ...,
        description=(
            "True when normalization changed the supplied name. The original "
            "survives only as a hash: a name that had to be defused is not a "
            "name to render."
        ),
    )
    declared_mime: str = Field(
        ..., description="What the upload claimed. Advisory, never trusted."
    )
    sniffed_mime: str = Field(
        ..., description="What the first bytes actually are. This is what the gate used."
    )
    mime_mismatch: bool = Field(
        ..., description="True when the declared type contradicts the sniff."
    )
    size_bytes: int = Field(..., ge=1, description="Length of the uploaded bytes.")
    source_sha256: str = Field(..., description="SHA-256 over the bytes as uploaded.")
    delivered_sha256: str | None = Field(
        default=None,
        description=(
            "SHA-256 over the bytes actually sent to a model. Equal to "
            "``source_sha256`` unless a converter produced a different artifact."
        ),
    )
    delivered_mime: str | None = None
    delivered_size_bytes: int | None = None
    status: AttachmentStatus
    failure_code: str | None = Field(
        default=None, max_length=ATTACHMENT_FAILURE_CODE_MAX_LENGTH
    )
    page_count: int | None = Field(
        default=None,
        description=(
            "Null until a deployment runs the converter. Counting pages means "
            "opening the file, and this server does not."
        ),
    )
    estimated_tokens: int | None = Field(
        default=None,
        description="Null whenever ``page_count`` is. Always an estimate, never a bound.",
    )
    converted_from: str | None = None
    origin: AttachmentOrigin
    origin_ref: str | None = Field(
        default=None,
        max_length=ATTACHMENT_ORIGIN_REF_MAX_LENGTH,
        description="The tracker's own identifier, for audit and dedupe. Null for uploads.",
    )
    created_at: dt.datetime
    updated_at: dt.datetime


class CreateAttachmentResponse(BaseModel):
    """The attachment an upload produced, and whether it already existed."""

    model_config = ConfigDict(extra="forbid")

    attachment: Attachment
    deduplicated: bool = Field(
        default=False,
        description=(
            "True when these bytes were already on this session and the "
            "existing row was returned. Uploading the same file twice is a "
            "user action with an obvious intent, not a conflict."
        ),
    )


class ListAttachmentsResponse(BaseModel):
    """Every attachment on one session, with the totals the quotas count."""

    model_config = ConfigDict(extra="forbid")

    attachments: list[Attachment]
    count: int
    total_bytes: int = Field(
        ...,
        description=(
            "Sum of ``size_bytes`` over attachments still holding bytes. "
            "Tombstoned rows contribute nothing: their bytes are gone."
        ),
    )


class GetAttachmentResponse(BaseModel):
    """One attachment."""

    model_config = ConfigDict(extra="forbid")

    attachment: Attachment


class DeleteAttachmentResponse(BaseModel):
    """The result of deleting one attachment's bytes."""

    model_config = ConfigDict(extra="forbid")

    deleted: bool
    attachment_key: AttachmentKey
    notice: str = Field(
        ...,
        description=(
            "Says what deletion did and did not do. It removes the file from "
            "this server and from future turns; a model that already read it "
            "has already read it, and the executor's own copy of the "
            "conversation is removed only by deleting the session."
        ),
    )


class AttachmentRefusalCode(StrEnum):
    """Why one file the step found is not in front of the agent.

    Every value maps to one hand-written sentence in the dispatcher's envelope.
    The code is server-authored and the sentence is server-authored; nothing
    upstream writes either, because a parser's or a tracker's own words about a
    file are attacker-influenced text arriving through the channel the whole
    attachment design is guarding.
    """

    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    FETCH_FAILED = "fetch_failed"
    NOT_FOUND = "not_found"
    LINK_ONLY = "link_only"
    BLOCKED_HOST = "blocked_host"
    OVER_PER_ISSUE_CAP = "over_per_issue_cap"
    OVER_TASK_BUDGET = "over_task_budget"
    BLOCKED = "blocked"
    NOT_CONVERTED = "not_converted"
    NO_TEXT = "no_text"

    # The last two are about a file this server holds every byte of, and they
    # exist because "stored" and "readable" are different facts. Delivery to a
    # model is text (nothing else survives the configured endpoint), so a file
    # whose text is not there is a file the agent cannot read, and an envelope
    # calling it delivered while the delivery section beneath says it was not
    # included would leave two server-authored sentences contradicting each
    # other about one file.


class StepAttachmentSummary(BaseModel):
    """What one dispatch step actually carried, kept on the step row.

    The durable record. It survives the session, and it survives the blob TTL
    that reclaims the bytes, which is what still answers "did this step have
    the spec" a week later. No bytes, no text, no URL.

    **A file that never arrived gets a row here too**, carrying its refusal and
    nothing else. Recording only the deliveries would leave the audit trail
    saying the same thing an under-delivering step says to an agent - that
    there was nothing to deliver - which is the failure this whole path exists
    to prevent. So ``sha256``, ``size_bytes`` and ``sniffed_mime`` are optional:
    they are facts about stored bytes, and a refusal has none.
    """

    display_name: str = Field(..., max_length=ATTACHMENT_DISPLAY_NAME_MAX_LENGTH)
    sha256: str | None = None
    size_bytes: int | None = None
    sniffed_mime: str | None = None
    origin: AttachmentOrigin
    origin_ref: str | None = Field(
        default=None, max_length=ATTACHMENT_ORIGIN_REF_MAX_LENGTH
    )
    verdict: TurnAttachmentVerdict
    failure_code: str | None = Field(
        default=None, max_length=ATTACHMENT_FAILURE_CODE_MAX_LENGTH
    )
    attachment_key: AttachmentKey | None = Field(
        default=None,
        description=(
            "The stored file, when there is one. The only identifier for it "
            "that leaves this server: a dispatcher receives keys and never a "
            "tracker URL."
        ),
    )
    text_ready: bool = Field(
        default=False,
        description=(
            "Whether this file's converted text will be in the turn message. "
            "Storing a file and putting it in front of a model are different "
            "events, and only this one means the agent can read it."
        ),
    )
    bytes_fetched: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Bytes pulled over the wire for this file, whatever became of "
            "them. Distinct from size_bytes, which counts only what was "
            "stored: an aborted download, a login page and a type refusal all "
            "cost real bytes and store none, and a per-task byte ceiling that "
            "charged for none of them would bound nothing."
        ),
    )


class StepFilesSummary(BaseModel):
    """Distinct files found on one issue against the ones actually delivered.

    ``found`` is the count of distinct upload URLs the discovery sweep saw, and
    it is deliberately not ``len(files)``: a per-issue cap means the step may
    never attempt most of them, and an envelope that said "1 of 1" about an
    issue carrying twelve would be the confident half-answer the count line
    exists to stop. ``files`` holds the ones this step attempted, delivered or
    refused, bounded by that cap.

    ``delivered`` counts files whose text is actually in the turn message, not
    files that were stored. The two differ, and the count line is the one
    sentence this whole path asks an agent to rely on.

    ``read_failed`` is the third state, and it is the reason this model does
    not simply collapse a failure to zero. A tracker that was down and an issue
    with nothing attached both produce ``found == 0``, and telling an agent
    positively that no files are attached when nobody could look is strictly
    worse than the silence it replaces: it is a server-authored sentence
    backing the wrong conclusion.
    """

    model_config = ConfigDict(extra="forbid")

    found: int = Field(..., ge=0)
    delivered: int = Field(..., ge=0)
    files: list[StepAttachmentSummary] = Field(default_factory=list)
    read_failed: bool = Field(
        default=False,
        description=(
            "The issue's files could not be listed at all. Distinct from an "
            "issue that has none."
        ),
    )
