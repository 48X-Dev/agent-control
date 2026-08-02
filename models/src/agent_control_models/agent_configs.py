"""Per-agent runtime configuration: the system prompt and the model.

One row, two fields, one version history. They ship on one mechanism because
they are the same shape - per-agent runtime configuration, stored centrally,
ADMIN-authored, versioned, delivered over the existing refresh channel, applied
at the same point in the same callback. Splitting them would mean two version
counters, two optimistic-concurrency tokens and two audit trails for one page.

Two things about this module are load-bearing rather than stylistic.

**The prompt body lands in ADK's ``config.system_instruction``, which
``extract_request_text`` never reads.** Nothing written here is evaluated by any
control in the deployment. That is correct for authored configuration - a system
prompt belongs in the highest-trust field - and it is exactly why the write
operation is ADMIN and why a saved body may not contain either fence delimiter.

**A model id is a destination selector, not a name.** A ``/`` prefix re-selects
the LiteLLM provider and a configured ``api_base`` is ignored for routing,
verified: ``litellm.get_llm_provider('bedrock/anthropic.claude-v2',
api_base='http://127.0.0.1:10531/v1')`` returns provider ``bedrock``. So an id
containing ``/`` or ``://`` is refused here, again at the server's write
boundary, again by a database check constraint, and again by the SDK before it
constructs anything. Four layers on one field, because a mistake in it sends
customer data to a vendor nobody chose.
"""

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
    """How a stored body should be interpreted.

    One member today, and the check constraint in the database enforces it. It
    exists so that if a prompt ever gains structure - variables, includes, a
    template dialect - restoring an old version fails loudly instead of feeding
    a ``{placeholder}`` to a model as literal text.
    """

    TEXT = "text"


class ConfigEventType(StrEnum):
    """What a version row records.

    ``cleared`` splits into ``PROMPT_CLEARED`` and ``MODEL_CLEARED`` because a
    single value on a two-field row cannot say which field was cleared, and the
    history panel would render "cleared" against a row whose prompt is intact.
    """

    CREATED = "created"
    UPDATED = "updated"
    PROMPT_CLEARED = "prompt_cleared"
    MODEL_CLEARED = "model_cleared"
    RESTORED = "restored"
    ENABLED = "enabled"
    DISABLED = "disabled"


class ConfigOrigin(StrEnum):
    """Where the body in a version row came from.

    ``COPIED_FROM_REPORTED`` exists for the confused deputy that source
    reporting introduces: an agent process registers under an AUTHENTICATED
    operation, so what it reports as its own instruction is untrusted text, and
    an admin who moves it into the editor should leave a trace that says so.
    """

    AUTHORED = "authored"
    COPIED_FROM_REPORTED = "copied_from_reported"
    RESTORED = "restored"


class ModelProvider(StrEnum):
    """Which client the SDK constructs for an allowlisted model.

    The SDK never infers this from the id string. That inference is the
    exfiltration path: ADK's ``LLMRegistry`` resolves bare names by regex, and a
    bare ``gpt-*`` string resolves to a client whose factory is ``AsyncOpenAI()``
    with no base-URL argument, which reads ``OPENAI_BASE_URL`` from the process
    or falls back to OpenAI itself. Being told beats guessing, so the provider
    travels on the wire beside the id.
    """

    GEMINI = "gemini"
    OPENAI_COMPATIBLE = "openai_compatible"


