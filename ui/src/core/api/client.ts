import createClient from 'openapi-fetch';

import type { paths } from './generated/api-types';
import type {
  CancelNudgeResponse,
  ClearAgentConfigFieldRequest,
  ClearAgentConfigFieldResponse,
  CreateAgentSessionRequest,
  CreateAgentSessionResponse,
  CreateControlRequest,
  CreateHaltResponse,
  CreateNudgeRequest,
  CreateNudgeResponse,
  GetAgentConfigResponse,
  GetAgentConfigVersionResponse,
  GetAgentControlsPathParams,
  GetAgentPathParams,
  GetAgentSessionResponse,
  GetControlSchemaResponse,
  GetTeamResponse,
  InitAgentRequestBody,
  ListAgentConfigVersionsQueryParams,
  ListAgentConfigVersionsResponse,
  ListAgentModelsResponse,
  ListAgentSessionsQueryParams,
  ListAgentSessionsResponse,
  ListAgentsQueryParams,
  ListAgentsResponse,
  ListHaltsResponse,
  ListNudgesResponse,
  ListSessionMessagesQueryParams,
  ListSessionMessagesResponse,
  ListTeamAgentsQueryParams,
  ListTeamMilestonesResponse,
  ListTeamsQueryParams,
  ListTeamsResponse,
  PatchControlRequest,
  PatchTeamRequest,
  PatchTeamResponse,
  PlanResponse,
  RenderControlTemplateRequest,
  RenderControlTemplateResponse,
  RestoreAgentConfigVersionRequest,
  SetAgentConfigRequest,
  SetAgentConfigResponse,
  SetControlDataRequest,
  SetPromptEnabledRequest,
  StartTurnRequest,
  TurnResponse,
  ValidateControlDataRequest,
  ValidateControlDataResponse,
} from './types';

const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL?.trim();
const isStaticExport = process.env.NEXT_PUBLIC_STATIC_EXPORT === 'true';
const API_URL =
  configuredApiUrl ?? (isStaticExport ? '' : 'http://localhost:8000');

export const apiClient = createClient<paths>({
  baseUrl: API_URL,
  credentials: 'include',
  headers: {
    'Content-Type': 'application/json',
  },
});

/**
 * Where the record of one turn lives, as a URL a person can open.
 *
 * Built here rather than in a component because the API base is this module's
 * business. It is a plain link to the trace endpoint: the hops of the turn as
 * this control plane recorded them, which is the independent evidence beside
 * anything an agent says about its own work. There is no trace page in this UI
 * yet, so it opens the API response itself, and the copy around it says so
 * instead of dressing it up as a viewer.
 */
export function traceUrl(traceId: string): string {
  return `${API_URL}/api/v1/observability/traces/${encodeURIComponent(traceId)}`;
}

// Global 401 listener — UI components can subscribe to be notified when
// a request is rejected due to missing/expired credentials.
type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

function notifyUnauthorized(): void {
  unauthorizedListeners.forEach((fn) => fn());
}

apiClient.use({
  async onResponse({ response }) {
    if (response.status === 401) {
      notifyUnauthorized();
    }
    return response;
  },
});

// ------------------------------------------------------------------
// Manual JSON requests
//
// Used by endpoints that are not yet part of the generated OpenAPI types.
// Mirrors the shape openapi-fetch returns so callers stay uniform.
// ------------------------------------------------------------------

type JsonResult<T> = {
  data?: T;
  error?: unknown;
  response: Response;
};

