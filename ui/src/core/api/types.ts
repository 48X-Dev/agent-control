/**
 * Re-export commonly used types from the generated API types
 * This makes it easier to import types without the verbose path
 */
import type { components, operations } from './generated/api-types';

// =============================================================================
// Error Types (RFC 7807 ProblemDetail)
// =============================================================================

/**
 * Validation error item (GitHub-style field-level error)
 */
export type ValidationErrorItem = {
  /** Resource type where error occurred (e.g., 'Control') */
  resource: string;
  /** Field path that caused the error (e.g., 'data.evaluator.config.pattern') */
  field: string | null;
  /** Machine-readable error code (e.g., 'required', 'invalid_format') */
  code: string;
  /** Human-readable error message */
  message: string;
  /** The invalid value (omitted for sensitive data) */
  value?: unknown;
};

/**
 * RFC 7807 Problem Detail error response
 */
export type ProblemDetail = {
  /** Error type URI */
  type: string;
  /** Short error title */
  title: string;
  /** HTTP status code */
  status: number;
  /** Human-readable error detail */
  detail: string;
  /** Request path */
  instance?: string;
  /** Machine-readable error code */
  error_code: string;
  /** Kubernetes-style reason */
  reason: string;
  /** Array of field-level validation errors */
  errors?: ValidationErrorItem[];
  /** Actionable hint for resolution */
  hint?: string;
};

// Agent types
export type Agent = components['schemas']['Agent'];
export type EvaluatorSchema = components['schemas']['EvaluatorSchema'];
export type StepSchema = components['schemas']['StepSchema'];
export type AgentSummary = components['schemas']['AgentSummary'];
export type ListAgentsResponse = components['schemas']['ListAgentsResponse'];

// Evaluator types
export type EvaluatorInfo = components['schemas']['EvaluatorInfo'];
export type EvaluatorsResponse = Record<string, EvaluatorInfo>;
// Request/Response types
export type InitAgentRequest = components['schemas']['InitAgentRequest'];
export type InitAgentResponse = components['schemas']['InitAgentResponse'];
export type GetAgentResponse = components['schemas']['GetAgentResponse'];
export type ControlActionDecision =
  components['schemas']['ControlAction']['decision'];
export type ControlExecution =
  components['schemas']['ControlDefinition-Input']['execution'];
export type ControlStage = NonNullable<
  components['schemas']['ControlScope']['stages']
>[number];
export type ControlScope = components['schemas']['ControlScope'];
export type ControlSelector = components['schemas']['ControlSelector'];
export type ControlAction = components['schemas']['ControlAction'];
export type ConditionNodeInput = components['schemas']['ConditionNode-Input'];
export type ConditionNodeOutput = components['schemas']['ConditionNode-Output'];
export type ConditionNode = ConditionNodeInput | ConditionNodeOutput;
export type ControlDefinitionInput =
  components['schemas']['ControlDefinition-Input'];
export type ControlDefinitionOutput =
  components['schemas']['ControlDefinition-Output'];
export type ControlDefinition =
  | ControlDefinitionInput
  | ControlDefinitionOutput;
export type Control = components['schemas']['Control'];
export type AgentControlsResponse =
  components['schemas']['AgentControlsResponse'];

// Control types
export type CreateControlRequest =
  components['schemas']['CreateControlRequest'];
export type CreateControlResponse =
  components['schemas']['CreateControlResponse'];
export type PatchControlRequest = components['schemas']['PatchControlRequest'];
export type PatchControlResponse =
  components['schemas']['PatchControlResponse'];
export type SetControlDataRequest =
  components['schemas']['SetControlDataRequest'];
export type SetControlDataResponse =
  components['schemas']['SetControlDataResponse'];
export type GetControlDataResponse =
  components['schemas']['GetControlDataResponse'];
export type GetControlSchemaResponse =
  components['schemas']['GetControlSchemaResponse'];

export type ValidateControlDataRequest =
  components['schemas']['ValidateControlDataRequest'];
export type ValidateControlDataResponse =
  components['schemas']['ValidateControlDataResponse'];
export type ControlSummary = components['schemas']['ControlSummary'];
export type ListControlsResponse =
  components['schemas']['ListControlsResponse'];

export type AgentRef = components['schemas']['AgentRef'];

export type PaginationInfo = components['schemas']['PaginationInfo'];

// =============================================================================
// Team Types (manual until API types are regenerated)
// =============================================================================

