"""Standardized error models for Agent Control API."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from .base import BaseModel


class ErrorCode(StrEnum):
    """Standardized error codes following OPA-style semantic naming."""

    # Authentication & Authorization (1xx pattern in code)
    AUTH_MISSING_KEY = "AUTH_MISSING_KEY"
    AUTH_INVALID_KEY = "AUTH_INVALID_KEY"
    AUTH_INSUFFICIENT_PRIVILEGES = "AUTH_INSUFFICIENT_PRIVILEGES"
    AUTH_MISCONFIGURED = "AUTH_MISCONFIGURED"
    AUTH_UPSTREAM_REJECTED = "AUTH_UPSTREAM_REJECTED"

    # Resource Not Found (2xx pattern)
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"  # Generic fallback
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    CONTROL_NOT_FOUND = "CONTROL_NOT_FOUND"
    CONTROL_VERSION_NOT_FOUND = "CONTROL_VERSION_NOT_FOUND"
    CONTROL_BINDING_NOT_FOUND = "CONTROL_BINDING_NOT_FOUND"
    EVALUATOR_NOT_FOUND = "EVALUATOR_NOT_FOUND"
    TEAM_NOT_FOUND = "TEAM_NOT_FOUND"
    AGENT_SESSION_NOT_FOUND = "AGENT_SESSION_NOT_FOUND"
    NUDGE_NOT_FOUND = "NUDGE_NOT_FOUND"
    HALT_NOT_FOUND = "HALT_NOT_FOUND"
    AGENT_CONFIG_NOT_FOUND = "AGENT_CONFIG_NOT_FOUND"
    AGENT_TASK_NOT_FOUND = "AGENT_TASK_NOT_FOUND"
    # A step index with no row on that task. Distinct from an out-of-range
    # index, which is a validation failure the request model catches first.
    AGENT_TASK_STEP_NOT_FOUND = "AGENT_TASK_STEP_NOT_FOUND"
    # A workflow key with no row in this namespace. The implicit one-step
    # workflow every team gets by default is not a row and never reaches this:
    # it is the fallback for a task whose workflow_key was never set to
    # anything else.
    AGENT_WORKFLOW_NOT_FOUND = "AGENT_WORKFLOW_NOT_FOUND"
    # An agent that never declared a plan is the ordinary case on a read, which
    # answers with an empty plan rather than this. This is for a step update
    # against a session that has no plan at all to update.
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    # An attachment key that names nothing in this namespace, or names
    # something on another caller's session. The two are indistinguishable on
    # purpose: whether a key exists elsewhere is not this caller's business.
    ATTACHMENT_NOT_FOUND = "ATTACHMENT_NOT_FOUND"

    # Conflict Errors (3xx pattern)
    AGENT_NAME_CONFLICT = "AGENT_NAME_CONFLICT"
    POLICY_NAME_CONFLICT = "POLICY_NAME_CONFLICT"
    CONTROL_NAME_CONFLICT = "CONTROL_NAME_CONFLICT"
    CONTROL_BINDING_CONFLICT = "CONTROL_BINDING_CONFLICT"
    EVALUATOR_NAME_CONFLICT = "EVALUATOR_NAME_CONFLICT"
    CONTROL_IN_USE = "CONTROL_IN_USE"
    CONTROL_TEMPLATE_CONFLICT = "CONTROL_TEMPLATE_CONFLICT"
    EVALUATOR_IN_USE = "EVALUATOR_IN_USE"
    SCHEMA_INCOMPATIBLE = "SCHEMA_INCOMPATIBLE"
    TEAM_HAS_MEMBERS = "TEAM_HAS_MEMBERS"
    AGENT_RUNTIME_NOT_BOUND = "AGENT_RUNTIME_NOT_BOUND"
    # The team carries no linear_team_key, so there is no scope to read issues
    # against. A conflict rather than a 404: the team exists and the request was
    # well formed, and what is missing is a link somebody has to make on purpose.
    TEAM_NOT_LINKED = "TEAM_NOT_LINKED"
    # Optimistic concurrency on the agent config row. One row carries the
    # system prompt and the model, so a prompt edit and a model edit conflict
    # with each other - which is correct, they are one version.
    AGENT_CONFIG_VERSION_CONFLICT = "AGENT_CONFIG_VERSION_CONFLICT"
    # A session runs one turn at a time. A second turn against a session that
    # already has one in flight is a conflict rather than a queue: the caller
    # has to decide whether to wait or to stop what is running.
    TURN_IN_FLIGHT = "TURN_IN_FLIGHT"
    # The mirror image, and a conflict for the same reason: whether a turn is
    # running is state, not something the caller got wrong. A halt is bound to
    # one turn, so a session running nothing has nothing to bind to.
    TURN_NOT_IN_FLIGHT = "TURN_NOT_IN_FLIGHT"
    # A step update naming a plan that has since been re-declared. A conflict
    # rather than a validation failure: the request was well formed and would
    # have been correct a moment earlier. Guessing "they must mean the latest
    # plan" marks a step of the new plan done because a step of the old one
    # finished.
    PLAN_REVISION_STALE = "PLAN_REVISION_STALE"
    # The claim went to somebody else. Zero rows came back from the atomic
    # claim statement, which is the only honest thing it can say: the caller
    # did not lose a check, it lost a race, and the answer is to move on to the
    # next task rather than to retry this one.
    TASK_ALREADY_CLAIMED = "TASK_ALREADY_CLAIMED"
    # A write from something that is not the current claim holder. Either the
    # lease expired and another dispatcher took the task, or the caller never
    # held it. Both mean the same thing to the caller: stop writing.
    TASK_NOT_CLAIMED = "TASK_NOT_CLAIMED"
    # A transition the status machine does not have. Retrying a blocked task,
    # finishing a task that was never claimed, starting a step on a completed
    # task. State, not a malformed request, so a conflict rather than a 422.
    TASK_STATUS_CONFLICT = "TASK_STATUS_CONFLICT"
    # ``deadline_at`` passed. Set by the server at claim time and checked
    # before each step starts, so a hung dispatcher cannot outlive it.
    TASK_DEADLINE_EXCEEDED = "TASK_DEADLINE_EXCEEDED"
    # The eligible set moved between the preview the operator read and the
    # commit they pressed. A digest over the sorted refs, so four items swapped
    # for four others fails too, where a count would not.
    SCOPE_CHANGED = "SCOPE_CHANGED"
    # The approving credential is the one that ran the agents: it equals the
    # task's claimed_by_hash, or the created_by_hash on a session belonging to
    # the task. A refusal rather than an access tier, because the local
    # credential path has three tiers and no per-key operation allowlist, so
    # "may run agents, may not accept their work" is not expressible as a tier.
    SELF_APPROVAL_REFUSED = "SELF_APPROVAL_REFUSED"
    # The write-back's output text, target issue, or resolved completed state
    # moved between the review card the human read and the accept they pressed.
    # The digest covers all three, because a reviewer is accountable for the
    # mutation they authorised, not only for the text they read.
    DECISION_CHANGED = "DECISION_CHANGED"
    # A writeback id that names nothing on this task. Steps and writebacks
    # share the pattern: the id is scoped to the task in the path.
    AGENT_TASK_WRITEBACK_NOT_FOUND = "AGENT_TASK_WRITEBACK_NOT_FOUND"
    # The deployment has AGENT_CONTROL_LINEAR_WRITE_ENABLED off, which is the
    # shipped default. A conflict rather than a 403: the caller's credentials
    # are fine, and the same request succeeds unchanged once an operator turns
    # the flag on.
    LINEAR_WRITE_DISABLED = "LINEAR_WRITE_DISABLED"
    # The session has no tracker issue to comment on: it was opened as a plain
    # chat rather than for a dispatch task, or its task came from a source with
    # nothing to write to, or that task is a dry run. A conflict because the
    # credentials are fine and the session is simply not that kind of session.
    # The agent is told which of the three it is, because "save this" is a
    # reasonable thing to ask and the useful answer names the reason.
    SESSION_HAS_NO_TRACKER_ISSUE = "SESSION_HAS_NO_TRACKER_ISSUE"
    # Linear could not be read or written at the moment an accept needed it.
    # Nothing was changed; the proposal keeps waiting and the same press works
    # once Linear answers.
    LINEAR_UNAVAILABLE = "LINEAR_UNAVAILABLE"
    # Level 1 of the fleet stop. New work is refused: import, claim, and every
    # dispatch-origin turn. A conflict rather than a 403, because the caller's
    # credentials are fine and the namespace's state is not - and because the
    # same request succeeds unchanged once somebody clears the flag.
    DISPATCH_PAUSED = "DISPATCH_PAUSED"
    # Level 3, the authoritative stop. Refuses every new session and every new
    # turn in the namespace, human chat included. Distinct from the pause so a
    # console can tell an operator which switch is thrown, and so clearing one
    # cannot be mistaken for clearing the other.
    EXECUTORS_HALTED = "EXECUTORS_HALTED"
    # One agent is running as many dispatch turns at once as this deployment
    # allows, which is one: the plugin's concurrent-invocation safety has never
    # been demonstrated. A conflict rather than a capacity error, because the
    # thing in the way is a specific turn rather than a rate.
    AGENT_CONCURRENCY_EXCEEDED = "AGENT_CONCURRENCY_EXCEEDED"
    # A step of a workflow that names no agent, on a team that has no
    # default_agent_name. Refused rather than guessed at: agents differ in
    # system prompt, in bound controls and in tools, so picking one is picking
    # the blast radius. Raised at import, before any row is created, so the
    # failure is four words on a confirm screen rather than four blocked tasks
    # and four identical comments on somebody's issues.
    NO_AGENT_SELECTED = "NO_AGENT_SELECTED"
    # The named agent is not a member of the team it is being made the default
    # for. A conflict rather than a validation failure: both rows exist and the
    # request was well formed; what is missing is a membership somebody has to
    # add on purpose.
    AGENT_NOT_IN_TEAM = "AGENT_NOT_IN_TEAM"
    # A turn naming an attachment that is not ``ready``. State rather than a
    # malformed request: the same call succeeds unchanged once conversion
    # finishes, and sending a file whose bytes are still being decided is how a
    # model reads half a document.
    ATTACHMENT_NOT_READY = "ATTACHMENT_NOT_READY"

    # Validation Errors (4xx pattern)
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_CONFIG = "INVALID_CONFIG"
    # A model id that is well formed but not offered by this deployment.
    # 400 on a save, where the caller can pick another; 409 on a restore,
    # where the caller asked for a state the server no longer understands.
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    CORRUPTED_DATA = "CORRUPTED_DATA"
    POLICY_CONTROL_INCOMPATIBLE = "POLICY_CONTROL_INCOMPATIBLE"
    CONTROL_BINDING_INCOMPATIBLE = "CONTROL_BINDING_INCOMPATIBLE"
    TEMPLATE_PARAMETER_INVALID = "TEMPLATE_PARAMETER_INVALID"
    TEMPLATE_RENDER_ERROR = "TEMPLATE_RENDER_ERROR"
    # Marking step 7 of a five-step plan. Refused whole: a step that does not
    # exist cannot be half written, and an agent whose index is wrong should
    # learn that rather than have a neighbouring step marked for it.
    PLAN_STEP_OUT_OF_RANGE = "PLAN_STEP_OUT_OF_RANGE"
    # The sniffed type is not one this deployment accepts. A 415, and it names
    # both the declared type and the sniffed one, because "your PDF is a ZIP"
    # is the only version of this message anyone can act on.
    ATTACHMENT_REJECTED = "ATTACHMENT_REJECTED"
    # A body past the byte cap, whether the header said so or the stream did.
    # 413 either way: a small ``Content-Length`` over a large body is aborted
    # mid-stream on the counted total, not on what the header promised.
    ATTACHMENT_TOO_LARGE = "ATTACHMENT_TOO_LARGE"
    # Where a file came from, refused. A host outside the allowlist, a link
    # rather than a file, a scheme that is not HTTPS. Deliberately not folded
    # into ATTACHMENT_REJECTED: "we will not go and get that" and "we looked
    # and it is the wrong type" are different facts, and only one of them is
    # about the content.
    ATTACHMENT_SOURCE_REFUSED = "ATTACHMENT_SOURCE_REFUSED"

    # Capacity (429)
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    # The namespace has spent its hourly allowance of dispatch-origin turns, or
    # its hourly allowance of imported tasks. Separate from QUOTA_EXCEEDED,
    # which is a per-credential rate on a sliding minute held in one process:
    # this one is a namespace ceiling counted in Postgres, and it is the number
    # that bounds an autonomous loop rather than a chatty client.
    DISPATCH_BUDGET_EXCEEDED = "DISPATCH_BUDGET_EXCEEDED"

    # Server Errors (5xx pattern)
    DATABASE_ERROR = "DATABASE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    EVALUATION_FAILED = "EVALUATION_FAILED"
    # The executor is the process that runs an agent. It is a separate service,
    # so its failures are reported as its own, and the detail text is written
    # here rather than lifted from whatever the executor returned.
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"
    EXECUTOR_REJECTED = "EXECUTOR_REJECTED"


class ErrorReason(StrEnum):
    """Kubernetes-style reason codes for error categorization."""

    # Client errors
    NOT_FOUND = "NotFound"
    ALREADY_EXISTS = "AlreadyExists"
    CONFLICT = "Conflict"
    INVALID = "Invalid"
    FORBIDDEN = "Forbidden"
    UNAUTHORIZED = "Unauthorized"
    BAD_REQUEST = "BadRequest"
    UNPROCESSABLE_ENTITY = "UnprocessableEntity"

    # Server errors
    INTERNAL_ERROR = "InternalError"
    SERVICE_UNAVAILABLE = "ServiceUnavailable"
    UNKNOWN = "Unknown"


class ValidationErrorItem(BaseModel):
    """GitHub-style validation error item."""

    resource: str = Field(
        ...,
        description="The resource type where the error occurred (e.g., 'Agent', 'Control')",
    )
    field: str | None = Field(
        default=None,
        description="The field that caused the error (e.g., 'name', 'config.threshold')",
    )
    code: str = Field(
        ...,
        description="Machine-readable error code for this specific validation (e.g., 'required', "
        "'invalid_format', 'too_long')",
    )
    message: str = Field(
        ...,
        description="Human-readable description of what went wrong",
    )
    value: Any | None = Field(
        default=None,
        description="The invalid value that was provided (omitted for sensitive data)",
    )
    parameter: str | None = Field(
        default=None,
        description="Template parameter key when the error maps to a template input",
    )
    parameter_label: str | None = Field(
        default=None,
        description="Human-readable template parameter label for template-aware errors",
    )
    rendered_field: str | None = Field(
        default=None,
        description="Rendered control field path that produced the validation error",
    )


class ErrorMetadata(BaseModel):
    """Metadata about the error occurrence."""

    request_id: str | None = Field(
        default=None,
        description="Unique identifier for the request (for log correlation)",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="ISO 8601 timestamp when the error occurred",
    )
    retry_after: int | None = Field(
        default=None,
        description="Suggested seconds to wait before retrying (for rate limits)",
    )


class ErrorDetails(BaseModel):
    """Additional structured details about the error."""

    name: str | None = Field(
        default=None,
        description="Name of the resource that caused the error",
    )
    kind: str | None = Field(
        default=None,
        description="Kind/type of the resource (e.g., 'Agent', 'Policy', 'Control')",
    )
    causes: list[ValidationErrorItem] | None = Field(
        default=None,
        description="List of underlying causes for this error",
    )
    retry_after_seconds: int | None = Field(
        default=None,
        description="Suggested retry interval in seconds",
    )


class ProblemDetail(BaseModel):
    """RFC 7807 Problem Details with Kubernetes and GitHub extensions."""

    # RFC 7807 core fields
    type: str = Field(
        default="about:blank",
        description="A URI reference that identifies the problem type. "
        "When dereferenced, should provide human-readable documentation.",
    )
    title: str = Field(
        ...,
        description="A short, human-readable summary of the problem type. "
        "Should not change between occurrences.",
    )
    status: int = Field(
        ...,
        description="The HTTP status code for this occurrence of the problem.",
    )
    detail: str = Field(
        ...,
        description="A human-readable explanation specific to this occurrence of the problem.",
    )
    instance: str | None = Field(
        default=None,
        description="A URI reference that identifies the specific occurrence of the problem. "
        "Typically the request path.",
    )

    # OPA-style semantic error code
    error_code: ErrorCode = Field(
        ...,
        description="Machine-readable error code following OPA-style semantic naming.",
    )

    # Kubernetes-style fields
    kind: str = Field(
        default="Status",
        description="Kubernetes-style kind identifier. Always 'Status' for errors.",
    )
    api_version: str = Field(
        default="v1",
        description="API version that generated this error.",
    )
    reason: ErrorReason = Field(
        ...,
        description="Kubernetes-style reason code for error categorization.",
    )
    metadata: ErrorMetadata | None = Field(
        default=None,
        description="Metadata about this error occurrence.",
    )

    # GitHub-style validation errors
    errors: list[ValidationErrorItem] | None = Field(
        default=None,
        description="Array of validation errors (GitHub-style). "
        "Populated for validation failures with field-level details.",
    )

    # Additional context
    details: ErrorDetails | None = Field(
        default=None,
        description="Kubernetes-style additional details about the error.",
    )

    # Hint for resolution
    hint: str | None = Field(
        default=None,
        description="Actionable suggestion for resolving the error.",
    )

    # Documentation link
    documentation_url: str | None = Field(
        default=None,
        description="URL to relevant documentation for this error type.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "type": "https://agent-control.dev/errors/not-found",
                    "title": "Resource Not Found",
                    "status": 404,
                    "detail": "Agent with name 'customer-service-bot' not found",
                    "instance": "/api/v1/agents/customer-service-bot",
                    "error_code": "AGENT_NOT_FOUND",
                    "kind": "Status",
                    "api_version": "v1",
                    "reason": "NotFound",
                    "metadata": {
                        "request_id": "req-abc123",
                        "timestamp": "2025-01-15T10:30:00Z",
                    },
                    "errors": None,
                    "hint": "Verify the agent ID is correct and the agent has been registered.",
                },
                {
                    "type": "https://agent-control.dev/errors/validation-error",
                    "title": "Validation Error",
                    "status": 422,
                    "detail": "Request validation failed with 2 errors",
                    "instance": "/api/v1/controls/42/data",
                    "error_code": "VALIDATION_ERROR",
                    "kind": "Status",
                    "api_version": "v1",
                    "reason": "Invalid",
                    "metadata": {
                        "timestamp": "2025-01-15T10:30:00Z",
                    },
                    "errors": [
                        {
                            "resource": "Control",
                            "field": "data.evaluator.config.threshold",
                            "code": "type_error",
                            "message": "Expected number, got string",
                            "value": "high",
                        },
                        {
                            "resource": "Control",
                            "field": "data.evaluator.name",
                            "code": "not_found",
                            "message": "Evaluator 'nonexistent' not registered",
                        },
                    ],
                    "hint": "Check the evaluator configuration against the schema.",
                },
            ]
        }
    }


# Error type URI base
ERROR_TYPE_BASE = "https://agentcontrol.dev/errors"


def make_error_type(error_code: ErrorCode) -> str:
    """Generate a standardized error type URI from an error code."""
    # Convert AGENT_NOT_FOUND to agent-not-found
    slug = error_code.value.lower().replace("_", "-")
    return f"{ERROR_TYPE_BASE}/{slug}"


# Pre-defined error titles for common error codes
ERROR_TITLES: dict[ErrorCode, str] = {
    # Auth errors
    ErrorCode.AUTH_MISSING_KEY: "Authentication Required",
    ErrorCode.AUTH_INVALID_KEY: "Invalid API Key",
    ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES: "Insufficient Privileges",
    ErrorCode.AUTH_MISCONFIGURED: "Authentication Misconfigured",
    ErrorCode.AUTH_UPSTREAM_REJECTED: "Authorization Upstream Rejected Request",
    # Not found errors
    ErrorCode.RESOURCE_NOT_FOUND: "Resource Not Found",
    ErrorCode.AGENT_NOT_FOUND: "Agent Not Found",
    ErrorCode.POLICY_NOT_FOUND: "Policy Not Found",
    ErrorCode.CONTROL_NOT_FOUND: "Control Not Found",
    ErrorCode.CONTROL_VERSION_NOT_FOUND: "Control Version Not Found",
    ErrorCode.CONTROL_BINDING_NOT_FOUND: "Control Binding Not Found",
    ErrorCode.EVALUATOR_NOT_FOUND: "Evaluator Not Found",
    ErrorCode.TEAM_NOT_FOUND: "Team Not Found",
    ErrorCode.AGENT_SESSION_NOT_FOUND: "Agent Session Not Found",
    ErrorCode.NUDGE_NOT_FOUND: "Nudge Not Found",
    ErrorCode.AGENT_CONFIG_NOT_FOUND: "Agent Configuration Not Found",
    ErrorCode.HALT_NOT_FOUND: "Halt Not Found",
    ErrorCode.PLAN_NOT_FOUND: "No Plan Was Declared",
    ErrorCode.ATTACHMENT_NOT_FOUND: "Attachment Not Found",
    # Conflict errors
    ErrorCode.AGENT_NAME_CONFLICT: "Agent Name Already Exists",
    ErrorCode.POLICY_NAME_CONFLICT: "Policy Name Already Exists",
    ErrorCode.CONTROL_NAME_CONFLICT: "Control Name Already Exists",
    ErrorCode.CONTROL_BINDING_CONFLICT: "Control Binding Already Exists",
    ErrorCode.EVALUATOR_NAME_CONFLICT: "Evaluator Name Conflict",
    ErrorCode.CONTROL_IN_USE: "Control In Use",
    ErrorCode.CONTROL_TEMPLATE_CONFLICT: "Control Template Conflict",
    ErrorCode.EVALUATOR_IN_USE: "Evaluator In Use",
    ErrorCode.SCHEMA_INCOMPATIBLE: "Schema Incompatible",
    ErrorCode.TEAM_HAS_MEMBERS: "Team Still Has Members",
    ErrorCode.AGENT_RUNTIME_NOT_BOUND: "Agent Has No Executor Binding",
    ErrorCode.AGENT_CONFIG_VERSION_CONFLICT: "Agent Configuration Was Changed",
    ErrorCode.TURN_IN_FLIGHT: "A Turn Is Already Running",
    ErrorCode.TURN_NOT_IN_FLIGHT: "No Turn Is Running",
    ErrorCode.PLAN_REVISION_STALE: "The Plan Was Revised",
    ErrorCode.SCOPE_CHANGED: "The Scope Changed",
    ErrorCode.SELF_APPROVAL_REFUSED: "The Credential That Ran This May Not Approve It",
    ErrorCode.DECISION_CHANGED: "The Decision Changed Since It Was Shown",
    ErrorCode.AGENT_TASK_WRITEBACK_NOT_FOUND: "Write-Back Not Found",
    ErrorCode.LINEAR_WRITE_DISABLED: "Linear Write-Back Is Disabled",
    ErrorCode.LINEAR_UNAVAILABLE: "Linear Is Unavailable",
    ErrorCode.TASK_ALREADY_CLAIMED: "Task Already Claimed",
    ErrorCode.TASK_NOT_CLAIMED: "Task Not Claimed By This Caller",
    ErrorCode.TASK_STATUS_CONFLICT: "Task Status Conflict",
    ErrorCode.TASK_DEADLINE_EXCEEDED: "Task Deadline Exceeded",
    ErrorCode.DISPATCH_PAUSED: "Dispatch Is Paused",
    ErrorCode.EXECUTORS_HALTED: "Executors Are Halted",
    ErrorCode.AGENT_CONCURRENCY_EXCEEDED: "Agent Is Already Running A Task",
    ErrorCode.AGENT_WORKFLOW_NOT_FOUND: "Workflow Not Found",
    ErrorCode.NO_AGENT_SELECTED: "No Agent Is Configured For This Step",
    ErrorCode.AGENT_NOT_IN_TEAM: "Agent Is Not In This Team",
    ErrorCode.ATTACHMENT_NOT_READY: "Attachment Is Not Ready",
    # Validation errors
    ErrorCode.VALIDATION_ERROR: "Validation Error",
    ErrorCode.INVALID_CONFIG: "Invalid Configuration",
    ErrorCode.MODEL_NOT_ALLOWED: "Model Not Allowed",
    ErrorCode.INVALID_SCHEMA: "Invalid Schema",
    ErrorCode.CORRUPTED_DATA: "Corrupted Data",
    ErrorCode.POLICY_CONTROL_INCOMPATIBLE: "Policy Control Incompatible",
    ErrorCode.CONTROL_BINDING_INCOMPATIBLE: "Control Binding Incompatible",
    ErrorCode.TEMPLATE_PARAMETER_INVALID: "Template Parameter Invalid",
    ErrorCode.TEMPLATE_RENDER_ERROR: "Template Render Error",
    ErrorCode.PLAN_STEP_OUT_OF_RANGE: "No Such Step In This Plan",
    ErrorCode.ATTACHMENT_REJECTED: "Attachment Type Not Accepted",
    ErrorCode.ATTACHMENT_TOO_LARGE: "Attachment Too Large",
    ErrorCode.ATTACHMENT_SOURCE_REFUSED: "Attachment Source Refused",
    # Capacity
    ErrorCode.QUOTA_EXCEEDED: "Quota Exceeded",
    ErrorCode.DISPATCH_BUDGET_EXCEEDED: "Dispatch Budget Exhausted",
    # Server errors
    ErrorCode.DATABASE_ERROR: "Database Error",
    ErrorCode.INTERNAL_ERROR: "Internal Server Error",
    ErrorCode.EVALUATION_FAILED: "Evaluation Failed",
    ErrorCode.EXECUTOR_UNAVAILABLE: "Executor Unavailable",
    ErrorCode.EXECUTOR_REJECTED: "Executor Rejected The Request",
}


def get_error_title(error_code: ErrorCode) -> str:
    """Get the standard title for an error code."""
    return ERROR_TITLES.get(error_code, "Error")
