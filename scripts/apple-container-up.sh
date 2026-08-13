#!/usr/bin/env bash
# Start the Agent Control stack under Apple's `container` runtime.
#
# This is the docker-compose.yml translated by hand, because `container` has no
# compose. Same images (built into container's own store - Docker's store is a
# different daemon), same .env, same ports, so the two runtimes are swappable
# but NOT concurrent: both want 8000 and 15432. Stop one before starting the
# other (`docker compose down` / scripts/apple-container-down.sh).
#
# Wiring is by container IP, not by name: inter-container DNS on user-created
# networks is not something this script assumes, and an IP read back from
# `container inspect` after each start works regardless. The cost is ordering -
# postgres first, then the server pointed at its IP, then the dispatcher
# pointed at the server's.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env at repo root; the stack is configured there" >&2; exit 1; }

# Read .env the way compose does - literally - NOT with `source`. Sourcing runs
# the shell's quote removal over every value, and the models allowlist is a
# JSON array full of double quotes: sourced, it exports as [{id:gpt...}], the
# server's settings parser rejects it, and the container dies before migrations.
while IFS= read -r line; do
  case "$line" in ''|\#*) continue;; esac
  export "${line%%=*}=${line#*=}"
done < .env

NETWORK=agent-control
PG_NAME=ac-postgres
SERVER_NAME=ac-server
DISPATCHER_NAME=ac-dispatcher
KNOWLEDGE_NAME=ac-knowledge

container network inspect "$NETWORK" >/dev/null 2>&1 || container network create "$NETWORK"
container volume inspect ac-pgdata >/dev/null 2>&1 || container volume create ac-pgdata

# Read from the shape `container inspect` actually emits (verified against a
# live container on 1.2.0): status.networks[0].ipv4Address, CIDR-suffixed.
ip_of() {
  container inspect "$1" | python3 -c '
import json,sys
d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d
nets=(d.get("status") or {}).get("networks") or []
addr=nets[0].get("ipv4Address","") if nets else ""
print(addr.split("/")[0])'
}

# The same JSON names the gateway, which from inside a VM is the HOST - the
# address executor runtime rows must use in place of host.docker.internal.
gateway_of() {
  container inspect "$1" | python3 -c '
import json,sys
d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d
nets=(d.get("status") or {}).get("networks") or []
print(nets[0].get("ipv4Gateway","") if nets else "")'
}

# Reuse a running container rather than tripping over our own name: the down
# script is how a container is meant to go away, and a re-run of up should be
# able to finish a partial start instead of failing on the survivors.
running() {
  container ls | awk '{print $1}' | grep -qx "$1"
}

echo "== postgres"
# PGDATA is a subdirectory because Apple's volume mounts carry a lost+found at
# the mount root, and initdb refuses a non-empty data directory outright.
if ! running "$PG_NAME"; then
container rm "$PG_NAME" >/dev/null 2>&1 || true
container run -d --name "$PG_NAME" --network "$NETWORK" -a arm64 \
  -p "${AGENT_CONTROL_DB_HOST_PORT:-5432}:5432" \
  -v ac-pgdata:/var/lib/postgresql/data \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -e POSTGRES_DB=agent_control \
  -e POSTGRES_USER=agent_control \
  -e "POSTGRES_PASSWORD=${AGENT_CONTROL_POSTGRES_PASSWORD:-agent_control}" \
  postgres:16-alpine >/dev/null
fi
for i in $(seq 1 30); do
  container exec "$PG_NAME" pg_isready -U agent_control -d agent_control >/dev/null 2>&1 && break
  sleep 2
done
container exec "$PG_NAME" pg_isready -U agent_control -d agent_control >/dev/null
# The provisioning docker-compose.dev.yml runs as a one-shot job: the adk role
# and the REVOKEs that close the control database to PUBLIC. It is not in the
# schema migrations on purpose (see the SQL's own header) and pg_dump does not
# carry database-level privileges, so both a fresh volume and a restored dump
# arrive without it - test_adk_db_isolation is what catches that. Idempotent;
# re-running on every up is the intended usage.
container exec -i "$PG_NAME" psql -q -v ON_ERROR_STOP=1 -U agent_control -d postgres \
  -v adk_password="${ADK_DB_PASSWORD:-adk_local}" \
  -f /dev/stdin < server/scripts/adk_db_init.sql >/dev/null