export type TeamSummary = {
  id: number;
  namespace_key: string;
  slug: string;
  display_name: string;
  description?: string | null;
  linear_team_key?: string | null;
  /**
   * Agent that runs a dispatched workflow step naming none, and therefore the
   * agent whose controls apply to that work. Null is a real state: the
   * dispatcher blocks the task rather than picking an agent itself.
   */
  default_agent_name?: string | null;
  /** Number of agents in the team. */
  member_count: number;
  created_at: string;
  updated_at: string;
};

export type TeamMemberRef = {
  agent_name: string;
  joined_at: string;
};

export type GetTeamResponse = TeamSummary & {
  /** Members ordered by agent name. */
  members: TeamMemberRef[];
};

export type ListTeamsResponse = {
  teams: TeamSummary[];
  pagination: PaginationInfo;
};

export type ListTeamsQueryParams = {
  cursor?: string;
  limit?: number;
};

/**
 * Omitted keys are left alone; an explicit null clears the field. Sending
 * `default_agent_name: null` is the only way back to no default agent.
 */
export type PatchTeamRequest = {
  display_name?: string;
  description?: string | null;
  linear_team_key?: string | null;
  default_agent_name?: string | null;
};

export type PatchTeamResponse = {
  success: boolean;
  slug: string;
  display_name: string;
  description?: string | null;
  linear_team_key?: string | null;
  default_agent_name?: string | null;
};

/** Agents filtered to one team. `team` is a slug, matched exactly. */
export type ListTeamAgentsQueryParams = {
  team: string;
  cursor?: string;
  limit?: number;
};

// =============================================================================
// Agent Session Types (manual until API types are regenerated)
//
// Mirrors `models/src/agent_control_models/sessions.py`. Two properties of the
// wire contract matter to anything rendering these:
//
// A message is a list of parts, not a string, because one model turn routinely
// mixes prose with a tool call. And every part is untrusted: the text came out
// of a model, and a tool result can carry whatever page the agent fetched.
// Rendering is plain text only, which the chat panel enforces.
// =============================================================================

export type AgentSessionStatus =
  | 'active'
  | 'archived'
  | 'orphaned'
  | 'orphaned_pending_delete';

export type SessionMessageRole = 'user' | 'agent' | 'system';

export type SessionMessagePartKind =
  | 'text'
  | 'tool_call'
  | 'tool_result'
  | 'unsupported';

export type ExecutorKind = 'google_adk';

export type SessionMessagePart = {
  kind: SessionMessagePartKind;
  /** Verbatim text, for text parts. Never markup, never trusted. */
  text?: string | null;
  tool_name?: string | null;
  tool_call_id?: string | null;
  arguments?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
};

export type SessionMessage = {
  /** 0-based position in the transcript as the server read it. */
  index: number;
  role: SessionMessageRole;
  author?: string | null;
  timestamp?: string | null;
  parts?: SessionMessagePart[];
};

export type AgentSessionSummary = {
  session_key: string;
  namespace_key: string;
  agent_name: string;
  team_slug?: string | null;
  title?: string | null;
  status: AgentSessionStatus;
  executor_kind: ExecutorKind;
  last_trace_id?: string | null;
  last_activity_at: string;
  created_at: string;
  updated_at: string;
};

/**
 * One session, with its live turn state.
 *
 * The two in-flight fields are not synonyms. `in_flight_since` is the lock: a
 * second turn is refused while it is set, and it clears whenever the server
 * stops waiting. `in_flight_trace_id` is the liveness marker: it clears only
 * when a turn genuinely ended, so it outlives the lock after a timeout or a
 * client that hung up. Lock clear plus marker set means "you may send again,
 * and the previous turn has not finished".
 */
export type AgentSessionDetail = AgentSessionSummary & {
  in_flight_since?: string | null;
  in_flight_trace_id?: string | null;
};

export type ListAgentSessionsResponse = {
  sessions?: AgentSessionSummary[];
  pagination: PaginationInfo;
};

export type ListAgentSessionsQueryParams = {
  agent?: string;
  team?: string;
  status?: AgentSessionStatus;
  cursor?: string;
  limit?: number;
};

export type GetAgentSessionResponse = { session: AgentSessionDetail };

export type CreateAgentSessionRequest = {
  agent_name: string;
  title?: string | null;
  team_slug?: string | null;
};

export type CreateAgentSessionResponse = { session: AgentSessionDetail };

