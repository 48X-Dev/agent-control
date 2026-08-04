import { type Page, test as base } from '@playwright/test';

import type {
  AgentConfigVersionSummary,
  AgentControlsResponse,
  AgentModelOption,
  AgentSessionDetail,
  AgentSessionSummary,
  AgentSummary,
  Attachment,
  ClearAgentConfigFieldRequest,
  Control,
  ControlSummary,
  EvaluatorsResponse,
  GetAgentConfigResponse,
  GetAgentResponse,
  GetControlSchemaResponse,
  GetTeamResponse,
  Halt,
  ListAgentsResponse,
  ListControlsResponse,
  ListTeamMilestonesResponse,
  ListTeamsResponse,
  Milestone,
  Nudge,
  Plan,
  RestoreAgentConfigVersionRequest,
  ScanFinding,
  SessionMessage,
  SessionMessagePart,
  SetAgentConfigRequest,
  SetPromptEnabledRequest,
  TeamSummary,
} from '@/core/api/types';
import type { StatsResponse } from '@/core/hooks/query-hooks/use-agent-monitor';

/**
 * Mock data for API responses
 * Uses API types to ensure type safety - if backend changes, TypeScript will catch it
 */

// Satisfies ensures type checking while allowing inference of literal types
const agentsList: AgentSummary[] = [
  {
    agent_name: 'customer-support-bot',
    policy_ids: [1],
    created_at: '2024-01-01T00:00:00Z',
    step_count: 5,
    evaluator_count: 2,
    active_controls_count: 3,
  },
  {
    agent_name: 'data-analysis-agent',
    policy_ids: [2],
    created_at: '2024-01-02T00:00:00Z',
    step_count: 3,
    evaluator_count: 1,
    active_controls_count: 2,
  },
  {
    agent_name: 'code-review-assistant',
    policy_ids: [3],
    created_at: '2024-01-03T00:00:00Z',
    step_count: 8,
    evaluator_count: 4,
    active_controls_count: 5,
  },
];

const agentsResponse: ListAgentsResponse = {
  agents: agentsList,
  pagination: {
    total: 3,
    limit: 25,
    has_more: false,
    next_cursor: null,
  },
};

const agentResponse: GetAgentResponse = {
  agent: {
    agent_name: 'customer-support-bot',
    agent_description: 'Handles customer inquiries and support tickets',
    agent_created_at: '2024-01-01T00:00:00Z',
    agent_updated_at: '2024-01-15T00:00:00Z',
    agent_version: '1.0.0',
    agent_metadata: null,
  },
  steps: [],
  evaluators: [],
};

/** Agent with populated steps for step dropdown tests */
const agentWithStepsResponse: GetAgentResponse = {
  ...agentResponse,
  steps: [
    {
      type: 'tool',
      name: 'search_db',
      input_schema: {
        query: { type: 'string' },
      },
      output_schema: {
        results: {
          type: 'array',
          items: { type: 'object' },
        },
      },
    },
    {
      type: 'tool',
      name: 'fetch_user',
      input_schema: {
        user_id: { type: 'string' },
      },
      output_schema: {
        user: {
          type: 'object',
          properties: {
            id: { type: 'string' },
            email: { type: 'string' },
          },
        },
      },
    },
    {
      type: 'tool',
      name: 'database_query',
      input_schema: {
        query: { type: 'string' },
        limit: { type: 'integer' },
      },
      output_schema: {
        rows: {
          type: 'array',
          items: { type: 'object' },
        },
      },
    },
    {
      type: 'llm',
      name: 'support-answer',
      input_schema: {
        messages: {
          type: 'array',
          items: { type: 'object' },
        },
      },
      output_schema: {
        text: { type: 'string' },
      },
    },
  ],
};

const templateBackedControl: Control = {
  id: 10,
  name: 'Template Regex Guard',
  control: {
    description: 'Deny when input matches pattern',
    enabled: true,
    execution: 'server',
    scope: { step_names: ['chat-completion'], stages: ['pre'] },
    condition: {
      selector: { path: 'input' },
      evaluator: {
        name: 'regex',
        config: { pattern: '\\b(SSN|social.security)\\b' },
      },
    },
    action: { decision: 'deny' },
    tags: [],
    template: {
      description: 'Regex denial template',
      parameters: {
        pattern: {
          type: 'regex_re2',
          label: 'Regex Pattern',
          description: 'RE2 pattern to match against input',
        },
        step_name: {
          type: 'string',
          label: 'Step Name',
          required: false,
          default: 'chat-completion',
        },
      },
      definition_template: {
        description: 'Deny when input matches pattern',
        execution: 'server',
        scope: {
          step_names: [{ $param: 'step_name' }],
          stages: ['pre'],
        },
        condition: {
          selector: { path: 'input' },
          evaluator: {
            name: 'regex',
            config: { pattern: { $param: 'pattern' } },
          },
        },
        action: { decision: 'deny' },
      },
    },
    template_values: {
      pattern: '\\b(SSN|social.security)\\b',
      step_name: 'chat-completion',
    },
  } as Control['control'],
};

const unrenderedTemplateControl = {
  id: 11,
  name: 'Unrendered Template Guard',
  control: {
    enabled: false,
    template: {
      description: 'Template without supplied values',
      definition_template: {
        description: 'Deny when input matches pattern',
        execution: 'server',
        scope: {
          stages: ['pre'],
        },
        condition: {
          selector: { path: 'input' },
          evaluator: {
            name: 'regex',
            config: { pattern: '\\bsecret\\b' },
          },
        },
        action: { decision: 'deny' },
      },
    },
  },
} as unknown as Control;

const controlsList: Control[] = [
  {
    id: 1,
    name: 'PII Detection',
    control: {
      description: 'Detects and masks personally identifiable information',
      enabled: true,
      execution: 'server',
      scope: { step_types: ['llm'], stages: ['post'] },
      condition: {
        selector: { path: 'output' },
        evaluator: {
          name: 'regex',
          config: { pattern: '\\b\\d{3}-\\d{2}-\\d{4}\\b' },
        },
      },
      action: { decision: 'deny' },
      tags: ['pii', 'compliance'],
    },
  },
  {
    id: 2,
    name: 'SQL Injection Guard',
    control: {
      description: 'Prevents SQL injection attacks',
      enabled: true,
      execution: 'server',
      scope: {
        step_types: ['tool'],
        step_names: ['database_query'],
        step_name_regex: '^db_.*',
        stages: ['pre'],
      },
      condition: {
        selector: { path: 'input.query' },
        evaluator: {
          name: 'sql',
          config: { mode: 'safe' },
        },
      },
      action: { decision: 'deny' },
      tags: ['security'],
    },
  },
  {
    id: 3,
    name: 'Rate Limiter',
    control: {
      description: 'Limits API call frequency',
      enabled: false,
      execution: 'server',
      scope: { step_types: ['llm'], stages: ['pre'] },
      condition: {
        selector: { path: '*' },
        evaluator: {
          name: 'list',
          config: { values: [], logic: 'any', match_on: 'match' },
        },
      },
      action: { decision: 'observe' },
      tags: [],
    },
  },
];

const controlsWithTemplateList: Control[] = [
  ...controlsList,
  templateBackedControl,
];

const controlsWithUnrenderedTemplateList: Control[] = [
  ...controlsList,
  unrenderedTemplateControl,
];

const controlsResponse: AgentControlsResponse = {
  controls: controlsList,
};

const controlsWithTemplateResponse: AgentControlsResponse = {
  controls: controlsWithTemplateList,
};

const controlsWithUnrenderedTemplateResponse: AgentControlsResponse = {
  controls: controlsWithUnrenderedTemplateList,
};

// Control summaries for GET /api/v1/controls (list all controls)
const controlSummariesList: (ControlSummary & {
  used_by_agent?: { agent_name: string } | null;
})[] = [
  {
    id: 1,
    name: 'PII Detection',
    description: 'Detects and masks personally identifiable information',
    enabled: true,
    execution: 'server',
    step_types: ['llm'],
    stages: ['post'],
    tags: ['pii', 'compliance'],
    used_by_agent: { agent_name: 'customer-support-bot' },
    used_by_agents_count: 1,
  },
  {
    id: 2,
    name: 'SQL Injection Guard',
    description: 'Prevents SQL injection attacks',
    enabled: true,
    execution: 'server',
    step_types: ['tool'],
    stages: ['pre'],
    tags: ['security'],
    used_by_agent: { agent_name: 'data-analysis-agent' },
    used_by_agents_count: 1,
  },
  {
    id: 3,
    name: 'Rate Limiter',
    description: 'Limits API call frequency',
    enabled: false,
    execution: 'server',
    step_types: ['llm'],
    stages: ['pre'],
    tags: [],
    used_by_agent: null,
    used_by_agents_count: 0,
  },
];

const templateControlSummary: ControlSummary & {
  used_by_agent?: { agent_name: string } | null;
  template_backed?: boolean;
} = {
  id: 10,
  name: 'Template Regex Guard',
  description: 'Deny when input matches pattern',
  enabled: true,
  execution: 'server',
  step_types: null,
  stages: ['pre'],
  tags: [],
  template_backed: true,
  used_by_agent: { agent_name: 'customer-support-bot' },
  used_by_agents_count: 1,
};

const listControlsResponse: ListControlsResponse = {
  controls: controlSummariesList,
  pagination: {
    total: controlSummariesList.length,
    limit: 20,
    has_more: false,
    next_cursor: null,
  },
};