# The company-knowledge corpus, right after the ADK one and for identical
# reasons: agent_knowledge owned by knowledge_sync, a knowledge_read role with
# SELECT and nothing else, and the control plane closed to both. Parity between
# the two runtimes is mandatory, not aspirational - a service that exists only
# in compose does not exist on the machine this stack actually runs on.
container exec -i "$PG_NAME" psql -q -v ON_ERROR_STOP=1 -U agent_control -d postgres \
  -v knowledge_sync_password="${KNOWLEDGE_DB_PASSWORD:-knowledge_local}" \
  -v knowledge_read_password="${KNOWLEDGE_READ_DB_PASSWORD:-knowledge_read_local}" \
  -f /dev/stdin < server/scripts/knowledge_db_init.sql >/dev/null
PG_IP=$(ip_of "$PG_NAME")
echo "   up at $PG_IP (hardened)"

echo "== server"
if running "$SERVER_NAME"; then echo "   already running"; else
container rm "$SERVER_NAME" >/dev/null 2>&1 || true
container run -d --name "$SERVER_NAME" --network "$NETWORK" -a arm64 \
  -p "${AGENT_CONTROL_SERVER_HOST_PORT:-8000}:8000" \
  -e "AGENT_CONTROL_DB_URL=postgresql+psycopg://agent_control:${AGENT_CONTROL_POSTGRES_PASSWORD:-agent_control}@${PG_IP}:5432/agent_control" \
  -e AGENT_CONTROL_HOST=0.0.0.0 \
  -e AGENT_CONTROL_PORT=8000 \
  -e "AGENT_CONTROL_API_KEY_ENABLED=${AGENT_CONTROL_API_KEY_ENABLED:-false}" \
  -e "AGENT_CONTROL_API_KEYS=${AGENT_CONTROL_API_KEYS:-}" \
  -e "AGENT_CONTROL_ADMIN_API_KEYS=${AGENT_CONTROL_ADMIN_API_KEYS:-}" \
  -e "AGENT_CONTROL_SESSION_SECRET=${AGENT_CONTROL_SESSION_SECRET:-}" \
  -e "AGENT_CONTROL_RUNTIME_TOKEN_SECRET=${AGENT_CONTROL_RUNTIME_TOKEN_SECRET:-}" \
  -e "AGENT_CONTROL_CORS_ORIGINS=${AGENT_CONTROL_CORS_ORIGINS:-http://localhost:4000}" \
  -e "AGENT_CONTROL_EXECUTOR_ENABLED=${AGENT_CONTROL_EXECUTOR_ENABLED:-false}" \
  -e "AGENT_CONTROL_EXECUTOR_SHARED_SECRET=${AGENT_CONTROL_EXECUTOR_SHARED_SECRET:-}" \
  -e "AGENT_CONTROL_EXECUTOR_TIMEOUT_SECONDS=${AGENT_CONTROL_EXECUTOR_TIMEOUT_SECONDS:-30}" \
  -e "AGENT_CONTROL_EXECUTOR_TURN_TIMEOUT_SECONDS=${AGENT_CONTROL_EXECUTOR_TURN_TIMEOUT_SECONDS:-300}" \
  -e "AGENT_CONTROL_EXECUTOR_ALLOW_INSECURE_LOCAL_DEV=${AGENT_CONTROL_EXECUTOR_ALLOW_INSECURE_LOCAL_DEV:-false}" \
  -e "AGENT_CONTROL_MODELS_ALLOWLIST=${AGENT_CONTROL_MODELS_ALLOWLIST:-[]}" \
  -e "AGENT_CONTROL_EXECUTOR_ATTACHMENTS_ENABLED=${AGENT_CONTROL_EXECUTOR_ATTACHMENTS_ENABLED:-false}" \
  -e "AGENT_CONTROL_EXECUTOR_ATTACHMENT_MAX_BYTES=${AGENT_CONTROL_EXECUTOR_ATTACHMENT_MAX_BYTES:-20971520}" \
  -e "AGENT_CONTROL_EXECUTOR_ATTACHMENT_MAX_PER_TURN=${AGENT_CONTROL_EXECUTOR_ATTACHMENT_MAX_PER_TURN:-3}" \
  -e "AGENT_CONTROL_EXECUTOR_ATTACHMENT_DELIVERY_MAX_CHARS=${AGENT_CONTROL_EXECUTOR_ATTACHMENT_DELIVERY_MAX_CHARS:-48000}" \
  -e "AGENT_CONTROL_LINEAR_ATTACHMENTS_ENABLED=${AGENT_CONTROL_LINEAR_ATTACHMENTS_ENABLED:-false}" \
  -e "AGENT_CONTROL_LINEAR_API_KEY=${AGENT_CONTROL_LINEAR_API_KEY:-}" \
  -e "AGENT_CONTROL_LINEAR_TIMEOUT_SECONDS=${AGENT_CONTROL_LINEAR_TIMEOUT_SECONDS:-10}" \
  -e "AGENT_CONTROL_LINEAR_CACHE_TTL_SECONDS=${AGENT_CONTROL_LINEAR_CACHE_TTL_SECONDS:-60}" \
  -e "AGENT_CONTROL_LINEAR_WRITE_ENABLED=${AGENT_CONTROL_LINEAR_WRITE_ENABLED:-false}" \
  -e "AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED=${AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED:-false}" \
  -e "AGENT_CONTROL_LINEAR_CONSOLE_BASE_URL=${AGENT_CONTROL_LINEAR_CONSOLE_BASE_URL:-}" \
  -e "AGENT_CONTROL_KNOWLEDGE_ENABLED=${AGENT_CONTROL_KNOWLEDGE_ENABLED:-false}" \
  -e "AGENT_CONTROL_KNOWLEDGE_DB_URL=postgresql+psycopg://knowledge_read:${KNOWLEDGE_READ_DB_PASSWORD:-knowledge_read_local}@${PG_IP}:5432/agent_knowledge" \
  -e "AGENT_CONTROL_KNOWLEDGE_SEARCH_MAX_RESULTS=${AGENT_CONTROL_KNOWLEDGE_SEARCH_MAX_RESULTS:-5}" \
  -e "AGENT_CONTROL_KNOWLEDGE_SNIPPET_MAX_CHARS=${AGENT_CONTROL_KNOWLEDGE_SNIPPET_MAX_CHARS:-1200}" \
  -e "AGENT_CONTROL_KNOWLEDGE_SEARCHES_PER_MINUTE=${AGENT_CONTROL_KNOWLEDGE_SEARCHES_PER_MINUTE:-6}" \
  -e "AGENT_CONTROL_KNOWLEDGE_STALENESS_WARN_SECONDS=${AGENT_CONTROL_KNOWLEDGE_STALENESS_WARN_SECONDS:-86400}" \
  -e "AGENT_CONTROL_KNOWLEDGE_RECENT_WINDOW_DAYS_MAX=${AGENT_CONTROL_KNOWLEDGE_RECENT_WINDOW_DAYS_MAX:-14}" \
  agent-control-server:local >/dev/null
