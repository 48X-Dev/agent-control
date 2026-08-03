# Task dispatcher. Deliberately a separate image from the server.
#
# The dispatcher authenticates at AUTHENTICATED level and gets no admin
# privileges anywhere; `dispatch preflight` refuses to start when its key
# validates as admin, because its credential must not be the credential that
# approves its own output (task-dispatcher.md section 4). Shipping it inside the
# server image would hand it the control plane's environment and undo that.
#
# Build from the REPO ROOT, not from this directory, because it is a uv
# workspace member:
#   docker build -t agent-control-dispatcher:local -f dispatcher/Dockerfile .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./
COPY models/ models/
COPY dispatcher/ dispatcher/

# models is the only workspace dependency: the dispatcher is a thin httpx client
# over the Agent Control API and never touches the database, so no driver and no
# server package are installed here. Same fallback as server/Dockerfile so a
# lockfile that is briefly out of sync during development does not fail the build.
RUN uv sync \
      --package agent-control-dispatcher \
      --package agent-control-models \
      --no-dev \
  || uv sync \
      --package agent-control-dispatcher \
      --package agent-control-models \
      --no-dev \
      --frozen

# Non-root. Nothing here writes to disk; the ledger is the server's.
RUN useradd --create-home --uid 10001 dispatcher \
 && chown -R dispatcher:dispatcher /app
USER dispatcher

ENV PATH="/app/.venv/bin:${PATH}"

# No CMD with a default subcommand. `once` claims and executes real tasks that
# spend real model budget, so an image that runs work on `docker compose up`
# would be a foot-gun; the subcommand is always supplied at run time.
ENTRYPOINT ["agent-control-dispatch"]
CMD ["--help"]
