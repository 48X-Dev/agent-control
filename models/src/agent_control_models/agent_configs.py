"""Per-agent runtime configuration: the system prompt and the model."""

from __future__ import annotations

import datetime as dt
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .base import BaseModel
from .server import PaginationInfo

BODY_MAX_LENGTH = 32_000
MODEL_ID_MAX_LENGTH = 128
NOTE_MAX_LENGTH = 500

#: The four delimiters a saved body may not contain. Opening tags as well as
#: closing ones: without the opening check a nested tag makes the field's
#: structure ambiguous, and without the guidance check a saved body can forge
#: control output that the model would read as Agent Control's own words.
MANAGED_PROMPT_OPEN_TAG = "agent_control_system_prompt"
GUIDANCE_TAG = "agent_control_guidance"

_FENCE_PATTERN = re.compile(
    r"<\s*/?\s*(?:" + MANAGED_PROMPT_OPEN_TAG + "|" + GUIDANCE_TAG + r")\b",
    re.IGNORECASE,
)


class BodyFormat(StrEnum):
    """How a stored body should be interpreted."""

    TEXT = "text"


class ConfigEventType(StrEnum):
    """What a version row records."""

    CREATED = "created"
    UPDATED = "updated"
    PROMPT_CLEARED = "prompt_cleared"
    MODEL_CLEARED = "model_cleared"
    RESTORED = "restored"
    ENABLED = "enabled"
    DISABLED = "disabled"


class ConfigOrigin(StrEnum):
    """Where the body in a version row came from."""

    AUTHORED = "authored"
    COPIED_FROM_REPORTED = "copied_from_reported"
    RESTORED = "restored"


class ModelProvider(StrEnum):
    """Which client the SDK constructs for an allowlisted model."""

    GEMINI = "gemini"
    OPENAI_COMPATIBLE = "openai_compatible"


class ModelCostTier(StrEnum):
    """Operator-authored spend banding for an allowlisted model."""

    ECONOMY = "economy"
    STANDARD = "standard"
    PREMIUM = "premium"


class PromptSource(StrEnum):
    """Which layer supplies the prompt the agent actually runs."""

    MANAGED = "managed"
    CODE = "code"
    NONE = "none"


class ModelSource(StrEnum):
    """Which layer supplies the model the agent actually calls."""

    MANAGED = "managed"
    CODE = "code"


class DeliveryState(StrEnum):
    """Whether this agent's configuration reaches a running process at all."""

    ACTIVE = "active"
    DISABLED = "disabled"
    BLOCKED_INSECURE_AUTH = "blocked_insecure_auth"


def contains_fence_delimiter(body: str) -> bool:
    """Whether a body carries any of the four fence delimiters."""
    return _FENCE_PATTERN.search(body) is not None


def validate_prompt_body(value: str) -> str:
    """Reject a body that is blank or that forges a provenance boundary."""
    if not value.strip():
        raise ValueError(
            "System prompt body must contain non-whitespace text. To stop using "
            "a managed prompt, call the clear-prompt route instead - an empty "
            "string and 'clear this' are different intents."
        )
    if contains_fence_delimiter(value):
        raise ValueError(
            "System prompt body may not contain the Agent Control fence "
            f"delimiters (<{MANAGED_PROMPT_OPEN_TAG}> or <{GUIDANCE_TAG}>, "
            "opening or closing). Those tags mark which text is operator "
            "configuration and which is control-authored guidance; a body that "
            "carries one can forge either boundary."
        )
    return value


def validate_model_id_shape(value: str) -> str:
    """Reject a model id that is really a destination selector."""
    if "://" in value:
        raise ValueError(
            "Model id may not contain '://'. This field names a model, not an "
            "endpoint. The endpoint comes from the executor process's own "
            "environment (AGENT_CONTROL_MODEL_BASE_URL or OPENAI_BASE_URL) and "
            "there is no per-agent endpoint."
        )
    if "/" in value:
        raise ValueError(
            "Model id may not contain '/'. A slash prefix re-selects the "
            "LiteLLM provider and the configured api_base is ignored for "
            "routing, so a slashed id sends traffic to a vendor of the writer's "
            "choosing while the UI still names the configured endpoint."
        )
    if value != value.strip():
        raise ValueError("Model id may not have leading or trailing whitespace.")
    return value


PromptBody = Annotated[
    str,
    StringConstraints(min_length=1, max_length=BODY_MAX_LENGTH),
]
ModelId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MODEL_ID_MAX_LENGTH),
]


class ScanFinding(BaseModel):
    """One advisory observation recorded when a body was saved."""

    scanner: str = Field(description="Which check produced this, e.g. 'secret_pattern'.")
    severity: Literal["info", "warning"] = Field(
        default="warning", description="Advisory weight. Nothing here blocks a save."
    )
    code: str = Field(description="Stable machine-readable finding id.")
    message: str = Field(description="What was observed, in words an operator can act on.")
    # Deliberately no matched text: a finding on a secret-shaped string would
    # otherwise copy the secret into the version row and the API response.
    match_count: int = Field(default=1, ge=1, description="How many times it matched.")


