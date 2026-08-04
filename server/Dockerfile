# Dockerfile for Agent Control Server
#
# Build from the repository root:
#   docker build -t agent-control-server -f server/Dockerfile .
#
# Run:
#   docker run -p 8000:8000 agent-control-server

FROM node:20-alpine AS ui-builder

WORKDIR /ui

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
ENV NEXT_TELEMETRY_DISABLED=1
ENV AGENT_CONTROL_STATIC_EXPORT=true
ENV NEXT_PUBLIC_STATIC_EXPORT=true

RUN corepack enable && corepack prepare pnpm@9 --activate

COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY ui/ ./
RUN pnpm build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Configure uv environment variables
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Copy workspace definition
COPY pyproject.toml uv.lock* ./

# Copy alembic configuration and migrations from server directory
COPY server/alembic.ini ./
COPY server/alembic/ alembic/

# Copy shared dependencies (Workspaces)
COPY models/ models/
COPY engine/ engine/
COPY telemetry/ telemetry/
COPY evaluators/ evaluators/

# Copy server application
COPY server/ server/

# Copy the exported UI bundle that FastAPI serves for hosted deployments
COPY --from=ui-builder /ui/out ./server/ui-dist

# Document conversion, declared as optional extras on agent-control-server.
#
# Without any of these the server still starts and every attachment converts to
# no_converter_installed - a stated status rather than a crash, but not a
# feature either.
#
# OCR is deliberately NOT in the default, and the number is why: adding it takes
# this image to 19.3GB, because docling pulls torch and its CUDA libraries.
# That is not a reasonable default for a published quick start.
#
# It is still the main path on real corpora rather than a fallback. Measured on
# one: five of six attachments were images with no text layer, where the
# text-layer converter returns zero characters and OCR returns useful text. A
# deployment whose files are images should build with:
#
#   --build-arg CONVERTER_EXTRAS="--extra text-extraction --extra ocr"
#
# and accept the size. Without it, an image attachment answers
# ocr_converter_absent, which says what is missing rather than failing silently.
ARG CONVERTER_EXTRAS="--extra text-extraction"

# Install server + bundled runtime packages (engine/evaluators/models/telemetry)
# We ignore the lockfile if it's missing or out of sync to ensure build succeeds during dev
RUN uv sync \
    --package agent-control-server \
    --package agent-control-engine \
    --package agent-control-evaluators \
    --package agent-control-models \
    --package agent-control-telemetry \
    ${CONVERTER_EXTRAS} \
    --no-dev \
  || uv sync \
    --package agent-control-server \
    --package agent-control-engine \
    --package agent-control-evaluators \
    --package agent-control-models \
    --package agent-control-telemetry \
    ${CONVERTER_EXTRAS} \
    --no-dev \
    --frozen

# Add the virtual environment to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set default environment variables
ENV AGENT_CONTROL_HOST=0.0.0.0
ENV AGENT_CONTROL_PORT=8000
ENV PSYCOPG_IMPL=python

# Expose the port
EXPOSE 8000

# Copy and configure entrypoint script
COPY server/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set entrypoint to run migrations before starting server
ENTRYPOINT ["docker-entrypoint.sh"]

# Run the server entrypoint defined in pyproject.toml
CMD ["agent-control-server"]
