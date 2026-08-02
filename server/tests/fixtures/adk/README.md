# Real `adk api_server` payloads (google-adk 2.6.1)

Captured during the Phase 0 spike on 2026-08-02. Findings and verdicts live in
`docs/plans/spike-findings.md`.

**Provenance.** One `adk api_server` process, google-adk 2.6.1, Python 3.12.10,
`--session_service_uri postgresql+asyncpg://adk:***@localhost:15432/adk_runtime`,
serving one agent (`spike_app`) whose model is an OpenAI-compatible endpoint
routed through `LiteLlm`. Every file is a verbatim request/response pair from
that process; nothing here is hand-written or edited except for the removal of
nothing at all. IDs, timestamps and token counts are real.

Each file has the shape:

```json
{ "name": "...", "request": {"method","path","body"},
  "response": {"status","content_type","body"} }
```

except `run_sse_frames.json` (headers plus per-frame arrival offsets),
`openapi_routes_and_schemas.json` (an extract of the server's own
`/openapi.json`), and the three `run_response_halt_*.json` files, which are
whole-experiment records including which plugin callbacks fired.

## What each file settles

| File | Settles |
| --- | --- |
| `create_session_with_id.json` | `POST /apps/{app}/users/{user}/sessions/{id}` takes the state map as the **bare** request body. |
| `create_session_wrapped_state_GUESS.json` | The `{"state": {...}}` wrapper currently in `services/adk_executor_client.py` is accepted with HTTP 200 and silently nests the state one level deep. |
| `get_session_empty.json`, `get_session_after_turn.json` | `Session` response shape; `stateDelta` sent on `POST /run` is merged into session state. |
| `get_session_missing_404.json` | `{"detail": "Session not found"}`. |
| `get_session_dangling_function_call.json` | A session whose last event is a `functionCall` with no `functionResponse`, produced by SIGKILLing the executor mid-tool. |
| `run_response.json` | `POST /run` returns a bare JSON **array** of events; camelCase keys; function-response events carry `content.role == "user"`. |
| `run_response_halt_before_model.json` | Returning an `LlmResponse` from `before_model_callback` ends the invocation (H1). |
| `run_response_halt_before_tool.json` | Returning a dict from `before_tool_callback` prevents the tool body from running (H2), and the invocation continues with one more model call. |
| `run_response_halt_before_tool_skip_summarization.json` | The same block plus `tool_context.actions.skip_summarization = True` ends the invocation with no further model call (A9). |
| `run_missing_session.json`, `run_unknown_app.json`, `run_422.json` | Error bodies. `run_unknown_app.json` leaks a filesystem path in `detail`; do not forward upstream bodies. |
| `run_sse_frames.json` | `POST /run_sse` emits `text/event-stream`, chunked, one `data:` line per event, incrementally, with no heartbeat and no terminal sentinel frame. |
| `health.json`, `list_apps.json` | `GET /health` exists and returns `{"status": "ok"}`. |
| `delete_session.json`, `delete_session_missing.json` | DELETE returns 200 with a `null` body whether or not the session exists. |
| `openapi_routes_and_schemas.json` | Route inventory and the `RunAgentRequest` / `CreateSessionRequest` / `Session` schemas. |