export type ListSessionMessagesQueryParams = {
  after_index?: number;
  limit?: number;
};

export type ListSessionMessagesResponse = {
  session_key: string;
  status: AgentSessionStatus;
  messages?: SessionMessage[];
  /** Pass back as `after_index` to read the next page; null on the last page. */
  next_index?: number | null;
  has_more: boolean;
  total: number;
  /**
   * Set when the transcript could not be read as-is, e.g. the executor no
   * longer holds this session. A banner above an empty transcript, not an
   * error.
   */
  notice?: string | null;
};

export type StartTurnRequest = {
  message: string;
  /**
   * Files already stored on this session to carry with this turn.
   *
   * Keys, never bytes. The server resolves them against rows this caller had to
   * be authorized to create, and a key naming anything else is a 404. A key
   * whose attachment is not `ready` refuses the whole turn rather than sending
   * a message the operator thinks carried a file.
   */
  attachment_keys?: string[];
};

// =============================================================================
// Attachments (manual until API types are regenerated)
//
// Nothing in this block is markup and nothing may be rendered as markup.
// `display_name` is chosen by whoever uploaded the file - or, on the Linear
// path, by whoever filed the issue - and the console session cookie is a
// credential on every admin endpoint. It is text in a text node, always.
// =============================================================================

export type AttachmentStatus =
  | 'pending'
  | 'converting'
  | 'ready'
  | 'rejected'
  | 'failed'
  | 'tombstoned';

export type AttachmentOrigin = 'operator_upload' | 'linear';

export type Attachment = {
  attachment_key: string;
  session_key: string;
  /** Server-normalized. Untrusted text; render it, never interpret it. */
  display_name: string;
  /** True when normalization changed the name it was given. */
  display_name_normalized: boolean;
  declared_mime: string;
  /** What the magic bytes say. The declared type decides nothing. */
  sniffed_mime: string;
  mime_mismatch: boolean;
  size_bytes: number;
  source_sha256: string;
  delivered_sha256?: string | null;
  delivered_mime?: string | null;
  delivered_size_bytes?: number | null;
  status: AttachmentStatus;
  failure_code?: string | null;
  page_count?: number | null;
  estimated_tokens?: number | null;
  converted_from?: string | null;
  origin: AttachmentOrigin;
  origin_ref?: string | null;
  created_at: string;
  updated_at: string;
};

export type CreateAttachmentResponse = {
  attachment: Attachment;
  /** The same bytes were already on this session. Not a conflict. */
  deduplicated: boolean;
};

export type ListAttachmentsResponse = {
  attachments?: Attachment[];
  count: number;
  total_bytes: number;
};

export type DeleteAttachmentResponse = {
  deleted: boolean;
  notice: string;
};

/**
 * One completed turn.
 *
 * `messages` is turn-relative: index 0 is the first message of this turn, not
 * of the conversation. The transcript endpoint is the authoritative record and
 * the only place indexes are absolute.
 */
export type TurnResponse = {
  session_key: string;
  trace_id: string;
  started_at: string;
  completed_at: string;
  duration_seconds: number;
  messages?: SessionMessage[];
};

// =============================================================================
// Nudges and halts (manual until API types are regenerated)
//
// The two things a human can do to a running agent. A nudge is guidance that
// arrives at the agent's next model call and the agent carries on. A halt is a
// stop that lands at the next model or tool boundary and ends the turn.
//
// Neither is immediate, and no copy built on these types may imply otherwise:
// a tool that has already started runs to completion and its side effect
// happens. That is a property of the executor, not of this panel.
// =============================================================================

export type NudgeStatus =
  | 'pending'
  | 'claimed'
  | 'applied'
  | 'expired'
  | 'cancelled'
  | 'rejected';

export type Nudge = {
  id: number;
  session_key: string;
  /**
   * The operator's exact text, and for an applied nudge the exact text the
   * model was shown. Rendered inline in the transcript so a person can judge
   * for themselves whether the agent was told what they think they said.
   * Plain text only, like everything else in this panel.
   */
  body: string;
  status: NudgeStatus;
  created_at: string;
  claimed_at?: string | null;
  claim_expires_at?: string | null;
  applied_at?: string | null;
  /** Trace of the turn it landed in. Transcript markers align on this. */
  applied_trace_id?: string | null;
  claim_count: number;
  injection_attempts: number;
  /** Control that denied it, when the status is `rejected`. */
  rejected_by_control?: string | null;
};