fi
SERVER_IP=$(ip_of "$SERVER_NAME")
echo "   up at $SERVER_IP (host: http://localhost:${AGENT_CONTROL_SERVER_HOST_PORT:-8000})"

echo "== dispatcher"
if running "$DISPATCHER_NAME"; then echo "   already running"; else
container rm "$DISPATCHER_NAME" >/dev/null 2>&1 || true
container run -d --name "$DISPATCHER_NAME" --network "$NETWORK" -a arm64 \
  -e "AGENT_CONTROL_BASE_URL=http://${SERVER_IP}:8000" \
  -e "AGENT_CONTROL_API_KEY=${AGENT_CONTROL_DISPATCHER_API_KEY:-${AGENT_CONTROL_API_KEYS:-}}" \
  agent-control-dispatcher:local \
  serve --poll-seconds "${AGENT_CONTROL_DISPATCHER_POLL_SECONDS:-5}" \
        --max-tasks "${AGENT_CONTROL_DISPATCHER_MAX_TASKS:-1}" >/dev/null
fi
echo "   polling ${SERVER_IP}:8000"

# Phase 2 is `once`: run to completion, then exit. Detached so `up` does not
# block on a first full sync, and skipped outright when the Drive credentials
# are unset - a sync that authenticates against nothing reports zero documents,
# which is indistinguishable from an empty folder.
echo "== knowledge sync"
if [ -z "${AGENT_KNOWLEDGE_DRIVE_REFRESH_TOKEN:-}" ] || [ -z "${AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID:-}" ]; then
  echo "   skipped: AGENT_KNOWLEDGE_DRIVE_REFRESH_TOKEN or AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID unset"
elif running "$KNOWLEDGE_NAME"; then
  echo "   already running"