class ModelCostTier(StrEnum):
    """Operator-authored spend banding for an allowlisted model.

    Agent Control prints no currency and no per-token figure: it does not know
    prices, prices change without telling it, and a wrong number beside a Save
    button is worse than no number. The operator wrote these tiers and knows
    what they mean.
    """

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
    """Whether this agent's configuration reaches a running process at all.

    ``BLOCKED_INSECURE_AUTH`` is the startup gate: on a server with credential
    enforcement off, every operation succeeds unauthenticated including ADMIN,
    so nothing here is applied to a running agent. Storage, versioning and the
    audit trail keep working, because a laptop with no credentials configured is
    how everyone first meets this feature.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    BLOCKED_INSECURE_AUTH = "blocked_insecure_auth"


def contains_fence_delimiter(body: str) -> bool:
    """Whether a body carries any of the four fence delimiters.

    Case-insensitive and tolerant of internal whitespace, because
    ``< / AGENT_CONTROL_GUIDANCE >`` is the same forgery as the tidy spelling.
    """
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
    """Reject a model id that is really a destination selector.

    Shape only. Membership of the server allowlist is checked at the write
    boundary, where a rejection can name the allowed values, and re-evaluated on
    every read so that removing an entry from server config does not rewrite
    stored rows.
    """
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
    """One advisory observation recorded when a body was saved.

    Never blocks the write. The value is the record - including the record that
    a human saw a finding and saved anyway. A blocking scan on a field authored
    by admins produces false positives that operators route around, which is
    worse than an advisory note nobody can delete.
    """

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
    """One entry in the server's model allowlist.

    ``recommended`` is named that way rather than ``default`` precisely so
    nobody later wires it to one. A server-side default would silently move
    every unmanaged agent in the deployment the day an operator edited one line
    of config, which destroys the zero-risk rollout the whole feature rests on.

    ``extra="forbid"`` catches the person who reads "model" and writes
    ``"api_base"`` or ``"base_url"`` into an allowlist entry. Ignoring the key
    would leave them believing they had configured a per-agent endpoint, when
    what they had actually done was nothing. There is no such field, here or
    anywhere else in this feature.
    """

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
    """The deployment's model allowlist.

    Deployment-wide and namespace-independent: it names vendors the operator has
    relationships with, not tenant data. That is also why the route requires the
    write operation rather than the read one - at AUTHENTICATED it would be
    cross-tenant vendor reconnaissance readable by any agent process key.
    """

    models: list[AgentModelOption] = Field(default_factory=list)


class GetAgentConfigResponse(BaseModel):
    """One agent's current configuration, resolved against server state.

    ``prompt_source`` and ``model_source`` resolve server-side, once. The SDK
    does not re-derive them, so the gate in section 5 of the design and the
    allowlist membership check are enforced in one place rather than in every
    client.
    """

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
        description=(
            "Reported by the agent process. Unverified. Never sent to a model."
        ),
    )
    source_reported_at: dt.datetime | None = None
    updated_by_hash: str | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class SetAgentConfigRequest(BaseModel):
    """Write both fields, or either one.

    Both optional so a model-only save does not round-trip a 32000-character
    body and a prompt-only save does not have to restate the model. A request
    with neither is rejected: a no-op write that burns a version number is worse
    than a refusal.

    ``extra="forbid"``, matching every other write request on this branch, and
    here it earns it twice. A misspelled ``prompt_enable`` would otherwise fall
    back to the default and switch delivery **on** for a running agent, with a
    200 and a version row saying it was deliberate. And a body carrying
    ``api_base`` or ``base_url`` would be accepted and dropped, teaching the
    caller that a per-agent endpoint exists. It does not.
    """

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
    """Stop using the managed prompt, or the managed model.

    Two verb routes rather than one taking a field list, because the version
    row's ``event_type`` has to name what happened and a list makes it
    ambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class ClearAgentConfigFieldResponse(BaseModel):
    """Whether anything was there to clear.

    Idempotent: clearing an already-null field returns ``cleared=False`` and
    writes no version row, matching the shape ``delete_control_binding_by_key``
    already uses.
    """

    cleared: bool
    version_num: int | None = None
    current_version: int
    etag: str | None = None
    prompt_source: PromptSource = PromptSource.NONE
    model_source: ModelSource = ModelSource.CODE
    delivery_state: DeliveryState = DeliveryState.ACTIVE


class SetPromptEnabledRequest(BaseModel):
    """Switch managed-prompt delivery without touching the body.

    A prompt body is expensive to retype, so a toggle that preserves it earns
    its column. There is deliberately no model equivalent: a model id is one
    dropdown selection preserved in history anyway, and a second boolean would
    only ever mean "the dropdown says X, ignore it".
    """

    model_config = ConfigDict(extra="forbid")

    prompt_enabled: bool
    expected_version: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class RestoreAgentConfigVersionRequest(BaseModel):
    """Copy an old version forward as a new one.

    Version numbers never rewind. A shared history that can be rewritten is a
    history nobody can reason about.
    """

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class AgentConfigVersionSummary(BaseModel):
    """One history row without its body.

    ``model_id`` is included even though the body is not: it is short, and it is
    what most history rows are about.
    """

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