export type CreateNudgeRequest = { body: string };
export type CreateNudgeResponse = { nudge: Nudge };
export type ListNudgesResponse = { session_key: string; nudges?: Nudge[] };
export type CancelNudgeResponse = { cancelled: boolean; nudge: Nudge };

export type HaltMode = 'graceful' | 'restart';
export type HaltStatus = 'pending' | 'applied' | 'expired';
export type HaltBoundary = 'model' | 'tool';

/**
 * One operator stop, bound to one turn.
 *
 * `status: 'applied'` is the executor saying it blocked, which is an assertion
 * by the party being stopped. The state that may be rendered as *stopped* is
 * `turn_ended_at`, which the server observes for itself.
 */
export type Halt = {
  id: number;
  session_key: string;
  target_trace_id: string;
  mode: HaltMode;
  status: HaltStatus;
  created_at: string;
  applied_at?: string | null;
  applied_at_boundary?: HaltBoundary | 'process' | null;
  /** Tool the agent was about to run. Executor-supplied; plain text only. */
  applied_tool_name?: string | null;
  turn_ended_at?: string | null;
};

export type CreateHaltResponse = { halt: Halt; created: boolean };
export type ListHaltsResponse = { session_key: string; halts?: Halt[] };

// =============================================================================
// Declared plans (manual until API types are regenerated)
//
// What the agent says it is doing. Every field here is a claim by the agent,
// not an observation of it, which is why the rail built on these types is
// labelled "Plan reported by the agent" and carries a trace link beside it.
//
// There is no percentage in this shape and none may be derived from it. Steps
// done over steps declared is a completion figure nobody measured, and the
// moment it is rendered, an agent's account of its own work has been laundered
// into a fact.
// =============================================================================

export type PlanStepStatus =
  | 'pending'
  | 'active'
  | 'done'
  | 'skipped'
  | 'failed';

export type PlanStep = {
  /** 0-based position in the plan, in the order the agent declared it. */
  index: number;
  /** The step as the agent worded it. Plain text only, like every body here. */
  title: string;
  status: PlanStepStatus;
  note?: string | null;
  /** Last write to this step. The basis for showing staleness, not progress. */
  updated_at: string;
};

export type Plan = {
  session_key: string;
  /** Revision these steps belong to. An agent that replans writes a new one. */
  revision: number;
  /** How many plans this agent has declared here. More than one means revised. */
  revision_count: number;
  steps?: PlanStep[];
  declared_at: string;
  last_updated_at: string;
};

/** `plan` is null when the agent never declared one, which is the common case. */
export type PlanResponse = { session_key: string; plan?: Plan | null };

// =============================================================================
// Linear Milestone Types (manual until API types are regenerated)
// =============================================================================

/**
 * Why a milestone response looks the way it does.
 *
 * Only `error` is a failure, and the server still delivers it with a 200 so an
 * unreachable Linear cannot make an Agent Control page look broken.
 */
export type MilestonesStatus =
  | 'not_configured'
  | 'not_linked'
  | 'error'
  | 'empty'
  | 'ok';

export type Milestone = {
  id: string;
  name: string;
  description?: string | null;
  /** ISO date (YYYY-MM-DD), or null when the milestone has no due date. */
  target_date?: string | null;
  /** Linear's own status text, passed through rather than enumerated. */
  status?: string | null;
  /** Completion from 0 to 1, when Linear reports it. */
  progress?: number | null;
  project_id?: string | null;
  project_name?: string | null;
  project_url?: string | null;
};

export type ListTeamMilestonesResponse = {
  status: MilestonesStatus;
  slug: string;
  linear_team_key?: string | null;
  milestones: Milestone[];
  /** Short, client-safe reason, set only when status is 'error'. */
  error?: string | null;
  retry_after_seconds?: number | null;
  cached: boolean;
  fetched_at?: string | null;
};

// =============================================================================
// Agent configuration (manual until API types are regenerated)
//
// One row per agent carrying two fields: the system prompt and the model. They
// share a version counter and an optimistic-concurrency token, which is why a
// prompt edit and a model edit conflict with each other and why one Save
// covers both.
//
// Every body on this surface is operator-authored text that renders in an
// admin console whose session cookie is a valid credential on this API. It is
// rendered as text nodes, never as markup, everywhere it appears.
// =============================================================================

export type BodyFormat = 'text';

