#!/usr/bin/env bash
# Stop the Apple-container stack. The pgdata volume survives; delete it
# yourself with `container volume rm ac-pgdata` if you mean to start clean.
set -uo pipefail

# Executors first: they hold sessions against the server, and their names are
# discovered rather than listed because fleet.yaml decides how many there are.
for name in $(container ls -a 2>/dev/null | awk '{print $1}' | grep '^ac-executor-' || true); do
  container stop "$name" >/dev/null 2>&1
  container rm "$name" >/dev/null 2>&1
  echo "stopped $name"
done

for name in ac-knowledge ac-dispatcher ac-server ac-postgres; do
  container stop "$name" >/dev/null 2>&1
  container rm "$name" >/dev/null 2>&1
  echo "stopped $name"
done