const evaluatorsResponse: EvaluatorsResponse = {
  regex: {
    name: 'Regex',
    version: '1.0.0',
    description: 'Pattern matching using regular expressions',
    requires_api_key: false,
    timeout_ms: 5000,
    config_schema: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Regular expression pattern' },
      },
      required: ['pattern'],
    },
  },
  list: {
    name: 'List',
    version: '1.0.0',
    description: 'Match against a list of allowed or blocked values',
    requires_api_key: false,
    timeout_ms: 5000,
    config_schema: {
      type: 'object',
      properties: {
        values: { type: 'array', items: { type: 'string' } },
        logic: { type: 'string', enum: ['any', 'all'] },
        match_on: { type: 'string', enum: ['match', 'no_match'] },
      },
      required: ['values'],
    },
  },
  sql: {
    name: 'SQL',
    version: '1.0.0',
    description: 'SQL injection detection and prevention',
    requires_api_key: false,
    timeout_ms: 5000,
    config_schema: {
      type: 'object',
      properties: {
        mode: { type: 'string', enum: ['safe', 'strict'] },
      },
    },
  },
  json: {
    name: 'JSON',
    version: '1.0.0',
    description: 'JSON schema validation',
    requires_api_key: false,
    timeout_ms: 5000,
    config_schema: {
      type: 'object',
      properties: {
        schema: { type: 'object' },
      },
      required: ['schema'],
    },
  },
  'galileo.luna': {
    name: 'Galileo Luna',
    version: '1.0.0',
    description: 'Galileo Luna direct scorer evaluation',
    requires_api_key: true,
    timeout_ms: 10000,
    config_schema: {
      type: 'object',
      properties: {
        scorer_label: { type: 'string' },
        scorer_id: { type: 'string' },
        scorer_version_id: { type: 'string' },
        threshold: {},
        operator: {
          type: 'string',
          enum: ['gt', 'gte', 'lt', 'lte', 'eq', 'ne', 'contains', 'any'],
        },
        payload_field: { type: 'string', enum: ['input', 'output'] },
        timeout_ms: { type: 'integer', minimum: 1000, maximum: 60000 },
        config: { type: 'object' },
      },
    },
  },
  'defenseclaw.rule_pack': {
    name: 'DefenseClaw Rule Pack',
    version: '1.0.0',
    description: 'DefenseClaw rule-pack evaluation',
    requires_api_key: false,
    timeout_ms: 10000,
    config_schema: {
      type: 'object',
      properties: {
        schema_version: { type: 'integer', const: 1, default: 1 },
        rule_pack: {
          type: 'object',
          properties: {
            version: { type: 'integer', const: 1, default: 1 },
            category: {
              type: 'string',
              const: 'agent-control',
              default: 'agent-control',
            },
            rules: {
              type: 'array',
              minItems: 1,
              items: {
                type: 'object',
                properties: {
                  id: { type: 'string', minLength: 1 },
                  pattern: { type: 'string', minLength: 1 },
                  title: { type: 'string', minLength: 1 },
                  severity: {
                    type: 'string',
                    enum: ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'],
                  },
                  confidence: { type: 'number', minimum: 0, maximum: 1 },
                  tags: { type: 'array', items: { type: 'string' } },
                },
              },
            },
          },
        },
      },
    },
  },
  'defenseclaw.opa_policy': {
    name: 'DefenseClaw OPA Policy',
    version: '1.0.0',
    description: 'DefenseClaw OPA-policy evaluation',
    requires_api_key: false,
    timeout_ms: 10000,
    config_schema: {
      type: 'object',
      properties: {
        schema_version: { type: 'integer', const: 1, default: 1 },
        policy: {
          type: 'object',
          properties: {
            domain: { type: 'string', const: 'guardrail' },
            block_at: {
              type: 'string',
              enum: ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'],
            },
            alert_at: {
              type: 'string',
              enum: ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'],
            },
            cisco_trust_level: { type: 'string', const: 'full' },
          },
        },
      },
    },
  },
};

const controlSchemaResponse: GetControlSchemaResponse = {
  schema: {
    $defs: {
      ControlSelector: {
        type: 'object',
        properties: {
          path: {
            anyOf: [{ type: 'string' }, { type: 'null' }],
            default: '*',
            examples: ['output', 'context.user_id', '*'],
          },
        },
      },
      EvaluatorSpec: {
        type: 'object',
        required: ['name', 'config'],
        properties: {
          name: {
            type: 'string',
            examples: ['regex', 'list', 'customer-support-bot:risk-threshold'],
          },
          config: {
            type: 'object',
            additionalProperties: true,
          },
        },
      },
      ConditionNode: {
        type: 'object',
        properties: {
          selector: {
            anyOf: [{ $ref: '#/$defs/ControlSelector' }, { type: 'null' }],
          },
          evaluator: {
            anyOf: [{ $ref: '#/$defs/EvaluatorSpec' }, { type: 'null' }],
          },
          and: {
            anyOf: [
              {
                type: 'array',
                items: { $ref: '#/$defs/ConditionNode' },
              },
              { type: 'null' },
            ],
          },
          or: {
            anyOf: [
              {
                type: 'array',
                items: { $ref: '#/$defs/ConditionNode' },
              },
              { type: 'null' },
            ],
          },
          not: {
            anyOf: [{ $ref: '#/$defs/ConditionNode' }, { type: 'null' }],
          },
        },
      },
      ControlScope: {
        type: 'object',
        properties: {
          step_types: {
            anyOf: [
              { type: 'array', items: { type: 'string' } },
              { type: 'null' },
            ],
          },
          step_names: {
            anyOf: [
              { type: 'array', items: { type: 'string' } },
              { type: 'null' },
            ],
          },
          step_name_regex: {
            anyOf: [{ type: 'string' }, { type: 'null' }],
          },
          stages: {
            anyOf: [
              {
                type: 'array',
                items: { type: 'string', enum: ['pre', 'post'] },
              },
              { type: 'null' },
            ],
          },
        },
      },
      SteeringContext: {
        type: 'object',
        required: ['message'],
        properties: {
          message: { type: 'string' },
        },
      },
      ControlAction: {
        type: 'object',
        required: ['decision'],
        properties: {
          decision: {
            type: 'string',
            enum: ['allow', 'deny', 'steer', 'warn', 'log'],
          },
          steering_context: {
            anyOf: [{ $ref: '#/$defs/SteeringContext' }, { type: 'null' }],
          },
        },
      },
    },
    type: 'object',
    required: ['execution', 'condition', 'action'],
    properties: {
      description: {
        anyOf: [{ type: 'string' }, { type: 'null' }],
      },
      enabled: { type: 'boolean' },
      execution: { type: 'string', enum: ['server', 'sdk'] },
      scope: {
        $ref: '#/$defs/ControlScope',
      },
      condition: {
        $ref: '#/$defs/ConditionNode',
      },
      action: {
        $ref: '#/$defs/ControlAction',
      },
      tags: {
        type: 'array',
        items: { type: 'string' },
      },
    },
  },
};

const statsResponse: StatsResponse = {
  agent_name: 'customer-support-bot',
  time_range: '1h',
  totals: {
    execution_count: 430,
    match_count: 40,
    non_match_count: 390,
    error_count: 2,
    action_counts: {
      observe: 15,
      deny: 25,
      steer: 0,
    },
  },
  controls: [
    {
      control_id: 1,
      control_name: 'PII Detection',
      execution_count: 150,
      match_count: 25,
      non_match_count: 125,
      observe_count: 10,
      deny_count: 15,
      steer_count: 0,
      error_count: 0,
      avg_confidence: 0.92,
      avg_duration_ms: 45,
    },
    {
      control_id: 2,
      control_name: 'SQL Injection Guard',
      execution_count: 80,
      match_count: 10,
      non_match_count: 70,
      observe_count: 0,
      deny_count: 10,
      steer_count: 0,
      error_count: 2,
      avg_confidence: 0.88,
      avg_duration_ms: 32,
    },
    {
      control_id: 3,
      control_name: 'Rate Limiter',
      execution_count: 200,
      match_count: 5,
      non_match_count: 195,
      observe_count: 5,
      deny_count: 0,
      steer_count: 0,
      error_count: 0,
      avg_confidence: 0.95,
      avg_duration_ms: 12,
    },
  ],
};

const emptyStatsResponse: StatsResponse = {
  agent_name: 'customer-support-bot',
  time_range: '1h',
  totals: {
    execution_count: 0,
    match_count: 0,
    non_match_count: 0,
    error_count: 0,
    action_counts: {},
  },
  controls: [],
};

// =============================================================================
// Teams
// =============================================================================