export type ConfigEventType =
  | 'created'
  | 'updated'
  | 'prompt_cleared'
  | 'model_cleared'
  | 'restored'
  | 'enabled'
  | 'disabled';

export type ConfigOrigin = 'authored' | 'copied_from_reported' | 'restored';

export type ModelProvider = 'gemini' | 'openai_compatible';

export type ModelCostTier = 'economy' | 'standard' | 'premium';

/** Which layer supplies the prompt the agent actually runs. */
export type PromptSource = 'managed' | 'code' | 'none';

/** Which layer supplies the model the agent actually calls. */
export type ModelSource = 'managed' | 'code';

/**
 * Whether this agent's configuration reaches a running process at all.
 *
 * `blocked_insecure_auth` is the server's startup gate: with credential
 * enforcement off, every operation succeeds unauthenticated including ADMIN,
 * so nothing here is applied to a running agent. Storage, versioning and the
 * audit trail keep working regardless.
 */
export type DeliveryState = 'active' | 'disabled' | 'blocked_insecure_auth';

/**
 * One advisory observation recorded when a body was saved.
 *
 * Never blocks a write. It carries no matched text on purpose: a finding on a
 * secret-shaped string would otherwise copy the secret into the version row
 * and into this response.
 */
export type ScanFinding = {
  scanner: string;
  severity: 'info' | 'warning';
  code: string;
  message: string;
  match_count: number;
};

/** One entry in the server's model allowlist. */
export type AgentModelOption = {
  id: string;
  label: string;
  provider: ModelProvider;
  cost_tier: ModelCostTier;
  /** Flagged as the operator's suggestion. Never applied as a default. */
  recommended: boolean;
};

export type ListAgentModelsResponse = { models?: AgentModelOption[] };

