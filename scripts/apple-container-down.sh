#!/usr/bin/env bash
# Stop the Apple-container stack. The pgdata volume survives; delete it
# yourself with `container volume rm ac-pgdata` if you mean to start clean.
set -uo pipefail
for name in ac-knowledge ac-dispatcher ac-server ac-postgres; do
  container stop "$name" >/dev/null 2>&1
  container rm "$name" >/dev/null 2>&1
  echo "stopped $name"
done