async function getJson<T>(path: string): Promise<JsonResult<T>> {
  const res = await fetch(`${API_URL}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
  });

  if (res.status === 401) {
    notifyUnauthorized();
  }

  const body = await res.json().catch(() => undefined);

  if (!res.ok) {
    return {
      data: undefined,
      error: body ?? { status: res.status, title: res.statusText },
      response: res,
    };
  }

  return { data: body as T, error: undefined, response: res };
}

/**
 * POST with a JSON body.
 *
 * Takes an `AbortSignal` because one caller needs it: a chat turn is a
 * blocking request that can run for a minute, and the panel offers to stop
 * waiting for it. Aborting abandons the response, not the turn.
 */
async function postJson<T>(
  path: string,
  body: unknown,
  options?: { signal?: AbortSignal }
): Promise<JsonResult<T>> {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options?.signal,
  });

  if (res.status === 401) {
    notifyUnauthorized();
  }

  const responseBody = await res.json().catch(() => undefined);

  if (!res.ok) {
    return {
      data: undefined,
      error: responseBody ?? { status: res.status, title: res.statusText },
      response: res,
    };
  }

  return { data: responseBody as T, error: undefined, response: res };
}

async function putJson<T>(path: string, body: unknown): Promise<JsonResult<T>> {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (res.status === 401) {
    notifyUnauthorized();
  }

  const responseBody = await res.json().catch(() => undefined);

  if (!res.ok) {
    return {
      data: undefined,
      error: responseBody ?? { status: res.status, title: res.statusText },
      response: res,
    };
  }

  return { data: responseBody as T, error: undefined, response: res };
}

async function deleteJson<T>(path: string): Promise<JsonResult<T>> {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'DELETE',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
  });

  if (res.status === 401) {
    notifyUnauthorized();
  }

  const responseBody = await res.json().catch(() => undefined);

  if (!res.ok) {
    return {
      data: undefined,
      error: responseBody ?? { status: res.status, title: res.statusText },
      response: res,
    };
  }

  return { data: responseBody as T, error: undefined, response: res };
}

async function patchJson<T>(
  path: string,
  body: unknown
): Promise<JsonResult<T>> {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (res.status === 401) {
    notifyUnauthorized();
  }

  const responseBody = await res.json().catch(() => undefined);

  if (!res.ok) {
    return {
      data: undefined,
      error: responseBody ?? { status: res.status, title: res.statusText },
      response: res,
    };
  }

  return { data: responseBody as T, error: undefined, response: res };
}

function toQueryString(params?: Record<string, string | number | undefined>) {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : '';
}

// ------------------------------------------------------------------
// Auth API (not part of the generated OpenAPI types)
// ------------------------------------------------------------------

export type ServerConfig = {
  requires_api_key: boolean;
  auth_mode: 'none' | 'api-key';
  has_active_session: boolean;
};

export type LoginResponse = {
  authenticated: boolean;
  is_admin: boolean;
};

const authBaseUrl = API_URL || '';

export const authApi = {
  getConfig: async (): Promise<ServerConfig> => {
    const res = await fetch(`${authBaseUrl}/api/config`, {
      credentials: 'include',
    });
    if (!res.ok) throw new Error('Failed to fetch server config');
    return res.json() as Promise<ServerConfig>;
  },

  login: async (
    apiKey: string
  ): Promise<{ status: number; data: LoginResponse }> => {
    const res = await fetch(`${authBaseUrl}/api/login`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey }),
    });
    const data = (await res.json()) as LoginResponse;
    return { status: res.status, data };
  },

  logout: async (): Promise<void> => {
    await fetch(`${authBaseUrl}/api/logout`, {
      method: 'POST',
      credentials: 'include',
    });
  },
};

// ------------------------------------------------------------------
// Typed API methods
// ------------------------------------------------------------------

export const api = {
  agents: {
    list: (params?: ListAgentsQueryParams) =>
      apiClient.GET('/api/v1/agents', {
        params: { query: params },
      }),
    // Separate from `list` because the generated query type predates the
    // `team` filter. Same endpoint, same response shape.
    listByTeam: (params: ListTeamAgentsQueryParams) =>
      getJson<ListAgentsResponse>(`/api/v1/agents${toQueryString(params)}`),
    get: (agentName: GetAgentPathParams['agent_name']) =>
      apiClient.GET('/api/v1/agents/{agent_name}', {
        params: { path: { agent_name: agentName } },
      }),
    initAgent: (data: InitAgentRequestBody) =>
      apiClient.POST('/api/v1/agents/initAgent', { body: data }),
    getControls: (agentName: GetAgentControlsPathParams['agent_name']) =>
      apiClient.GET('/api/v1/agents/{agent_name}/controls', {
        params: { path: { agent_name: agentName } },
      }),
    getPolicies: (agentName: GetAgentPathParams['agent_name']) =>
      apiClient.GET('/api/v1/agents/{agent_name}/policies', {
        params: { path: { agent_name: agentName } },
      }),
    addPolicy: (
      agentName: GetAgentPathParams['agent_name'],
      policyId: number
    ) =>
      apiClient.POST('/api/v1/agents/{agent_name}/policies/{policy_id}', {
        params: { path: { agent_name: agentName, policy_id: policyId } },
      }),
    removePolicy: (
      agentName: GetAgentPathParams['agent_name'],
      policyId: number
    ) =>
      apiClient.DELETE('/api/v1/agents/{agent_name}/policies/{policy_id}', {
        params: { path: { agent_name: agentName, policy_id: policyId } },
      }),
    clearPolicies: (agentName: GetAgentPathParams['agent_name']) =>
      apiClient.DELETE('/api/v1/agents/{agent_name}/policies', {
        params: { path: { agent_name: agentName } },
      }),
    addControl: (
      agentName: GetAgentPathParams['agent_name'],
      controlId: number
    ) =>
      apiClient.POST('/api/v1/agents/{agent_name}/controls/{control_id}', {
        params: { path: { agent_name: agentName, control_id: controlId } },
      }),
    removeControl: (
      agentName: GetAgentPathParams['agent_name'],
      controlId: number
    ) =>
      apiClient.DELETE('/api/v1/agents/{agent_name}/controls/{control_id}', {
        params: { path: { agent_name: agentName, control_id: controlId } },
      }),
  },
  evaluators: {
    list: () => apiClient.GET('/api/v1/evaluators'),
  },
  controls: {
    list: (params?: {
      cursor?: number;
      limit?: number;
      name?: string;
      enabled?: boolean;
      step_type?: string;
      stage?: string;
      execution?: string;
      tag?: string;
    }) =>
      apiClient.GET('/api/v1/controls', {
        params: params ? { query: params } : undefined,
      }),
    create: (data: CreateControlRequest) =>
      apiClient.PUT('/api/v1/controls', { body: data }),
    getSchema: () =>
      apiClient.GET('/api/v1/controls/schema') as Promise<{
        data?: GetControlSchemaResponse;
        error?: unknown;
        response: Response;
      }>,
    getData: (controlId: number) =>
      apiClient.GET('/api/v1/controls/{control_id}/data', {
        params: { path: { control_id: controlId } },
      }),
    updateMetadata: (controlId: number, data: PatchControlRequest) =>
      apiClient.PATCH('/api/v1/controls/{control_id}', {
        params: { path: { control_id: controlId } },
        body: data,
      }),
    setData: (controlId: number, data: SetControlDataRequest) =>
      apiClient.PUT('/api/v1/controls/{control_id}/data', {
        params: { path: { control_id: controlId } },
        body: data,
      }),
    validateData: ({
      data,
      signal,
    }: {
      data: ValidateControlDataRequest['data'];
      signal?: AbortSignal;
    }) =>
      apiClient.POST('/api/v1/controls/validate', {
        body: { data },
        signal,
      }) as Promise<{
        data?: ValidateControlDataResponse;
        error?: unknown;
        response: Response;
      }>,
    delete: (controlId: number, options?: { force?: boolean }) =>
      apiClient.DELETE('/api/v1/controls/{control_id}', {
        params: {
          path: { control_id: controlId },
          query:
            options?.force !== undefined ? { force: options.force } : undefined,
        },
      }),
  },
  controlTemplates: {
    render: async (data: RenderControlTemplateRequest) => {
      const res = await fetch(`${API_URL}/api/v1/control-templates/render`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      const body = await res.json();
      if (!res.ok) return { data: undefined, error: body, response: res };
      return {
        data: body as RenderControlTemplateResponse,
        error: undefined,
        response: res,
      };
    },
  },
  policies: {
    create: (name: string) =>
      apiClient.PUT('/api/v1/policies', { body: { name } }),
    addControl: (policyId: number, controlId: number) =>
      apiClient.POST('/api/v1/policies/{policy_id}/controls/{control_id}', {
        params: { path: { policy_id: policyId, control_id: controlId } },
      }),
    removeControl: (policyId: number, controlId: number) =>
      apiClient.DELETE('/api/v1/policies/{policy_id}/controls/{control_id}', {
        params: { path: { policy_id: policyId, control_id: controlId } },
      }),
  },
  teams: {
    list: (params?: ListTeamsQueryParams) =>
      getJson<ListTeamsResponse>(`/api/v1/teams${toQueryString(params)}`),
    get: (slug: string) =>
      getJson<GetTeamResponse>(`/api/v1/teams/${encodeURIComponent(slug)}`),
    getMilestones: (slug: string) =>
      getJson<ListTeamMilestonesResponse>(
        `/api/v1/teams/${encodeURIComponent(slug)}/milestones`
      ),
    patch: (slug: string, body: PatchTeamRequest) =>
      patchJson<PatchTeamResponse>(
        `/api/v1/teams/${encodeURIComponent(slug)}`,
        body
      ),
  },
  agentSessions: {
    list: (params?: ListAgentSessionsQueryParams) =>
      getJson<ListAgentSessionsResponse>(
        `/api/v1/agent-sessions${toQueryString(params)}`
      ),
    get: (sessionKey: string) =>
      getJson<GetAgentSessionResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}`
      ),
    create: (body: CreateAgentSessionRequest) =>
      postJson<CreateAgentSessionResponse>('/api/v1/agent-sessions', body),
    messages: (sessionKey: string, params?: ListSessionMessagesQueryParams) =>
      getJson<ListSessionMessagesResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/messages${toQueryString(params)}`
      ),
    // Blocking: resolves when the agent has finished answering, which is
    // routinely tens of seconds. The signal is how the panel stops waiting.
    startTurn: (
      sessionKey: string,
      body: StartTurnRequest,
      options?: { signal?: AbortSignal }
    ) =>
      postJson<TurnResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/turns`,
        body,
        options
      ),
    listNudges: (sessionKey: string) =>
      getJson<ListNudgesResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/nudges`
      ),
    createNudge: (sessionKey: string, body: CreateNudgeRequest) =>
      postJson<CreateNudgeResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/nudges`,
        body
      ),
    cancelNudge: (sessionKey: string, nudgeId: number) =>
      deleteJson<CancelNudgeResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/nudges/${nudgeId}`
      ),
    listHalts: (sessionKey: string) =>
      getJson<ListHaltsResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/halts`
      ),
    // The body is empty and stays empty: a stop carries no operator text, so
    // it can never become an unevaluated instruction channel into the model.
    createHalt: (sessionKey: string) =>
      postJson<CreateHaltResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/halts`,
        {}
      ),
    // Read-only from this client. Plans are written by the agent under a
    // session-bound runtime token, never from a browser: what a person sees is
    // the agent's account, and a console that could edit it would be showing
    // something other than what the agent said.
    getPlan: (sessionKey: string) =>
      getJson<PlanResponse>(
        `/api/v1/agent-sessions/${encodeURIComponent(sessionKey)}/plan`
      ),
  },
  // One agent's system prompt and model. Reads are AUTHENTICATED; every write
  // here is ADMIN on the server, so a non-admin key gets a 403 rather than a
  // silent no-op, and the callers surface it.
  agentConfigs: {
    get: (agentName: string) =>
      getJson<GetAgentConfigResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config`
      ),
    set: (agentName: string, body: SetAgentConfigRequest) =>
      putJson<SetAgentConfigResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config`,
        body
      ),
    // POST with a verb suffix rather than DELETE: the call carries
    // `expected_version`, and bodies on DELETE get dropped by some proxies.
    clearPrompt: (agentName: string, body: ClearAgentConfigFieldRequest) =>
      postJson<ClearAgentConfigFieldResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config:clear-prompt`,
        body
      ),
    clearModel: (agentName: string, body: ClearAgentConfigFieldRequest) =>
      postJson<ClearAgentConfigFieldResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config:clear-model`,
        body
      ),
    setPromptEnabled: (agentName: string, body: SetPromptEnabledRequest) =>
      patchJson<SetAgentConfigResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config`,
        body
      ),
    listVersions: (
      agentName: string,
      params?: ListAgentConfigVersionsQueryParams
    ) =>
      getJson<ListAgentConfigVersionsResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config/versions${toQueryString(params)}`
      ),
    getVersion: (agentName: string, versionNum: number) =>
      getJson<GetAgentConfigVersionResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config/versions/${versionNum}`
      ),
    restoreVersion: (
      agentName: string,
      versionNum: number,
      body: RestoreAgentConfigVersionRequest
    ) =>
      postJson<SetAgentConfigResponse>(
        `/api/v1/agents/${encodeURIComponent(agentName)}/config/versions/${versionNum}:restore`,
        body
      ),
  },
  // The deployment's model allowlist. Deployment-wide, namespace-independent,
  // and gated on the *write* operation, so a 403 here is also the honest
  // answer to "may this caller save?".
  agentModels: {
    list: () => getJson<ListAgentModelsResponse>('/api/v1/agent-models'),
  },
  observability: {
    getStats: (params: {
      agent_name: string;
      time_range?:
        | '1m'
        | '5m'
        | '15m'
        | '1h'
        | '24h'
        | '7d'
        | '30d'
        | '180d'
        | '365d';
      include_timeseries?: boolean;
    }) =>
      apiClient.GET('/api/v1/observability/stats', {
        params: { query: params },
      }),
  },
};
