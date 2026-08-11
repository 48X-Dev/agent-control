#!/bin/bash
set -euo pipefail

# Refuses the environment, or writes /agents/${AGENT_CONTROL_AGENT_NAME}/agent.py.
python /app/examples/google_adk_plugin/my_agent/materialize.py

# ADK binds 127.0.0.1 by default, which no other container could reach. Nothing
# publishes this port, so every interface here means the container network only.
set -- adk api_server --host 0.0.0.0 --port 8000

# --session_service_uri is a click option with no envvar binding, so exporting
# the variable and nothing else leaves ADK on in-memory sessions.
if [ -n "${ADK_SESSION_SERVICE_URI:-}" ]; then
    set -- "$@" --session_service_uri "${ADK_SESSION_SERVICE_URI}"
fi

exec "$@" /agents
