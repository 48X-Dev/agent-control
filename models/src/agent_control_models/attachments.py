"""Files attached to a session, and their wire models."""

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
    """Where one attachment is in its life."""

    PENDING = "pending"
    CONVERTING = "converting"
    READY = "ready"
    REJECTED = "rejected"
    FAILED = "failed"
    TOMBSTONED = "tombstoned"


class AttachmentOrigin(StrEnum):
    """Who put this file in the conversation."""

    OPERATOR_UPLOAD = "operator_upload"
    LINEAR = "linear"


class AttachmentVariant(StrEnum):
    """Which artifact of one attachment a blob row holds."""

    ORIGINAL = "original"
    DELIVERED_PDF = "delivered_pdf"
    EXTRACTED_TEXT = "extracted_text"


class TurnAttachmentVerdict(StrEnum):
    """What happened to one attachment on one turn."""

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
    declared_mime: str = Field(..., description="What the upload claimed. Advisory, never trusted.")
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
    failure_code: str | None = Field(default=None, max_length=ATTACHMENT_FAILURE_CODE_MAX_LENGTH)
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
    """Why one file the step found is not in front of the agent."""

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
    """What one dispatch step actually carried, kept on the step row."""

    display_name: str = Field(..., max_length=ATTACHMENT_DISPLAY_NAME_MAX_LENGTH)
    sha256: str | None = None
    size_bytes: int | None = None
    sniffed_mime: str | None = None
    origin: AttachmentOrigin
    origin_ref: str | None = Field(default=None, max_length=ATTACHMENT_ORIGIN_REF_MAX_LENGTH)
    verdict: TurnAttachmentVerdict
    failure_code: str | None = Field(default=None, max_length=ATTACHMENT_FAILURE_CODE_MAX_LENGTH)
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
    """Distinct files found on one issue against the ones actually delivered."""

    model_config = ConfigDict(extra="forbid")

    found: int = Field(..., ge=0)
    delivered: int = Field(..., ge=0)
    files: list[StepAttachmentSummary] = Field(default_factory=list)
    read_failed: bool = Field(
        default=False,
        description=(
            "The issue's files could not be listed at all. Distinct from an issue that has none."
        ),
    )
