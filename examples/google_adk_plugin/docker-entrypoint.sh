#!/bin/bash
set -euo pipefail

if [ "$#" -gt 0 ]; then
    echo "executor: this image takes no arguments (got: $*)." >&2
    echo "It runs the processes AGENT_CONTROL_FLEET_AGENTS names and nothing else." >&2
    exit 64
fi

# Refuses the environment, or writes /agents/<name>/<name>/agent.py per process.
python /app/examples/google_adk_plugin/my_agent/materialize.py

pids=()

# One `adk api_server` per entry, each given its own agents root: list_agents()
# enumerates directories under the root it is handed, so a root shared across the
# group would make every process advertise every name (topology plan 3.4).
launch() {
    local name="$1" port="$2"

    # ADK binds 127.0.0.1 by default, which no other container could reach.
    # Nothing publishes these ports, so every interface means this network only.
    set -- adk api_server --host 0.0.0.0 --port "$port"

    # --session_service_uri is a click option with no envvar binding, so exporting
    # the variable and nothing else leaves ADK on per-agent SQLite under the tmpfs.
    if [ -n "${ADK_SESSION_SERVICE_URI:-}" ]; then
        set -- "$@" --session_service_uri "${ADK_SESSION_SERVICE_URI}"
    fi

    AGENT_CONTROL_AGENT_NAME="$name" "$@" "/agents/$name" &
    pids+=("$!")
}

IFS=',' read -r -a entries <<< "${AGENT_CONTROL_FLEET_AGENTS}"
for entry in "${entries[@]}"; do
    launch "${entry%%:*}" "${entry##*:}"
done

# No supervisor. The first process to exit takes the container down, because a
# container that is up while an agent inside it is gone is a runtime row that
# reads healthy and answers nothing.
while :; do
    for pid in "${pids[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
            status=0
            wait "$pid" || status=$?
            kill "${pids[@]}" 2>/dev/null || true
            exit "$status"
        fi
    done
    sleep 2
done
