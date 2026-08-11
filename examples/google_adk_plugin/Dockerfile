# The agent executor. One image for the whole fleet; a container differs from its
# siblings only by AGENT_CONTROL_AGENT_NAME, which the entrypoint turns into the
# single agent package this process serves (agent-fleet-topology.md 3.3-3.5).
#
# Build from the REPO ROOT, because the example is a uv workspace member:
#   container build -t agent-control-executor:local -f examples/google_adk_plugin/Dockerfile .
#
# Run it read-only with a tmpfs on /agents: the materialized package is the one
# thing this writes and it is rewritten at every start.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./
COPY models/ models/
COPY engine/ engine/
COPY evaluators/ evaluators/
COPY telemetry/ telemetry/
COPY sdks/python/ sdks/python/

# Named files rather than the directory: the example holds a real .env and a
# .venv, and the ignore file at the context root does not know about either.
COPY examples/google_adk_plugin/pyproject.toml examples/google_adk_plugin/README.md \
     examples/google_adk_plugin/
COPY examples/google_adk_plugin/my_agent/ examples/google_adk_plugin/my_agent/

# Same lockfile fallback as server/Dockerfile, dispatcher/Dockerfile and
# knowledge_sync/Dockerfile.
RUN uv sync \
      --package agent-control-google-adk-plugin-example \
      --no-dev \
  || uv sync \
      --package agent-control-google-adk-plugin-example \
      --no-dev \
      --frozen

COPY examples/google_adk_plugin/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Non-root, continuing the uid 10001 dispatcher and 10002 knowledge sequence.
# /agents is created here so the image is runnable without a tmpfs; the fleet
# mounts one over it.
RUN useradd --create-home --uid 10003 executor \
 && mkdir -p /agents \
 && chown -R executor:executor /app /agents
USER executor

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
