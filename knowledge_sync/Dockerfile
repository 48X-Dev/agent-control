# Company knowledge sync. A separate image from the server, like the dispatcher
# and for a sharper reason: this is the only process that holds a Drive
# credential (company-knowledge.md 2.2), and shipping it inside the server image
# would hand that credential the control plane's environment.
#
# Build from the REPO ROOT, not from this directory, because it is a uv
# workspace member:
#   docker build -t agent-control-knowledge:local -f knowledge_sync/Dockerfile .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# libpq for psycopg: unlike the dispatcher, this process writes a database.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock* ./
COPY models/ models/
COPY knowledge_sync/ knowledge_sync/

# The corpus migrations live under server/ but are not the server's to run: they
# need CREATE, and the server holds the reader role. env.py imports no server
# code precisely so this copy works.
COPY server/knowledge_alembic.ini ./
COPY server/knowledge_alembic/ knowledge_alembic/

# MarkItDown is required, not optional. Without it every document fails as
# converter_absent and the run still reports a completed sync.
ARG CONVERTER_EXTRAS="--extra text-extraction"

# Same lockfile fallback as server/Dockerfile and dispatcher/Dockerfile.
RUN uv sync \
      --package agent-control-knowledge-sync \
      --package agent-control-models \
      ${CONVERTER_EXTRAS} \
      --no-dev \
  || uv sync \
      --package agent-control-knowledge-sync \
      --package agent-control-models \
      ${CONVERTER_EXTRAS} \
      --no-dev \
      --frozen

COPY knowledge_sync/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Non-root. The corpus is the only thing this writes, and it is in Postgres.
RUN useradd --create-home --uid 10002 knowledge \
 && chown -R knowledge:knowledge /app
USER knowledge

ENV PATH="/app/.venv/bin:${PATH}" \
    PSYCOPG_IMPL=python

# `--help` rather than `once`, matching the dispatcher: `once` reads Drive and
# writes the corpus, so the working subcommand is supplied by whoever meant it.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["--help"]
