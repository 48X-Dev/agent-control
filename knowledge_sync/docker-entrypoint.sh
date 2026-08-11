#!/bin/bash
# Migrate the corpus, then run the CLI. This container holds the only DSN that
# can: the server's reader role has SELECT and nothing else.
set -e

case "${1:-}" in
  serve|once)
    if [ -n "${AGENT_KNOWLEDGE_DB_URL:-}" ]; then
      echo "Running corpus migrations..."
      alembic -c /app/knowledge_alembic.ini upgrade head
      echo "✓ Corpus migrations complete"
    else
      echo "⚠ AGENT_KNOWLEDGE_DB_URL unset, skipping corpus migrations"
    fi
    ;;
esac

exec agent-knowledge-sync "$@"