class AgentModelOption(BaseModel):
    """One entry in the server's model allowlist."""

    model_config = ConfigDict(extra="forbid")

    id: ModelId
    label: str = Field(max_length=128)
    provider: ModelProvider
    cost_tier: ModelCostTier
    recommended: bool = False

    @field_validator("id")
    @classmethod
    def _check_id_shape(cls, value: str) -> str:
        return validate_model_id_shape(value)


class ListAgentModelsResponse(BaseModel):
    """The deployment's model allowlist."""

    models: list[AgentModelOption] = Field(default_factory=list)


class GetAgentConfigResponse(BaseModel):
    """One agent's current configuration, resolved against server state."""

    agent_name: str
    body: str | None = Field(default=None, description="None when unmanaged or cleared.")
    body_format: BodyFormat = BodyFormat.TEXT
    prompt_enabled: bool = True
    prompt_source: PromptSource = PromptSource.NONE

    model_id: str | None = None
    model_provider: ModelProvider | None = Field(
        default=None,
        description=(
            "Resolved from the allowlist on every read; null when the stored id "
            "is no longer offered. The SDK refuses to construct anything when "
            "this is absent rather than inferring a provider from the id."
        ),
    )
    model_allowed: bool = Field(
        default=True, description="False when the stored id has left the server allowlist."
    )
    model_cost_tier: ModelCostTier | None = None
    model_source: ModelSource = ModelSource.CODE

    delivery_state: DeliveryState = DeliveryState.ACTIVE
    etag: str | None = Field(
        default=None,
        description=(
            "Opaque, server-issued, covering both fields, so a model-only change "
            "produces a new value. Echoed onto control execution events."
        ),
    )
    current_version: int = Field(default=0, ge=0)

    source_instruction: str | None = Field(
        default=None,
        description=("Reported by the agent process. Unverified. Never sent to a model."),
    )
    source_reported_at: dt.datetime | None = None
    updated_by_hash: str | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class SetAgentConfigRequest(BaseModel):
    """Write both fields, or either one."""

    model_config = ConfigDict(extra="forbid")

    body: PromptBody | None = None
    model_id: ModelId | None = None
    expected_version: int = Field(
        ge=0,
        description=(
            "The current_version the editor loaded. Compared under a row lock; a "
            "mismatch is a 409 carrying the real version, so a concurrent edit "
            "fails loudly instead of overwriting a colleague's paragraph."
        ),
    )
    # Only the two authoring origins are writable. ``restored`` is stamped by
    # the restore route, so a caller cannot label an ordinary save as one.
    origin: Literal["authored", "copied_from_reported"] = "authored"
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    prompt_enabled: bool = True

    @field_validator("body")
    @classmethod
    def _check_body(cls, value: str | None) -> str | None:
        return None if value is None else validate_prompt_body(value)

    @field_validator("model_id")
    @classmethod
    def _check_model_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_model_id_shape(value)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> SetAgentConfigRequest:
        if self.body is None and self.model_id is None:
            raise ValueError(
                "Supply a body, a model_id, or both. A write carrying neither "
                "would burn a version number without changing anything."
            )
        return self


class SetAgentConfigResponse(BaseModel):
    """What a write produced, including anything the save-time scan noticed."""

    success: bool = True
    version_num: int
    current_version: int
    etag: str | None = None
    prompt_source: PromptSource = PromptSource.NONE
    model_source: ModelSource = ModelSource.CODE
    delivery_state: DeliveryState = DeliveryState.ACTIVE
    scan_findings: list[ScanFinding] = Field(default_factory=list)


class ClearAgentConfigFieldRequest(BaseModel):
    """Stop using the managed prompt, or the managed model."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class ClearAgentConfigFieldResponse(BaseModel):
    """Whether anything was there to clear."""

    cleared: bool
    version_num: int | None = None
    current_version: int
    etag: str | None = None
    prompt_source: PromptSource = PromptSource.NONE
    model_source: ModelSource = ModelSource.CODE
    delivery_state: DeliveryState = DeliveryState.ACTIVE


class SetPromptEnabledRequest(BaseModel):
    """Switch managed-prompt delivery without touching the body."""

    model_config = ConfigDict(extra="forbid")

    prompt_enabled: bool
    expected_version: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class RestoreAgentConfigVersionRequest(BaseModel):
    """Copy an old version forward as a new one."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class AgentConfigVersionSummary(BaseModel):
    """One history row without its body."""

    version_num: int
    event_type: ConfigEventType
    origin: ConfigOrigin
    model_id: str | None = None
    note: str | None = None
    has_body: bool = False
    scan_findings: list[ScanFinding] = Field(default_factory=list)
    changed_by_hash: str | None = Field(
        default=None,
        description=(
            "Identifies a credential, not a person. Under the shipped default "
            "provider every dashboard caller hashes to the same value."
        ),
    )
    created_at: dt.datetime


class AgentConfigVersionDetail(AgentConfigVersionSummary):
    """One history row with its body."""

    body: str | None = None
    body_format: BodyFormat = BodyFormat.TEXT
    etag: str | None = None


class ListAgentConfigVersionsResponse(BaseModel):
    """Newest-first cursor page of version summaries."""

    versions: list[AgentConfigVersionSummary] = Field(default_factory=list)
    pagination: PaginationInfo = Field(..., description="Pagination metadata")


class GetAgentConfigVersionResponse(BaseModel):
    """One full version row."""

    version: AgentConfigVersionDetail