function teamSummary(
  overrides: Partial<TeamSummary> & Pick<TeamSummary, 'slug' | 'display_name'>
): TeamSummary {
  return {
    id: 1,
    namespace_key: 'default',
    description: null,
    linear_team_key: null,
    default_agent_name: null,
    member_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

const teamMemberNames: Record<string, string[]> = {
  'sales-outreach': [
    'customer-support-bot',
    'lead-qualifier',
    'outreach-scheduler',
  ],
  engineering: ['code-review-assistant', 'data-analysis-agent'],
  marketing: [],
};

const teamsList: TeamSummary[] = [
  teamSummary({
    id: 1,
    slug: 'sales-outreach',
    display_name: 'Sales & Outreach',
    description: 'Owns pipeline, prospecting and follow-up.',
    default_agent_name: 'lead-qualifier',
    member_count: teamMemberNames['sales-outreach'].length,
  }),
  teamSummary({
    id: 2,
    slug: 'engineering',
    display_name: 'Engineering',
    description: 'Builds and runs the platform.',
    member_count: teamMemberNames.engineering.length,
  }),
  // No members and no description: the sparsest card the overview can render.
  teamSummary({
    id: 3,
    slug: 'marketing',
    display_name: 'Marketing',
    member_count: 0,
  }),
];

const teamsResponse: ListTeamsResponse = {
  teams: teamsList,
  pagination: {
    total: teamsList.length,
    limit: 100,
    has_more: false,
    next_cursor: null,
  },
};

const emptyTeamsResponse: ListTeamsResponse = {
  teams: [],
  pagination: {
    total: 0,
    limit: 100,
    has_more: false,
    next_cursor: null,
  },
};

/** Longest name and largest membership the API permits, for layout tests. */
export const crowdedTeamName =
  'Global Revenue Operations, Partner Enablement and Strategic Customer Success Organisation';

const crowdedMemberNames = Array.from(
  { length: 14 },
  (_, index) =>
    `extremely-long-agent-name-for-layout-testing-${String(index + 1).padStart(2, '0')}`
);

const crowdedTeam: TeamSummary = teamSummary({
  id: 4,
  slug: 'global-revenue-operations',
  display_name: crowdedTeamName,
  description:
    'A description long enough to need clamping: this team coordinates ' +
    'revenue operations, partner enablement, customer success, renewals, ' +
    'expansion and every adjacent motion across all regions and segments.',
  member_count: crowdedMemberNames.length,
});

const crowdedTeamsResponse: ListTeamsResponse = {
  teams: [crowdedTeam, ...teamsList],
  pagination: {
    total: teamsList.length + 1,
    limit: 100,
    has_more: false,
    next_cursor: null,
  },
};

function teamDetail(team: TeamSummary, memberNames: string[]): GetTeamResponse {
  return {
    ...team,
    members: memberNames.map((agent_name) => ({
      agent_name,
      joined_at: '2024-01-05T00:00:00Z',
    })),
  };
}

const teamDetails: Record<string, GetTeamResponse> = {
  ...Object.fromEntries(
    teamsList.map((team) => [
      team.slug,
      teamDetail(team, teamMemberNames[team.slug] ?? []),
    ])
  ),
  [crowdedTeam.slug]: teamDetail(crowdedTeam, crowdedMemberNames),
};

// =============================================================================
// Team detail: agents filtered to a team, and Linear milestones
// =============================================================================

/**
 * An AgentSummary for any member name.
 *
 * Members that also appear on the agents overview reuse that row, so the two
 * pages agree on control counts. Names unique to a team get a synthesized row.
 */
function agentSummaryFor(agentName: string, index: number): AgentSummary {
  const known = agentsList.find((agent) => agent.agent_name === agentName);
  if (known) return known;
  return {
    agent_name: agentName,
    policy_ids: [],
    created_at: '2024-02-01T00:00:00Z',
    step_count: index + 1,
    evaluator_count: 1,
    active_controls_count: index === 0 ? 1 : index + 1,
  };
}

/** Membership by slug, as the `?team=` filter on the agents endpoint sees it. */
const teamAgents: Record<string, AgentSummary[]> = {
  ...Object.fromEntries(
    Object.entries(teamMemberNames).map(([slug, names]) => [
      slug,
      names.map(agentSummaryFor),
    ])
  ),
  [crowdedTeam.slug]: crowdedMemberNames.map(agentSummaryFor),
};

function milestone(overrides: Partial<Milestone> = {}): Milestone {
  return {
    id: 'ms-1',
    name: 'Beta launch',
    description: 'Ship the beta to design partners.',
    target_date: '2026-09-01',
    status: 'unstarted',
    progress: 0.25,
    project_id: 'proj-1',
    project_name: 'Platform',
    project_url: 'https://linear.app/acme/project/platform',
    ...overrides,
  };
}

const milestonesList: Milestone[] = [
  milestone(),
  milestone({
    id: 'ms-2',
    name: 'GA',
    status: 'next',
    progress: 0.6,
    target_date: '2026-12-15',
  }),
  // No date, no progress, no project: the sparsest row the panel can draw.
  milestone({
    id: 'ms-3',
    name: 'Untargeted follow-up work',
    status: null,
    progress: null,
    target_date: null,
    project_id: null,
    project_name: null,
    project_url: null,
  }),
];

function milestonesResponse(
  slug: string,
  overrides: Partial<ListTeamMilestonesResponse> = {}
): ListTeamMilestonesResponse {
  return {
    status: 'ok',
    slug,
    linear_team_key: 'ENG',
    milestones: milestonesList,
    error: null,
    retry_after_seconds: null,
    cached: false,
    fetched_at: '2026-08-01T09:30:00Z',
    ...overrides,
  };
}

/** One canned response per documented milestone status. */
const milestoneStates = {
  ok: (slug: string) => milestonesResponse(slug),
  empty: (slug: string) =>
    milestonesResponse(slug, { status: 'empty', milestones: [] }),
  not_linked: (slug: string) =>
    milestonesResponse(slug, {
      status: 'not_linked',
      linear_team_key: null,
      milestones: [],
      fetched_at: null,
    }),
  not_configured: (slug: string) =>
    milestonesResponse(slug, {
      status: 'not_configured',
      linear_team_key: null,
      milestones: [],
      fetched_at: null,
    }),
  error: (slug: string) =>
    milestonesResponse(slug, {
      status: 'error',
      milestones: [],
      error: 'Linear did not answer in time.',
      retry_after_seconds: 30,
      fetched_at: null,
    }),
} as const;

export type MilestoneState = keyof typeof milestoneStates;

// =============================================================================
// Agent chat fixtures
//
// The transcript is the only untrusted content this UI renders, so the mocks
// below are shaped like the real endpoint rather than like whatever the panel
// happens to read: absolute message indexes, `after_index` paging, and the two
// in-flight fields kept distinct (see `AgentSessionDetail`).
// =============================================================================

/** The agent `mockRoutes.agent` answers with, whatever id the URL carries. */
export const chatAgentName = 'customer-support-bot';

function chatSession(
  sessionKey: string,
  title: string,
  createdAt: string
): AgentSessionSummary {
  return {
    session_key: sessionKey,
    namespace_key: 'default',
    agent_name: chatAgentName,
    team_slug: null,
    title,
    status: 'active',
    executor_kind: 'google_adk',
    last_trace_id: null,
    last_activity_at: createdAt,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

/** Newest first, the order the list endpoint returns. */
const chatSessions: AgentSessionSummary[] = [
  chatSession('sess-refunds', 'Refund policy', '2024-03-02T09:00:00Z'),
  chatSession(
    'sess-onboarding',
    'Onboarding checklist',
    '2024-03-01T09:00:00Z'
  ),
];

const chatTranscripts: Record<string, SessionMessage[]> = {
  'sess-refunds': [
    {
      index: 0,
      role: 'user',
      timestamp: '2024-03-02T09:00:01Z',
      parts: [{ kind: 'text', text: 'What is our refund window?' }],
    },
    {
      index: 1,
      role: 'agent',
      author: chatAgentName,
      timestamp: '2024-03-02T09:00:09Z',
      parts: [
        {
          kind: 'tool_call',
          tool_name: 'fetch_policy',
          tool_call_id: 'call-1',
          arguments: { topic: 'refunds', locale: 'en-GB' },
        },
        {
          kind: 'tool_result',
          tool_name: 'fetch_policy',
          tool_call_id: 'call-1',
          result: { window_days: 30, source: 'policies/refunds.md' },
        },
        {
          kind: 'text',
          text: 'Refunds are accepted within 30 days of delivery.',
        },
      ],
    },
  ],
  'sess-onboarding': [
    {
      index: 0,
      role: 'user',
      timestamp: '2024-03-01T09:00:01Z',
      parts: [{ kind: 'text', text: 'Draft the onboarding checklist.' }],
    },
    {
      index: 1,
      role: 'agent',
      author: chatAgentName,
      timestamp: '2024-03-01T09:00:04Z',
      parts: [{ kind: 'text', text: 'Step one: create the workspace.' }],
    },
  ],
};

/** A refusal in the shape the server actually writes it. */
export type ProblemMock = {
  status: number;
  errorCode: string;
  detail: string;
  title?: string;
  hint?: string;
};

function problemBody(problem: ProblemMock) {
  return JSON.stringify({
    type: 'about:blank',
    title: problem.title ?? 'Error',
    status: problem.status,
    detail: problem.detail,
    error_code: problem.errorCode,
    reason: problem.title ?? 'Error',
    ...(problem.hint ? { hint: problem.hint } : {}),
  });
}

/**
 * The filename out of a multipart upload body.
 *
 * Read off the raw body rather than through a form parser, because the point of
 * these tests is what the panel does with a name it did not choose, and a
 * parser that normalized or rejected one would hide exactly that.
 */
function uploadedFilename(request: {
  postData(): string | null;
}): string | null {
  const body = request.postData();
  if (!body) return null;
  const match = /filename="([^"]*)"/.exec(body);
  return match ? match[1] : null;
}

export type AgentSessionsMockOptions = {
  /** Newest first. An empty array is an agent nobody has chatted with. */
  sessions?: AgentSessionSummary[];
  /** Absolute-indexed transcript per session key. */
  transcripts?: Record<string, SessionMessage[]>;
  /**
   * Held open before a turn answers. While it is unresolved the turn holds
   * the session, which is what the detail endpoint reports.
   */
  turnGate?: Promise<void>;
  /** What the completed turn appends to the transcript. */
  turnReply?: SessionMessagePart[];
  turnError?: ProblemMock;
  listError?: ProblemMock;
  messagesError?: ProblemMock;
  createError?: ProblemMock;
  /** Merged into a session's detail response, e.g. a stranded invocation. */
  detailOverrides?: Record<string, Partial<AgentSessionDetail>>;
  /** Queued guidance per session key, newest first, as the server returns it. */
  nudges?: Record<string, Nudge[]>;
  /** Stops recorded per session key, newest first. */
  halts?: Record<string, Halt[]>;
  /** Refusal for `POST /nudges`, e.g. the per-session queue ceiling. */
  nudgeCreateError?: ProblemMock;
  /** Refusal for `DELETE /nudges/:id`, e.g. a nudge already claimed. */
  nudgeCancelError?: ProblemMock;
  /** Refusal for `POST /halts`, e.g. nothing in flight to stop. */
  haltCreateError?: ProblemMock;
  /** Files already stored per session key, oldest first, as the server lists them. */
  attachments?: Record<string, Attachment[]>;
  /** Refusal for the upload, e.g. a 415 on a type this deployment will not take. */
  attachmentUploadError?: ProblemMock;
  /**
   * The plan each session's agent declared, if it declared one.
   *
   * Absent is the ordinary case and the one the fallback view exists for, so
   * the default here is no plan rather than an empty one.
   */
  plans?: Record<string, Plan>;
};

export type AgentSessionsMock = {
  /** Every turn the browser started, in order. */
  turns: Array<{ sessionKey: string; message: string }>;
  /**
   * The attachment keys each turn carried, in order.
   *
   * Recorded separately from `turns` rather than added to it: a turn with no
   * files must go out with exactly the body this endpoint has always been
   * sent, and a single list would make "sent no field" and "sent an empty
   * list" indistinguishable in the assertion.
   */
  turnAttachmentKeys: Array<{ sessionKey: string; keys: string[] | undefined }>;
  /** Every file the browser uploaded, in order. */
  attachmentsUploaded: Array<{ sessionKey: string; name: string }>;
  /** Every file the browser removed, in order. */
  attachmentsDeleted: Array<{ sessionKey: string; attachmentKey: string }>;
  /** Stored attachments per session, mutable so a test can spoil one. */
  attachments: Record<string, Attachment[]>;
  /** Every transcript read, for asserting a poll did or did not happen. */
  messageReads: string[];
  /** Live transcripts, so a test can assert what the panel was given. */
  transcripts: Record<string, SessionMessage[]>;
  /** Every nudge queued from the browser, in order. */
  nudgesQueued: Array<{ sessionKey: string; body: string }>;
  /** Every nudge withdrawal, in order. */
  nudgesCancelled: Array<{ sessionKey: string; nudgeId: number }>;
  /** Every stop pressed, in order. Repeats are visible, as on the server. */
  haltsRequested: string[];
  /**
   * Live queues and stop records, mutable so a test can move one on mid-run.
   *
   * That is the point of exposing them: what the panel says about a stop
   * changes as the server's own record changes underneath it - queued, then
   * acknowledged by the executor, then observed to have ended - and none of
   * those transitions are things the browser causes.
   */
  nudges: Record<string, Nudge[]>;
  halts: Record<string, Halt[]>;
  /**
   * Declared plans per session, mutable so a test can have the agent replan
   * mid-run. Nothing the browser does changes these: plans are written by the
   * agent under its own session-bound credential.
   */
  plans: Record<string, Plan>;
};

/**
 * Mock the five agent-session endpoints the chat panel calls.
 *
 * One handler rather than five routes: Playwright's `*` spans `/`, so
 * separate patterns for `/agent-sessions/:key` and `/agent-sessions/:key/turns`
 * overlap and the ordering rules are easier to get wrong than a switch on the
 * path is to read.
 */
async function mockAgentSessions(
  page: Page,
  options: AgentSessionsMockOptions = {}
): Promise<AgentSessionsMock> {
  const sessions = [...(options.sessions ?? chatSessions)];
  const transcripts: Record<string, SessionMessage[]> = Object.fromEntries(
    Object.entries(options.transcripts ?? chatTranscripts).map(
      ([key, messages]) => [key, [...messages]]
    )
  );
  const cloneByKey = <T>(source: Record<string, T[]> | undefined) =>
    Object.fromEntries(
      Object.entries(source ?? {}).map(([key, rows]) => [key, [...rows]])
    ) as Record<string, T[]>;

  const state: AgentSessionsMock = {
    turns: [],
    turnAttachmentKeys: [],
    attachmentsUploaded: [],
    attachmentsDeleted: [],
    attachments: cloneByKey(options.attachments),
    messageReads: [],
    transcripts,
    nudgesQueued: [],
    nudgesCancelled: [],
    haltsRequested: [],
    nudges: cloneByKey(options.nudges),
    halts: cloneByKey(options.halts),
    plans: { ...(options.plans ?? {}) },
  };

  /** Sessions a turn is currently holding, and since when. */
  const inFlightSince = new Map<string, string>();
  let created = 0;
  let nextOperatorId = 1000;

  const detailOf = (sessionKey: string): AgentSessionDetail | null => {
    const summary = sessions.find((s) => s.session_key === sessionKey);
    if (!summary) return null;
    const since = inFlightSince.get(sessionKey) ?? null;
    return {
      ...summary,
      in_flight_since: since,
      in_flight_trace_id: since ? 'trace-in-flight' : null,
      ...(options.detailOverrides?.[sessionKey] ?? {}),
    };
  };

  await page.route('**/api/v1/agent-sessions**', async (route, request) => {
    const url = new URL(request.url());
    const segments = url.pathname.split('/').filter(Boolean);
    // ['api','v1','agent-sessions', <key>?, <sub>?]
    const sessionKey = segments[3] ? decodeURIComponent(segments[3]) : null;
    const sub = segments[4] ?? null;
    const method = request.method();

    const json = async (body: unknown, status = 200) => {
      await route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    };
    const problem = async (p: ProblemMock) => {
      await route.fulfill({
        status: p.status,
        contentType: 'application/json',
        body: problemBody(p),
      });
    };

    if (sessionKey === null) {
      if (method === 'POST') {
        if (options.createError) {
          await problem(options.createError);
          return;
        }
        created += 1;
        const session = chatSession(
          `sess-new-${created}`,
          `New chat ${created}`,
          new Date().toISOString()
        );
        sessions.unshift(session);
        transcripts[session.session_key] ??= [];
        await json({ session: { ...session, in_flight_since: null } });
        return;
      }

      if (options.listError) {
        await problem(options.listError);
        return;
      }
      const agent = url.searchParams.get('agent');
      const matching = agent
        ? sessions.filter((s) => s.agent_name === agent)
        : sessions;
      await json({
        sessions: matching,
        pagination: {
          total: matching.length,
          limit: 50,
          has_more: false,
          next_cursor: null,
        },
      });
      return;
    }

    if (sub === 'messages') {
      state.messageReads.push(sessionKey);
      if (options.messagesError) {
        await problem(options.messagesError);
        return;
      }
      const all = transcripts[sessionKey] ?? [];
      const afterIndexParam = url.searchParams.get('after_index');
      const start = afterIndexParam === null ? 0 : Number(afterIndexParam) + 1;
      const limit = Number(url.searchParams.get('limit') ?? '100');
      const window = all.slice(start, start + limit);
      const consumed = start + window.length;
      await json({
        session_key: sessionKey,
        status: detailOf(sessionKey)?.status ?? 'active',
        messages: window,
        next_index: consumed < all.length ? consumed - 1 : null,
        has_more: consumed < all.length,
        total: all.length,
        notice: null,
      });
      return;
    }

    if (sub === 'turns' && method === 'POST') {
      const body = (request.postDataJSON() ?? {}) as {
        message?: string;
        attachment_keys?: string[];
      };
      state.turns.push({ sessionKey, message: body.message ?? '' });
      state.turnAttachmentKeys.push({
        sessionKey,
        keys: body.attachment_keys,
      });
      inFlightSince.set(sessionKey, new Date().toISOString());
      try {
        if (options.turnGate) await options.turnGate;
        if (options.turnError) {
          await problem(options.turnError);
          return;
        }
        const existing = transcripts[sessionKey] ?? [];
        const next = existing.length;
        const userMessage: SessionMessage = {
          index: next,
          role: 'user',
          timestamp: new Date().toISOString(),
          parts: [{ kind: 'text', text: body.message ?? '' }],
        };
        const agentMessage: SessionMessage = {
          index: next + 1,
          role: 'agent',
          author: chatAgentName,
          timestamp: new Date().toISOString(),
          parts: options.turnReply ?? [
            { kind: 'text', text: 'Understood. I have noted that.' },
          ],
        };
        transcripts[sessionKey] = [...existing, userMessage, agentMessage];
        await json({
          session_key: sessionKey,
          trace_id: `trace-${state.turns.length}`,
          started_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
          duration_seconds: 1.5,
          messages: [
            { ...userMessage, index: 0 },
            { ...agentMessage, index: 1 },
          ],
        });
      } catch {
        // The panel abandoned the request. Nothing left to answer.
      } finally {
        inFlightSince.delete(sessionKey);
      }
      return;
    }

    // Files stored against one session. The upload answers with the row the
    // server would have written, including the normalized display name, so a
    // test can hand the panel a filename nobody should ever render as markup
    // and assert what it did with it.
    if (sub === 'attachments') {
      const stored = (state.attachments[sessionKey] ??= []);
      const attachmentKey = segments[5]
        ? decodeURIComponent(segments[5])
        : null;

      if (method === 'DELETE' && attachmentKey !== null) {
        state.attachmentsDeleted.push({ sessionKey, attachmentKey });
        const index = stored.findIndex(
          (row) => row.attachment_key === attachmentKey
        );
        if (index !== -1) stored.splice(index, 1);
        await json({
          deleted: true,
          notice:
            'Removed from Agent Control and from future turns. A model that ' +
            'already read this file has already read it.',
        });
        return;
      }

      if (method === 'POST') {
        const name = uploadedFilename(request) ?? 'file';
        state.attachmentsUploaded.push({ sessionKey, name });
        if (options.attachmentUploadError) {
          await problem(options.attachmentUploadError);
          return;
        }
        nextOperatorId += 1;
        const attachment: Attachment = {
          attachment_key: `att-${nextOperatorId}`,
          session_key: sessionKey,
          display_name: name,
          display_name_normalized: false,
          declared_mime: 'application/pdf',
          sniffed_mime: 'application/pdf',
          mime_mismatch: false,
          size_bytes: 2_400_000,
          source_sha256: 'a'.repeat(64),
          status: 'ready',
          origin: 'operator_upload',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        stored.push(attachment);
        await json({ attachment, deduplicated: false }, 201);
        return;
      }

      await json({
        attachments: stored,
        count: stored.length,
        total_bytes: stored.reduce((sum, row) => sum + row.size_bytes, 0),
      });
      return;
    }

    // Guidance queued for an agent that is already working. The list is
    // newest first, matching the server, and nothing here ever moves a nudge
    // on by itself: an executor claims and applies them on its own schedule,
    // so a test that wants "delivered" says so by editing `state.nudges`.
    if (sub === 'nudges') {
      const queue = (state.nudges[sessionKey] ??= []);
      const nudgeId = segments[5] ? Number(segments[5]) : null;

      if (method === 'DELETE' && nudgeId !== null) {
        state.nudgesCancelled.push({ sessionKey, nudgeId });
        if (options.nudgeCancelError) {
          await problem(options.nudgeCancelError);
          return;
        }
        const index = queue.findIndex((nudge) => nudge.id === nudgeId);
        if (index === -1) {
          await problem({
            status: 404,
            errorCode: 'NUDGE_NOT_FOUND',
            title: 'Not Found',
            detail: 'Nudge not found on this session.',
          });
          return;
        }
        const cancelled: Nudge = { ...queue[index], status: 'cancelled' };
        queue[index] = cancelled;
        await json({ cancelled: true, nudge: cancelled });
        return;
      }

      if (method === 'POST') {
        if (options.nudgeCreateError) {
          await problem(options.nudgeCreateError);
          return;
        }
        const body = (request.postDataJSON() ?? {}) as { body?: string };
        state.nudgesQueued.push({ sessionKey, body: body.body ?? '' });
        nextOperatorId += 1;
        const nudge: Nudge = {
          id: nextOperatorId,
          session_key: sessionKey,
          body: body.body ?? '',
          status: 'pending',
          created_at: new Date().toISOString(),
          claim_count: 0,
          injection_attempts: 0,
        };
        queue.unshift(nudge);
        await json({ nudge });
        return;
      }
      await json({ session_key: sessionKey, nudges: queue });
      return;
    }

    // Stopping a turn. Bound to the session's liveness marker, and refused
    // with the server's own 409 when there is no live turn to bind to.
    if (sub === 'halts') {
      const recorded = (state.halts[sessionKey] ??= []);
      if (method === 'POST') {
        state.haltsRequested.push(sessionKey);
        if (options.haltCreateError) {
          await problem(options.haltCreateError);
          return;
        }
        const traceId = detailOf(sessionKey)?.in_flight_trace_id ?? null;
        if (!traceId) {
          await problem({
            status: 409,
            errorCode: 'TURN_NOT_IN_FLIGHT',
            title: 'Conflict',
            detail:
              'This session is not running a turn, so there is nothing to stop.',
          });
          return;
        }
        // One turn, one stop: a second press answers with the first row.
        const existing = recorded.find((h) => h.target_trace_id === traceId);
        if (existing) {
          await json({ halt: existing, created: false });
          return;
        }
        nextOperatorId += 1;
        const halt: Halt = {
          id: nextOperatorId,
          session_key: sessionKey,
          target_trace_id: traceId,
          mode: 'graceful',
          status: 'pending',
          created_at: new Date().toISOString(),
        };
        recorded.unshift(halt);
        await json({ halt, created: true });
        return;
      }
      await json({ session_key: sessionKey, halts: recorded });
      return;
    }

    // What the agent says it is doing. Read-only from a browser, and null when
    // the agent never declared a plan, which is the ordinary case the panel's
    // fallback view exists for.
    if (sub === 'plan' && method === 'GET') {
      await json({
        session_key: sessionKey,
        plan: state.plans[sessionKey] ?? null,
      });
      return;
    }

    if (sub === null && method === 'GET') {
      const detail = detailOf(sessionKey);
      if (!detail) {
        await problem({
          status: 404,
          errorCode: 'SESSION_NOT_FOUND',
          detail: 'That chat no longer exists.',
          title: 'Not Found',
        });
        return;
      }
      await json({ session: detail });
      return;
    }

    await route.fallback();
  });

  return state;
}

// =============================================================================
// Agent configuration: one row carrying the system prompt and the model
//
// Writes are applied to the mock's own row rather than answered with a canned
// body, because most of what this tab promises is about what comes back after
// a write: the version number the toast names, the history row the save
// created, and the `expected_version` the next save is built from. A mock that
// always returned version 4 would let a broken invalidation pass.
// =============================================================================

/** The allowlist a server offers, spanning all three cost tiers. */
const agentModelOptions: AgentModelOption[] = [
  {
    id: 'gpt-5.4-mini',
    label: 'GPT-5.4 mini',
    provider: 'openai_compatible',
    cost_tier: 'economy',
    recommended: false,
  },
  {
    id: 'gpt-5.6-sol',
    label: 'GPT-5.6 Sol',
    provider: 'openai_compatible',
    cost_tier: 'premium',
    recommended: true,
  },
  {
    id: 'gemini-2.5-flash',
    label: 'Gemini 2.5 Flash',
    provider: 'gemini',
    cost_tier: 'standard',
    recommended: false,
  },
];

const storedPromptBody =
  'You are the support agent for Acme.\n' +
  'Cite the policy you used in every answer.';

function agentConfig(
  overrides: Partial<GetAgentConfigResponse> = {}
): GetAgentConfigResponse {
  return {
    agent_name: chatAgentName,
    body: storedPromptBody,
    body_format: 'text',
    prompt_enabled: true,
    prompt_source: 'managed',
    model_id: 'gpt-5.4-mini',
    model_provider: 'openai_compatible',
    model_allowed: true,
    model_cost_tier: 'economy',
    model_source: 'managed',
    delivery_state: 'active',
    etag: 'v3-1a2b3c4d5e6f',
    current_version: 3,
    source_instruction: null,
    source_reported_at: null,
    updated_by_hash: 'c0ffee1234abcd',
    created_at: '2026-07-01T09:00:00Z',
    updated_at: '2026-07-30T11:15:00Z',
    ...overrides,
  };
}

/** Newest first, the order the versions endpoint returns. */
const agentConfigVersionsList: AgentConfigVersionSummary[] = [
  {
    version_num: 3,
    event_type: 'updated',
    origin: 'authored',
    model_id: 'gpt-5.4-mini',
    note: 'Tighter policy citation wording.',
    has_body: true,
    scan_findings: [],
    changed_by_hash: 'c0ffee1234abcd',
    created_at: '2026-07-30T11:15:00Z',
  },
  {
    version_num: 2,
    event_type: 'updated',
    origin: 'authored',
    model_id: 'gpt-5.6-sol',
    note: null,
    has_body: true,
    scan_findings: [
      {
        scanner: 'secret_patterns',
        severity: 'warning',
        code: 'SECRET_LIKE_STRING',
        message: 'A string shaped like an API key was found in this body.',
        match_count: 1,
      },
    ],
    changed_by_hash: 'c0ffee1234abcd',
    created_at: '2026-07-12T08:00:00Z',
  },
  {
    version_num: 1,
    event_type: 'created',
    origin: 'authored',
    model_id: null,
    note: 'First prompt for this agent.',
    has_body: true,
    scan_findings: [],
    changed_by_hash: 'c0ffee1234abcd',
    created_at: '2026-07-01T09:00:00Z',
  },
];

const agentConfigVersionBodies: Record<number, string | null> = {
  3: storedPromptBody,
  2: 'You are the support agent for Acme.\nAlways answer in one paragraph.',
  1: 'You are a support agent.',
};

export type AgentConfigMockOptions = {
  /** The row the tab loads. */
  config?: Partial<GetAgentConfigResponse>;
  /** The server allowlist. An empty array is a server with none configured. */
  models?: AgentModelOption[];
  /**
   * Refusal for `GET /agent-models`. A 403 is the read-only path; anything
   * else is a failed request, which must not be read as a claim about the
   * deployment.
   */
  modelsError?: ProblemMock;
  versions?: AgentConfigVersionSummary[];
  /** Full bodies by version number, fetched when a row is opened. */
  versionBodies?: Record<number, string | null>;
  configError?: ProblemMock;
  versionsError?: ProblemMock;
  saveError?: ProblemMock;
  clearError?: ProblemMock;
  restoreError?: ProblemMock;
  enableError?: ProblemMock;
  /** Advisory findings the next save answers with. Never blocks the write. */
  saveFindings?: ScanFinding[];
};

export type AgentConfigMock = {
  /** Every body that left the browser on `PUT .../config`, in order. */
  saves: SetAgentConfigRequest[];
  clears: Array<{
    field: 'prompt' | 'model';
    body: ClearAgentConfigFieldRequest;
  }>;
  restores: Array<{
    versionNum: number;
    body: RestoreAgentConfigVersionRequest;
  }>;
  enablePatches: SetPromptEnabledRequest[];
  /**
   * Whether `saveError` is still in force.
   *
   * Mutable so a test can refuse one save and accept the next, which is the
   * only way to exercise recovery from a conflict: refuse, let the editor
   * reload the row somebody else wrote, then accept the re-applied edit.
   */
  failSaves: boolean;
  /** The live row, mutated by every write the mock accepts. */
  config: GetAgentConfigResponse;
  /** The live history, newest first. */
  versions: AgentConfigVersionSummary[];
  versionBodies: Record<number, string | null>;
  /** How many times the tab re-read the row. */
  configReads: number;
};

/**
 * Mock the eight agent-config routes plus the deployment's model allowlist.
 *
 * One handler over `**\/api\/v1\/agents\/**` rather than a route per path: the
 * clear routes carry a verb suffix on the same path the row lives at, so
 * separate patterns overlap in ways that are easier to get wrong than a switch
 * on the tail is to read. Anything that is not a config path falls through to
 * whichever handler was registered before this one.
 */
async function mockAgentConfig(
  page: Page,
  options: AgentConfigMockOptions = {}
): Promise<AgentConfigMock> {
  const state: AgentConfigMock = {
    saves: [],
    clears: [],
    restores: [],
    enablePatches: [],
    failSaves: Boolean(options.saveError),
    config: agentConfig(options.config),
    versions: [...(options.versions ?? agentConfigVersionsList)],
    versionBodies: { ...(options.versionBodies ?? agentConfigVersionBodies) },
    configReads: 0,
  };

  const models = options.models ?? agentModelOptions;

  const appendVersion = (
    eventType: AgentConfigVersionSummary['event_type'],
    extra: Partial<AgentConfigVersionSummary> = {}
  ): number => {
    state.config.current_version += 1;
    const versionNum = state.config.current_version;
    state.config.etag = `v${versionNum}-1a2b3c4d5e6f`;
    state.versions.unshift({
      version_num: versionNum,
      event_type: eventType,
      origin: 'authored',
      model_id: state.config.model_id ?? null,
      note: null,
      has_body: Boolean(state.config.body),
      scan_findings: [],
      changed_by_hash: 'c0ffee1234abcd',
      created_at: new Date().toISOString(),
      ...extra,
    });
    state.versionBodies[versionNum] = state.config.body ?? null;
    return versionNum;
  };

  await page.route('**/api/v1/agent-models', async (route) => {
    if (options.modelsError) {
      await route.fulfill({
        status: options.modelsError.status,
        contentType: 'application/json',
        body: problemBody(options.modelsError),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ models }),
    });
  });

  await page.route('**/api/v1/agents/**', async (route, request) => {
    const url = new URL(request.url());
    const segments = url.pathname.split('/').filter(Boolean);
    // ['api','v1','agents', <name>, ...]
    const tail = segments.slice(4).join('/');
    const method = request.method();

    if (!tail.startsWith('config')) {
      await route.fallback();
      return;
    }

    const json = async (body: unknown, status = 200) => {
      await route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    };
    const problem = async (p: ProblemMock) => {
      await route.fulfill({
        status: p.status,
        contentType: 'application/json',
        body: problemBody(p),
      });
    };
    const writeEcho = (versionNum: number, findings: ScanFinding[] = []) => ({
      success: true,
      version_num: versionNum,
      current_version: state.config.current_version,
      etag: state.config.etag,
      prompt_source: state.config.prompt_source,
      model_source: state.config.model_source,
      delivery_state: state.config.delivery_state,
      scan_findings: findings,
    });

    if (tail === 'config' && method === 'GET') {
      state.configReads += 1;
      if (options.configError) {
        await problem(options.configError);
        return;
      }
      await json(state.config);
      return;
    }

    if (tail === 'config' && method === 'PUT') {
      const body = (request.postDataJSON() ?? {}) as SetAgentConfigRequest;
      state.saves.push(body);
      if (options.saveError && state.failSaves) {
        await problem(options.saveError);
        return;
      }
      if (body.body !== undefined && body.body !== null) {
        state.config.body = body.body;
        state.config.prompt_source = state.config.prompt_enabled
          ? 'managed'
          : 'code';
      }
      if (body.model_id !== undefined && body.model_id !== null) {
        state.config.model_id = body.model_id;
        const picked = models.find((model) => model.id === body.model_id);
        state.config.model_provider = picked?.provider ?? null;
        state.config.model_cost_tier = picked?.cost_tier ?? null;
        state.config.model_allowed = Boolean(picked);
        state.config.model_source = picked ? 'managed' : 'code';
      }
      if (body.prompt_enabled !== undefined) {
        state.config.prompt_enabled = body.prompt_enabled;
      }
      const findings = options.saveFindings ?? [];
      const versionNum = appendVersion('updated', {
        note: body.note ?? null,
        origin: body.origin ?? 'authored',
        scan_findings: findings,
      });
      await json(writeEcho(versionNum, findings));
      return;
    }

    if (tail === 'config' && method === 'PATCH') {
      const body = (request.postDataJSON() ?? {}) as SetPromptEnabledRequest;
      state.enablePatches.push(body);
      if (options.enableError) {
        await problem(options.enableError);
        return;
      }
      state.config.prompt_enabled = body.prompt_enabled;
      state.config.prompt_source = body.prompt_enabled ? 'managed' : 'code';
      state.config.delivery_state = body.prompt_enabled ? 'active' : 'disabled';
      const versionNum = appendVersion(
        body.prompt_enabled ? 'enabled' : 'disabled'
      );
      await json(writeEcho(versionNum));
      return;
    }

    if (tail === 'config:clear-prompt' || tail === 'config:clear-model') {
      const field = tail.endsWith('prompt') ? 'prompt' : 'model';
      const body = (request.postDataJSON() ??
        {}) as ClearAgentConfigFieldRequest;
      state.clears.push({ field, body });
      if (options.clearError) {
        await problem(options.clearError);
        return;
      }
      const had =
        field === 'prompt'
          ? Boolean(state.config.body)
          : Boolean(state.config.model_id);
      if (!had) {
        // Idempotent, and it writes no version row.
        await json({
          cleared: false,
          version_num: null,
          current_version: state.config.current_version,
          etag: state.config.etag,
          prompt_source: state.config.prompt_source,
          model_source: state.config.model_source,
          delivery_state: state.config.delivery_state,
        });
        return;
      }
      if (field === 'prompt') {
        state.config.body = null;
        state.config.prompt_enabled = false;
        state.config.prompt_source = 'none';
      } else {
        state.config.model_id = null;
        state.config.model_provider = null;
        state.config.model_cost_tier = null;
        state.config.model_allowed = true;
        state.config.model_source = 'code';
      }
      const versionNum = appendVersion(
        field === 'prompt' ? 'prompt_cleared' : 'model_cleared',
        { note: body.note ?? null }
      );
      await json({
        cleared: true,
        version_num: versionNum,
        current_version: state.config.current_version,
        etag: state.config.etag,
        prompt_source: state.config.prompt_source,
        model_source: state.config.model_source,
        delivery_state: state.config.delivery_state,
      });
      return;
    }

    if (tail === 'config/versions' && method === 'GET') {
      if (options.versionsError) {
        await problem(options.versionsError);
        return;
      }
      await json({
        versions: state.versions,
        pagination: {
          total: state.versions.length,
          limit: 50,
          has_more: false,
          next_cursor: null,
        },
      });
      return;
    }

    const versionMatch = tail.match(/^config\/versions\/(\d+)(:restore)?$/);
    if (versionMatch) {
      const versionNum = Number(versionMatch[1]);
      const summary = state.versions.find((v) => v.version_num === versionNum);

      if (!summary) {
        await problem({
          status: 404,
          errorCode: 'AGENT_CONFIG_NOT_FOUND',
          title: 'Not Found',
          detail: 'That version does not exist for this agent.',
        });
        return;
      }

      if (versionMatch[2]) {
        const body = (request.postDataJSON() ??
          {}) as RestoreAgentConfigVersionRequest;
        state.restores.push({ versionNum, body });
        if (options.restoreError) {
          await problem(options.restoreError);
          return;
        }
        state.config.body = state.versionBodies[versionNum] ?? null;
        state.config.model_id = summary.model_id ?? null;
        const restored = appendVersion('restored', {
          origin: 'restored',
          note: body.note ?? null,
          model_id: summary.model_id ?? null,
        });
        await json(writeEcho(restored));
        return;
      }

      await json({
        version: {
          ...summary,
          body: state.versionBodies[versionNum] ?? null,
          body_format: 'text',
          etag: `v${versionNum}-1a2b3c4d5e6f`,
        },
      });
      return;
    }

    await route.fallback();
  });

  return state;
}

/**
 * Typed mock data for tests
 */
export const mockData = {
  chatSessions,
  chatTranscripts,
  agents: agentsResponse,
  teams: teamsResponse,
  emptyTeams: emptyTeamsResponse,
  crowdedTeams: crowdedTeamsResponse,
  crowdedTeam,
  crowdedMemberNames,
  teamMemberNames,
  teamDetails,
  teamAgents,
  milestones: milestonesList,
  milestoneStates,
  agent: agentResponse,
  agentWithSteps: agentWithStepsResponse,
  controls: controlsResponse,
  controlsWithTemplate: controlsWithTemplateResponse,
  controlsWithUnrenderedTemplate: controlsWithUnrenderedTemplateResponse,
  templateControl: templateBackedControl,
  unrenderedTemplateControl,
  listControls: listControlsResponse,
  templateControlSummary: templateControlSummary,
  evaluators: evaluatorsResponse,
  controlSchema: controlSchemaResponse,
  stats: statsResponse,
  emptyStats: emptyStatsResponse,
  agentConfig: agentConfig(),
  agentConfigVersions: agentConfigVersionsList,
  agentConfigVersionBodies,
  agentModels: agentModelOptions,
  storedPromptBody,
} as const;

/**
 * Response options for route mocking
 */
type MockResponseOptions<T> =
  | { data: T; status?: number }
  | { error: string; status: number }
  | { handler: () => T | Promise<T> };

/**
 * Helper to fulfill a route with consistent formatting
 */
async function fulfillRoute<T>(
  route: Parameters<Parameters<Page['route']>[1]>[0],
  options: MockResponseOptions<T>,
  defaultData: T
) {
  if ('error' in options) {
    await route.fulfill({
      status: options.status,
      contentType: 'application/json',
      body: JSON.stringify({ error: options.error }),
    });
  } else if ('handler' in options) {
    const data = await options.handler();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(data),
    });
  } else {
    await route.fulfill({
      status: options.status ?? 200,
      contentType: 'application/json',
      body: JSON.stringify(options.data ?? defaultData),
    });
  }
}