else
container rm "$KNOWLEDGE_NAME" >/dev/null 2>&1 || true
container run -d --name "$KNOWLEDGE_NAME" --network "$NETWORK" -a arm64 \
  -e "AGENT_KNOWLEDGE_DB_URL=postgresql+psycopg://knowledge_sync:${KNOWLEDGE_DB_PASSWORD:-knowledge_local}@${PG_IP}:5432/agent_knowledge" \
  -e "AGENT_KNOWLEDGE_DRIVE_CLIENT_ID=${AGENT_KNOWLEDGE_DRIVE_CLIENT_ID:-}" \
  -e "AGENT_KNOWLEDGE_DRIVE_CLIENT_SECRET=${AGENT_KNOWLEDGE_DRIVE_CLIENT_SECRET:-}" \
  -e "AGENT_KNOWLEDGE_DRIVE_REFRESH_TOKEN=${AGENT_KNOWLEDGE_DRIVE_REFRESH_TOKEN:-}" \
  -e "AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID=${AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID:-}" \
  -e "AGENT_KNOWLEDGE_GITHUB_TOKEN=${AGENT_KNOWLEDGE_GITHUB_TOKEN:-}" \
  -e "AGENT_KNOWLEDGE_FILE_MAX_BYTES=${AGENT_KNOWLEDGE_FILE_MAX_BYTES:-}" \
  -e "AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_RUN=${AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_RUN:-}" \
  -e "AGENT_KNOWLEDGE_SOURCE_MAX_BYTES=${AGENT_KNOWLEDGE_SOURCE_MAX_BYTES:-}" \
  -e "AGENT_KNOWLEDGE_RUN_MAX_FETCH_BYTES=${AGENT_KNOWLEDGE_RUN_MAX_FETCH_BYTES:-}" \
  -e "AGENT_KNOWLEDGE_REQUEST_TIMEOUT_SECONDS=${AGENT_KNOWLEDGE_REQUEST_TIMEOUT_SECONDS:-}" \
  -e "AGENT_KNOWLEDGE_TOMBSTONE_RETENTION_DAYS=${AGENT_KNOWLEDGE_TOMBSTONE_RETENTION_DAYS:-}" \
  -e "AGENT_KNOWLEDGE_SYNC_INTERVAL_SECONDS=${AGENT_KNOWLEDGE_SYNC_INTERVAL_SECONDS:-}" \
  -e "AGENT_KNOWLEDGE_ALLOWLIST_PATH=${AGENT_KNOWLEDGE_ALLOWLIST_PATH:-/config/knowledge.yaml}" \
  -e "AGENT_KNOWLEDGE_LOG_LEVEL=${AGENT_KNOWLEDGE_LOG_LEVEL:-INFO}" \
  -e "AGENT_CONTROL_EXECUTOR_DRIVE_ROOT_ID=${AGENT_CONTROL_EXECUTOR_DRIVE_ROOT_ID:-}" \
  -v "${AGENT_KNOWLEDGE_ALLOWLIST_FILE:-$PWD/knowledge.yaml.example}:/config/knowledge.yaml:ro" \
  agent-control-knowledge:local \
  serve >/dev/null
echo "   syncing against $PG_IP every ${AGENT_KNOWLEDGE_SYNC_INTERVAL_SECONDS:-900}s; container logs $KNOWLEDGE_NAME"
fi

# One executor container per agent named in fleet.yaml, skipped by the same
# mechanism the knowledge sync uses: no config, no service, and it says so.
# `up` is idempotent and does its own ordering - server health, register, start,
# bind - so this hands over rather than sequencing anything itself.
echo "== fleet"
FLEET_CONFIG="${AGENT_CONTROL_FLEET_CONFIG_PATH:-$PWD/fleet.yaml}"
if [ ! -f "$FLEET_CONFIG" ]; then
  echo "   skipped: no $FLEET_CONFIG (copy fleet.yaml.example to start one)"
else
  AGENT_CONTROL_FLEET_CONFIG_PATH="$FLEET_CONFIG" \
  AGENT_CONTROL_FLEET_SERVER_URL="${AGENT_CONTROL_FLEET_SERVER_URL:-http://localhost:${AGENT_CONTROL_SERVER_HOST_PORT:-8000}}" \
    uv run --package agent-control-fleet agent-control-fleet up
fi

echo
echo "UI: http://localhost:${AGENT_CONTROL_SERVER_HOST_PORT:-8000}/ui"
echo
echo "Executors started by the fleet run in containers on the agent-control network"
echo "and publish no ports. An executor started by hand runs on the HOST, where the"
echo "host is the network gateway rather than host.docker.internal; bind it to"
echo "0.0.0.0 and aim its agent_runtimes row at the gateway IP."
