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

export type PatchTeamRequest = {
  display_name?: string;
  description?: string | null;
  linear_team_key?: string | null;
};

export type PatchTeamResponse = {
  success: boolean;
  slug: string;
  display_name: string;
  description?: string | null;
  linear_team_key?: string | null;
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

export type StartTurnRequest = { message: string };

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