export type GetAgentConfigResponse = {
  agent_name: string;
  /** Null when unmanaged or cleared. */
  body?: string | null;
  body_format: BodyFormat;
  prompt_enabled: boolean;
  prompt_source: PromptSource;
  model_id?: string | null;
  /**
   * Resolved from the allowlist on every read; null once the stored id stops
   * being offered. The SDK refuses to construct anything without it rather
   * than inferring a provider from the id string.
   */
  model_provider?: ModelProvider | null;
  /** False when the stored id has left the server allowlist. */
  model_allowed: boolean;
  model_cost_tier?: ModelCostTier | null;
  model_source: ModelSource;
  delivery_state: DeliveryState;
  /** Opaque, server-issued, covering both fields. */
  etag?: string | null;
  current_version: number;
  /** Reported by the agent process. Unverified. Never sent to a model. */
  source_instruction?: string | null;
  source_reported_at?: string | null;
  updated_by_hash?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type SetAgentConfigRequest = {
  body?: string | null;
  model_id?: string | null;
  /** The `current_version` the editor loaded. A mismatch is a 409. */
  expected_version: number;
  origin?: 'authored' | 'copied_from_reported';
  note?: string | null;
  prompt_enabled?: boolean;
};

export type SetAgentConfigResponse = {
  success: boolean;
  version_num: number;
  current_version: number;
  etag?: string | null;
  prompt_source: PromptSource;
  model_source: ModelSource;
  delivery_state: DeliveryState;
  scan_findings?: ScanFinding[];
};

export type ClearAgentConfigFieldRequest = {
  expected_version: number;
  note?: string | null;
};

export type ClearAgentConfigFieldResponse = {
  /** False when the field was already null. Nothing was written. */
  cleared: boolean;
  version_num?: number | null;
  current_version: number;
  etag?: string | null;
  prompt_source: PromptSource;
  model_source: ModelSource;
  delivery_state: DeliveryState;
};

export type SetPromptEnabledRequest = {
  prompt_enabled: boolean;
  expected_version: number;
  note?: string | null;
};

export type RestoreAgentConfigVersionRequest = {
  expected_version: number;
  note?: string | null;
};

export type AgentConfigVersionSummary = {
  version_num: number;
  event_type: ConfigEventType;
  origin: ConfigOrigin;
  model_id?: string | null;
  note?: string | null;
  has_body: boolean;
  scan_findings?: ScanFinding[];
  /**
   * Identifies a credential, not a person. Under the shipped default provider
   * every dashboard caller hashes to the same value, so the history column is
   * labelled "credential".
   */
  changed_by_hash?: string | null;
  created_at: string;
};

export type AgentConfigVersionDetail = AgentConfigVersionSummary & {
  body?: string | null;
  body_format: BodyFormat;
  etag?: string | null;
};

export type ListAgentConfigVersionsResponse = {
  versions?: AgentConfigVersionSummary[];
  pagination: PaginationInfo;
};

export type GetAgentConfigVersionResponse = {
  version: AgentConfigVersionDetail;
};

export type ListAgentConfigVersionsQueryParams = {
  cursor?: number;
  limit?: number;
};

// =============================================================================
// Template Types (manual until API types are regenerated)
// =============================================================================

export type TemplateValue = string | boolean | string[];

export type TemplateParameterBase = {
  label: string;
  description?: string | null;
  required?: boolean;
  ui_hint?: string | null;
};

export type StringTemplateParameter = TemplateParameterBase & {
  type: 'string';
  default?: string | null;
  placeholder?: string | null;
};

export type StringListTemplateParameter = TemplateParameterBase & {
  type: 'string_list';
  default?: string[] | null;
  placeholder?: string[] | null;
};

export type EnumTemplateParameter = TemplateParameterBase & {
  type: 'enum';
  allowed_values: string[];
  default?: string | null;
};

export type BooleanTemplateParameter = TemplateParameterBase & {
  type: 'boolean';
  default?: boolean | null;
};

export type RegexTemplateParameter = TemplateParameterBase & {
  type: 'regex_re2';
  default?: string | null;
  placeholder?: string | null;
};

export type TemplateParameterDefinition =
  | StringTemplateParameter
  | StringListTemplateParameter
  | EnumTemplateParameter
  | BooleanTemplateParameter
  | RegexTemplateParameter;

export type TemplateDefinition = {
  description?: string | null;
  parameters: Record<string, TemplateParameterDefinition>;
  definition_template: unknown;
};

export type TemplateControlInput = {
  template: TemplateDefinition;
  template_values: Record<string, TemplateValue>;
};

export type RenderControlTemplateRequest = {
  template: TemplateDefinition;
  template_values: Record<string, TemplateValue>;
};

export type RenderControlTemplateResponse = {
  control: ControlDefinition;
};

// Helper type to extract query parameters from operations
type ExtractQueryParams<T> = T extends { parameters: { query?: infer Q } }
  ? Q
  : never;

// Helper type to extract path parameters from operations
type ExtractPathParams<T> = T extends { parameters: { path?: infer P } }
  ? P
  : never;

// Helper type to extract request body from operations
type ExtractRequestBody<T> = T extends {
  requestBody?: { content: { 'application/json': infer B } };
}
  ? B
  : never;

// Specific parameter types using operations
export type ListAgentsQueryParams = ExtractQueryParams<
  operations['list_agents_api_v1_agents_get']
>;
export type GetAgentPathParams = ExtractPathParams<
  operations['get_agent_api_v1_agents__agent_name__get']
>;
export type GetAgentControlsPathParams = ExtractPathParams<
  operations['list_agent_controls_api_v1_agents__agent_name__controls_get']
>;

// Request body types
export type InitAgentRequestBody = ExtractRequestBody<
  operations['init_agent_api_v1_agents_initAgent_post']
>;

// =============================================================================
// Dispatch: milestone issues, the task ledger, workflows, fleet state
// (manual until API types are regenerated)
//
// Everything on this surface that came from a tracker is untrusted input
// written by whoever can file an issue: titles, bodies, and every word of
// agent output derived from them. It renders as text nodes, never as markup,
// everywhere it appears.
// =============================================================================

/** One issue in a milestone, as the server read it from Linear. */
export type MilestoneIssue = {
  /** Linear's internal id. This is what the ledger deduplicates on. */
  ref: string;
  /** The human-facing key, e.g. `OPS-114`. */
  identifier: string;
  title: string;
  description?: string | null;
  url?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  creator_id?: string | null;
  creator_display_name?: string | null;
};

/**
 * Why an issue in this milestone was left out.
 *
 * Counted in Python rather than filtered away in the query, so the confirm can
 * say "2 issues are assigned to a person and were skipped" instead of showing
 * a shorter list with no explanation.
 */
export type MilestoneIssueSkipCounts = {
  started: number;
  assigned: number;
  other_team: number;
};

export type MilestoneIssueCounts = {
  fetched: number;
  eligible: number;
  skipped: MilestoneIssueSkipCounts;
  /** True when the read came back at its hard cap, so more may exist. */
  beyond_page_cap: boolean;
};

export type ListMilestoneIssuesResponse = {
  status: MilestonesStatus;
  slug: string;
  linear_team_key: string;
  milestone_id: string;
  issues: MilestoneIssue[];
  counts: MilestoneIssueCounts;
  error?: string | null;
  retry_after_seconds?: number | null;
  cached: boolean;
  fetched_at?: string | null;
};

export type TaskSourceKind = 'linear' | 'file';

export type AgentTaskStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'paused_quota'
  | 'running_unknown'
  | 'awaiting_approval'
  | 'cancelled';

export type AgentTaskStepStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'abandoned';

export type AgentTaskSummary = {
  task_key: string;
  source_kind: TaskSourceKind;
  source_ref: string;
  source_url?: string | null;
  source_scope_kind?: 'milestone' | 'team_label' | null;
  source_scope_ref?: string | null;
  source_scope_name?: string | null;
  source_team_key?: string | null;
  /** Untrusted. Written by whoever filed the item. */
  title: string;
  team_slug?: string | null;
  workflow_key: string;
  status: AgentTaskStatus;
  dry_run: boolean;
  current_step: number;
  turns_used: number;
  claimed_by?: string | null;
  claimed_at?: string | null;
  heartbeat_at?: string | null;
  lease_expires_at?: string | null;
  deadline_at: string;
  chain_trace_id?: string | null;
  failure_code?: string | null;
  failure_detail?: string | null;
  created_at: string;
  updated_at: string;
};

export type ListAgentTasksResponse = {
  tasks: AgentTaskSummary[];
  pagination: {
    limit: number;
    total: number;
    next_cursor?: string | null;
    has_more: boolean;
  };
};

/**
 * One position in a chain, planned or run.
 *
 * `ran: false` is the whole reason this is a chain rather than a list of rows:
 * a two-step workflow that stopped after its first agent has one step row, and
 * a view built from rows alone would render that as a finished one-agent task.
 */
export type AgentTaskChainHop = {
  step_index: number;
  agent_name?: string | null;
  brief: string;
  ran: boolean;
  status?: AgentTaskStepStatus | null;
  session_key?: string | null;
  turn_trace_id?: string | null;
  /** The agent's own words. Untrusted, and rendered as text. */
  output_text?: string | null;
  output_truncated: boolean;
  attempts: number;
  failure_code?: string | null;
  failure_detail?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
};

export type AgentTaskChain = {
  task_key: string;
  source_kind: string;
  source_ref: string;
  source_url?: string | null;
  title: string;
  team_slug?: string | null;
  workflow_key: string;
  workflow_display_name: string;
  status: AgentTaskStatus;
  dry_run: boolean;
  chain_trace_id?: string | null;
  hops: AgentTaskChainHop[];
  hops_planned: number;
  hops_ran: number;
  failure_code?: string | null;
  failure_detail?: string | null;
};

export type GetAgentTaskChainResponse = { chain: AgentTaskChain };

/** One candidate row the operator is being asked to agree to. */
export type ImportCandidate = {
  source_ref: string;
  title: string;
  source_url?: string | null;
  flags: string[];
};

export type ImportSkipCounts = {
  already_queued: number;
  already_worked: number;
  other_team: number;
  assigned: number;
  in_progress: number;
  label_filtered: number;
  beyond_page_cap: number;
};

export type DispatchBudget = {
  max_turns_per_hour: number;
  turns_used_this_hour: number;
  turns_remaining_this_hour: number;
  max_tasks_per_hour: number;
  tasks_created_this_hour: number;
  tasks_remaining_this_hour: number;
  window_started_at: string;
  window_resets_at: string;
};

/**
 * The two stop switches and the hourly budget.
 *
 * Advisory wherever it is rendered: enforcement lives on the turn path inside
 * the server, and a screen that reports a budget is not the thing that spends
 * it. The `*_by_hash` fields identify a credential and not a person, because
 * every browser caller hashes identically today.
 */
export type DispatchStateSnapshot = {
  paused: boolean;
  paused_at?: string | null;
  paused_by_hash?: string | null;
  paused_reason?: string | null;
  executors_halted: boolean;
  executors_halted_at?: string | null;
  executors_halted_by_hash?: string | null;
  executors_halted_reason?: string | null;
  budget: DispatchBudget;
  updated_at: string;
};

export type GetDispatchStateResponse = { state: DispatchStateSnapshot };

export type ImportTaskItem = {
  source_ref: string;
  title: string;
  body?: string;
  source_url?: string | null;
};

export type ImportAgentTasksRequest = {
  scope: {
    kind: 'items';
    source_kind: TaskSourceKind;
    items: ImportTaskItem[];
  };
  team_slug?: string | null;
  workflow_key?: string | null;
  dry_run: boolean;
  requeue_completed?: boolean;
  mode: 'preview' | 'commit';
  /** Required on commit: sha256 over the sorted refs that were displayed. */
  expected_refs_digest?: string | null;
};

export type ImportAgentTasksResponse = {
  mode: 'preview' | 'commit';
  eligible: ImportCandidate[];
  refs_digest: string;
  skipped: ImportSkipCounts;
  workflow_key: string;
  dry_run: boolean;
  created: number;
  task_keys: string[];
  dispatch_state?: DispatchStateSnapshot | null;
};

export type AgentWorkflowStep = {
  agent_name?: string | null;
  brief: string;
  max_turns: number;
  required_output: 'text' | 'none';
  idempotent: boolean;
};

export type AgentWorkflow = {
  workflow_key: string;
  display_name: string;
  team_slug?: string | null;
  steps: AgentWorkflowStep[];
  created_at: string;
  updated_at: string;
};

export type ListAgentWorkflowsResponse = { workflows: AgentWorkflow[] };

// =============================================================================
// Write-back and the review queue (manual until API types are regenerated)
//
// Mirrors agent_control_models.tasks. The summary text and the write-back body
// are agent output and therefore untrusted: they render as text nodes only,
// like everything else on this surface.
// =============================================================================

export type WritebackKind = 'comment' | 'status_change';

export type WritebackStatus =
  | 'pending'
  | 'sent'
  | 'denied'
  | 'failed'
  | 'awaiting_approval'
  | 'rejected';

export type AgentTaskWriteback = {
  writeback_id: number;
  task_key: string;
  kind: WritebackKind;
  status: WritebackStatus;
  step_index: number;
  /** Untrusted agent output, sanitized server-side. Still rendered as text. */
  body: string;
  target_state_id?: string | null;
  decision_digest?: string | null;
  /**
   * A credential, not a person: every browser caller hashes to the same
   * value, so this must never be rendered as somebody's name.
   */
  approved_by_hash?: string | null;
  approved_at?: string | null;
  rejected_reason?: string | null;
  attempts: number;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
};

/** The target issue, read live from Linear when the review card was built. */
export type ReviewQueueIssue = {
  source_ref: string;
  identifier?: string | null;
  title?: string | null;
  state_name?: string | null;
  state_type?: string | null;
  team_key?: string | null;
  milestone_id?: string | null;
  /** True when Linear could not be read; such a card cannot be accepted. */
  read_failed: boolean;
};

/** One completed task's proposal to close its issue, waiting on a person. */
export type ReviewQueueEntry = {
  task_key: string;
  writeback_id: number;
  agent_name?: string | null;
  /** The task's final output. Untrusted text. */
  summary: string;
  source_ref: string;
  source_url?: string | null;
  team_slug?: string | null;
  source_scope_name?: string | null;
  chain_trace_id?: string | null;
  created_at: string;
  stale: boolean;
  /** Echo this back on accept. Null when Linear could not be read. */
  decision_digest?: string | null;
  issue?: ReviewQueueIssue | null;
};

export type ListReviewQueueResponse = {
  entries: ReviewQueueEntry[];
  /** Waiting entries in scope, beyond this page too. */
  total: number;
};

export type AcceptAgentTaskRequest = {
  writeback_id: number;
  /** The digest the review card showed, over (text, target, state) together. */
  expected_decision_digest: string;
};

/**
 * The server answers with the full task detail; this console reads only the
 * summary fields plus the row it decided, so that is all these types name.
 */
export type AcceptAgentTaskResponse = {
  task: AgentTaskSummary;
  writeback: AgentTaskWriteback;
  /** 'ALREADY_COMPLETED' when a person closed the issue first. Not an error. */
  note?: string | null;
  /** The milestone's progress after the close, rendered optimistically. */
  milestone_progress?: number | null;
};

export type RejectAgentTaskRequest = {
  writeback_id: number;
  reason?: string | null;
};

export type RejectAgentTaskResponse = {
  task: AgentTaskSummary;
  writeback: AgentTaskWriteback;
};