/** Server config response (auth) - used so AuthProvider does not require a real backend in tests */
export const serverConfigResponse = {
  requires_api_key: false,
  auth_mode: 'none' as const,
  has_active_session: false,
};

export type ServerConfigMock = {
  requires_api_key: boolean;
  auth_mode: 'none' | 'api-key';
  has_active_session: boolean;
};

/**
 * Individual route mock helpers - can be used standalone or with custom data
 */
export const mockRoutes = {
  /** Mock GET /api/config (auth) - must be hit before app content renders */
  config: async (
    page: Page,
    options: MockResponseOptions<ServerConfigMock> = {
      data: serverConfigResponse,
    }
  ) => {
    const data = {
      ...serverConfigResponse,
      ...('data' in options ? options.data : {}),
    };
    await page.route('**/api/config', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    });
  },

  /** Mock POST /api/login (auth) - optional success/failure for API key flow tests */
  login: async (
    page: Page,
    options: { authenticated: boolean; is_admin?: boolean } = {
      authenticated: true,
      is_admin: false,
    }
  ) => {
    await page.route('**/api/login', async (route) => {
      if (route.request().method() !== 'POST') {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          authenticated: options.authenticated,
          is_admin: options.is_admin ?? false,
        }),
      });
    });
  },

  /** Mock GET /api/v1/agents */
  agents: async (
    page: Page,
    options: MockResponseOptions<ListAgentsResponse> = { data: mockData.agents }
  ) => {
    await page.route('**/api/v1/agents?**', async (route, request) => {
      // Helper to filter agents based on query params (server-side search)
      const getFilteredResponse = (url: string): ListAgentsResponse => {
        const urlObj = new URL(url);
        const nameFilter = urlObj.searchParams.get('name');
        const teamFilter = urlObj.searchParams.get('team');

        // `?team=` narrows to that team's membership. An unknown slug matches
        // nobody, which is a 200 with an empty page on the real server too.
        let agents = teamFilter
          ? (teamAgents[teamFilter] ?? [])
          : mockData.agents.agents;

        // Apply name filter (case-insensitive partial match, like the real API)
        if (nameFilter) {
          const lowerFilter = nameFilter.toLowerCase();
          agents = agents.filter((a) =>
            a.agent_name.toLowerCase().includes(lowerFilter)
          );
        }

        // Cursor paging, so the team panel's infinite scroll can be exercised.
        // The cursor is the index of the next row.
        const limit = Number(urlObj.searchParams.get('limit')) || 10;
        const offset = Number(urlObj.searchParams.get('cursor')) || 0;
        const total = agents.length;
        const pageRows = agents.slice(offset, offset + limit);
        const nextOffset = offset + pageRows.length;
        const hasMore = nextOffset < total;

        return {
          agents: pageRows,
          pagination: {
            total,
            limit,
            has_more: hasMore,
            next_cursor: hasMore ? String(nextOffset) : null,
          },
        };
      };

      const url = request.url();
      const filteredData = getFilteredResponse(url);

      await fulfillRoute(
        route,
        { ...options, data: filteredData },
        filteredData
      );
    });
  },

  /** Mock GET /api/v1/agents/:id and /api/v1/agents/:id/controls */
  agent: async (
    page: Page,
    options: {
      agent?: MockResponseOptions<GetAgentResponse>;
      controls?: MockResponseOptions<AgentControlsResponse>;
    } = {}
  ) => {
    const controlsOpts = options.controls ?? { data: mockData.controls };
    const agentOpts = options.agent ?? { data: mockData.agent };

    // Register controls route first (more specific pattern)
    await page.route('**/api/v1/agents/*/controls', async (route) => {
      await fulfillRoute(route, controlsOpts, mockData.controls);
    });

    // Register agent route second
    await page.route('**/api/v1/agents/*', async (route, request) => {
      const url = request.url();
      // Skip if it's a controls request (handled by separate route above)
      if (url.includes('/controls')) {
        await route.continue();
        return;
      }
      await fulfillRoute(route, agentOpts, mockData.agent);
    });
  },

  /** Mock GET /api/v1/evaluators */
  evaluators: async (
    page: Page,
    options: MockResponseOptions<EvaluatorsResponse> = {
      data: mockData.evaluators,
    }
  ) => {
    await page.route('**/api/v1/evaluators', async (route) => {
      await fulfillRoute(route, options, mockData.evaluators);
    });
  },

  /** Mock GET /api/v1/controls/schema */
  controlSchema: async (
    page: Page,
    options: MockResponseOptions<GetControlSchemaResponse> = {
      data: mockData.controlSchema,
    }
  ) => {
    await page.route('**/api/v1/controls/schema', async (route) => {
      await fulfillRoute(route, options, mockData.controlSchema);
    });
  },

  /** Mock GET /api/v1/controls (list all controls) and PUT /api/v1/controls (create) */
  controlsList: async (
    page: Page,
    options: MockResponseOptions<ListControlsResponse> = {
      data: mockData.listControls,
    }
  ) => {
    // Helper to filter controls based on query params (server-side search)
    const getFilteredResponse = (url: string): ListControlsResponse => {
      const urlObj = new URL(url);
      const nameFilter = urlObj.searchParams.get('name');

      let controls = mockData.listControls.controls;

      // Apply name filter (case-insensitive partial match, like the real API)
      if (nameFilter) {
        const lowerFilter = nameFilter.toLowerCase();
        controls = controls.filter(
          (c) =>
            c.name.toLowerCase().includes(lowerFilter) ||
            (c.description ?? '').toLowerCase().includes(lowerFilter)
        );
      }

      return {
        controls,
        pagination: {
          total: controls.length,
          limit: 20,
          has_more: false,
          next_cursor: null,
        },
      };
    };

    // Handle both with and without query params
    await page.route('**/api/v1/controls?**', async (route, request) => {
      const method = request.method();
      if (method === 'GET') {
        const response = getFilteredResponse(request.url());
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(response),
        });
        return;
      }
      await route.continue();
    });
    // Handle base path for GET (list) and PUT (create)
    await page.route('**/api/v1/controls', async (route, request) => {
      const method = request.method();
      if (method === 'GET') {
        await fulfillRoute(route, options, mockData.listControls);
        return;
      }
      if (method === 'PUT') {
        const body = await request.postDataJSON();
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            control_id: 100,
            name: body.name || 'New Control',
          }),
        });
        return;
      }
      await route.continue();
    });
  },

  /** @deprecated Use controlsList which now handles both GET and PUT */
  controlCreate: async (_page: Page) => {
    // No-op - handled by controlsList
  },

  /** Mock GET and PUT /api/v1/controls/:id/data */
  controlGetData: async (page: Page) => {
    await page.route('**/api/v1/controls/*/data', async (route, request) => {
      const method = request.method();
      if (method === 'GET') {
        // Extract control ID from URL
        const url = request.url();
        const match = url.match(/\/controls\/(\d+)\/data/);
        const controlId = match ? parseInt(match[1], 10) : 1;

        // Find matching control from mock data
        const control =
          controlsList.find((c) => c.id === controlId) ?? controlsList[0];

        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            data: control.control,
          }),
        });
        return;
      }
      if (method === 'PUT') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ success: true }),
        });
        return;
      }
      await route.continue();
    });
  },

  /** Mock POST /api/v1/controls/validate */
  controlValidate: async (
    page: Page,
    options: MockResponseOptions<{ success: boolean }> = {
      data: { success: true },
    }
  ) => {
    await page.route('**/api/v1/controls/validate', async (route) => {
      await fulfillRoute(route, options, { success: true });
    });
  },

  /** @deprecated Use controlGetData which now handles both GET and PUT */
  controlUpdate: async (_page: Page) => {
    // No-op - handled by controlGetData
  },

  /** Mock POST /api/v1/control-templates/render */
  controlRenderTemplate: async (page: Page) => {
    await page.route('**/api/v1/control-templates/render', async (route) => {
      const body = await route.request().postDataJSON();
      // Return the template + values back as a rendered control preview
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          control: {
            ...templateBackedControl.control,
            template: body.template,
            template_values: body.template_values,
          },
        }),
      });
    });
  },

  /** Mock PATCH /api/v1/controls/:id */
  controlPatch: async (page: Page) => {
    await page.route('**/api/v1/controls/*', async (route, request) => {
      if (request.method() !== 'PATCH') {
        await route.fallback();
        return;
      }
      const body = await request.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          name: body.name ?? 'Template Regex Guard',
          enabled: body.enabled ?? true,
        }),
      });
    });
  },

  /** Mock POST /api/v1/agents/:name/controls/:id (attach control) */
  agentAddControl: async (page: Page) => {
    await page.route(
      '**/api/v1/agents/*/controls/*',
      async (route, request) => {
        if (request.method() !== 'POST') {
          await route.fallback();
          return;
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ success: true }),
        });
      }
    );
  },

  /**
   * Mock `GET /api/v1/agent-models`, the admin-tier probe.
   *
   * The route is gated on `AGENT_CONFIGS_WRITE`, so the UI reads a 403 here as
   * "this credential is not an admin" and disables admin-only controls. Pass
   * `admin: false` to be that read-only session. Answering 200 by default
   * keeps every other test on the admin path it already assumed.
   *
   * Distinct from the fuller `agentConfig` mock, which also serves this path:
   * that one spans `**\/api\/v1\/agents\/**` and is kept out of the default
   * set for the timing reason documented on it. This is one exact path.
   */
  adminProbe: async (page: Page, options: { admin?: boolean } = {}) => {
    await page.route('**/api/v1/agent-models', async (route) => {
      if (options.admin === false) {
        await route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({
            type: 'about:blank',
            title: 'Forbidden',
            status: 403,
            detail: 'Admin key required',
            error_code: 'FORBIDDEN',
            reason: 'Forbidden',
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ models: agentModelOptions }),
      });
    });
  },

  /**
   * Mock GET /api/v1/teams and GET /api/v1/teams/:slug.
   *
   * `details` supplies the per-team responses the overview fetches for member
   * names; a slug missing from it is answered with a 500 so tests can exercise
   * a failed member read without breaking the list.
   */
  teams: async (
    page: Page,
    options: {
      list?: MockResponseOptions<ListTeamsResponse>;
      details?: Record<string, GetTeamResponse>;
      detailStatus?: number;
    } = {}
  ) => {
    const listOpts = options.list ?? { data: mockData.teams };
    const details = options.details ?? mockData.teamDetails;

    await page.route('**/api/v1/teams?**', async (route) => {
      await fulfillRoute(route, listOpts, mockData.teams);
    });

    await page.route('**/api/v1/teams/*', async (route, request) => {
      const pathname = new URL(request.url()).pathname;
      // `*` spans `/` here, so sub-resources land in this handler too. They
      // belong to their own mocks.
      if (pathname.split('/').length > 5 || request.method() !== 'GET') {
        await route.fallback();
        return;
      }

      const slug = decodeURIComponent(pathname.split('/').pop() ?? '');
      const detail = details[slug];
      if (!detail) {
        // The real server answers with an RFC 7807 ProblemDetail. The client
        // reads `status` off the parsed body, so a bare `{error}` here would
        // let a broken 404 branch pass.
        const status = options.detailStatus ?? 500;
        await route.fulfill({
          status,
          contentType: 'application/json',
          body: JSON.stringify({
            type: 'about:blank',
            title: status === 404 ? 'Not Found' : 'Error',
            status,
            detail: 'Team detail unavailable',
            error_code: status === 404 ? 'TEAM_NOT_FOUND' : 'INTERNAL_ERROR',
            reason: status === 404 ? 'Not found' : 'Server error',
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(detail),
      });
    });
  },

  /**
   * Mock GET /api/v1/teams/:slug/milestones.
   *
   * `state` picks one of the five documented statuses. Every one of them is a
   * 200 on the real server, including `error`: a Linear outage must not reach
   * the client as a failed request.
   */
  teamMilestones: async (
    page: Page,
    options: {
      state?: MilestoneState;
      body?: Partial<ListTeamMilestonesResponse>;
      /** Set to fail the request itself, i.e. Agent Control being down. */
      status?: number;
      /** Resolves before the response is sent, for loading-state assertions. */
      gate?: Promise<void>;
    } = {}
  ) => {
    const state = options.state ?? 'ok';

    await page.route('**/api/v1/teams/*/milestones', async (route, request) => {
      if (options.gate) await options.gate;

      const slug = decodeURIComponent(
        new URL(request.url()).pathname.split('/').slice(-2)[0] ?? ''
      );

      if (options.status && options.status >= 400) {
        await route.fulfill({
          status: options.status,
          contentType: 'application/json',
          body: JSON.stringify({
            type: 'about:blank',
            title: 'Error',
            status: options.status,
            detail: 'Milestones unavailable',
            error_code: 'INTERNAL_ERROR',
            reason: 'Server error',
          }),
        });
        return;
      }

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...milestoneStates[state](slug),
          ...options.body,
        }),
      });
    });
  },

  /**
   * Mock PATCH /api/v1/teams/:slug, the write the Linear link form makes.
   *
   * Records each submitted body so a test can assert what left the browser.
   *
   * Pass the same `details` object given to {@link mockRoutes.teams} to have an
   * accepted write land in it, so the refetch that follows returns the new
   * value. Without it the GET keeps serving the old one and a test cannot tell
   * a refreshed view from a stale one.
   */
  teamPatch: async (
    page: Page,
    options: {
      status?: number;
      errorCode?: string;
      details?: Record<string, GetTeamResponse>;
    } = {}
  ) => {
    const submitted: Array<Record<string, unknown>> = [];

    await page.route('**/api/v1/teams/*', async (route, request) => {
      if (request.method() !== 'PATCH') {
        await route.fallback();
        return;
      }

      const body = (request.postDataJSON() ?? {}) as Record<string, unknown>;
      submitted.push(body);

      const status = options.status ?? 200;
      if (status >= 400) {
        await route.fulfill({
          status,
          contentType: 'application/json',
          body: JSON.stringify({
            type: 'about:blank',
            title: 'Error',
            status,
            detail: 'Not permitted',
            error_code: options.errorCode ?? 'FORBIDDEN',
            reason: 'Forbidden',
          }),
        });
        return;
      }

      const slug = decodeURIComponent(
        new URL(request.url()).pathname.split('/').pop() ?? ''
      );

      const stored = options.details?.[slug];
      if (stored && 'default_agent_name' in body) {
        stored.default_agent_name = (body.default_agent_name ?? null) as
          | string
          | null;
      }

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          slug,
          display_name: mockData.teamDetails[slug]?.display_name ?? slug,
          description: null,
          linear_team_key: body.linear_team_key ?? null,
          default_agent_name: body.default_agent_name ?? null,
        }),
      });
    });

    return submitted;
  },

  /** Mock the agent-session endpoints behind the chat panel. */
  agentSessions: mockAgentSessions,

  /**
   * Mock the agent-config routes and the deployment model allowlist.
   *
   * Deliberately not part of {@link mockApiRoutes}. Its handler spans
   * `**\/api\/v1\/agents\/**` and falls through for everything that is not a
   * config path, so putting it in the default set adds an interception hop to
   * the agent and controls reads on every page in the suite. That is enough to
   * move request timing measurably: with it registered by default, two
   * assertions in `agent-chat-nudge.spec.ts` that read mock state
   * synchronously after a click failed in three full-suite runs out of four,
   * and passed in every run without it. The underlying race is theirs, but
   * there is no reason to arm it here when only this spec opens the tab.
   */
  agentConfig: mockAgentConfig,

  /** Mock GET /api/v1/observability/stats */
  stats: async (
    page: Page,
    options: MockResponseOptions<StatsResponse> = { data: mockData.stats }
  ) => {
    await page.route('**/api/v1/observability/stats**', async (route) => {
      await fulfillRoute(route, options, mockData.stats);
    });
  },
};

/**
 * Helper to set up all API route mocking with defaults
 */
export async function mockApiRoutes(page: Page) {
  await mockRoutes.config(page);
  await mockRoutes.adminProbe(page);
  await mockRoutes.agents(page);
  await mockRoutes.agent(page);
  await mockRoutes.evaluators(page);
  await mockRoutes.controlSchema(page);
  await mockRoutes.controlsList(page);
  await mockRoutes.controlGetData(page);
  await mockRoutes.controlValidate(page);
  await mockRoutes.controlCreate(page);
  await mockRoutes.controlUpdate(page);
  await mockRoutes.stats(page);
  await mockRoutes.teams(page);
  await mockRoutes.teamMilestones(page);
  await mockRoutes.agentSessions(page);
}

/**
 * Set up all API route mocks with auth required (for login flow tests).
 * Call mockRoutes.login(page, ...) in the test for success/failure.
 */
export async function mockApiRoutesWithAuthRequired(page: Page) {
  await mockRoutes.config(page, {
    data: {
      ...serverConfigResponse,
      requires_api_key: true,
      auth_mode: 'api-key',
      has_active_session: false,
    },
  });
  await mockRoutes.adminProbe(page);
  await mockRoutes.agents(page);
  await mockRoutes.agent(page);
  await mockRoutes.evaluators(page);
  await mockRoutes.controlSchema(page);
  await mockRoutes.controlsList(page);
  await mockRoutes.controlGetData(page);
  await mockRoutes.controlValidate(page);
  await mockRoutes.controlCreate(page);
  await mockRoutes.controlUpdate(page);
  await mockRoutes.stats(page);
  await mockRoutes.teams(page);
  await mockRoutes.teamMilestones(page);
  await mockRoutes.agentSessions(page);
}

export {
  focusJsonEditorAt,
  getJsonEditorSuggestions,
  getJsonEditorValue,
  setJsonEditorValue,
} from './json-editor-bridge';

/**
 * Extended test with mocked API
 */
export const test = base.extend<{ mockedPage: Page }>({
  /* eslint-disable react-hooks/rules-of-hooks */
  mockedPage: async ({ page }, use) => {
    await mockApiRoutes(page);
    await use(page);
  },
  /* eslint-enable react-hooks/rules-of-hooks */
});

export { expect } from '@playwright/test';
