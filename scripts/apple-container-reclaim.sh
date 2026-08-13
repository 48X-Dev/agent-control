#!/usr/bin/env bash
# Reclaim disk from Apple's `container` runtime.
#
# Run this when builds start failing with "No space left on device", or on a
# schedule. Measured on this machine 2026-08-13: the store had grown to 69GB
# with 2.5GB free, and 20GB of that was the buildkit container's own filesystem
# holding a build cache nothing reads between builds.
#
# `container image prune` does NOT reclaim it, and that is the trap. Prune only
# removes dangling images; the builder is a running container and its cache is
# its rootfs. Every rebuild of a tag also strands the previous snapshot, which
# prune leaves alone because the image it belonged to still exists by name.
#
# Nothing here touches a running application container, a volume, or a tagged
# image. The builder is recreated automatically by the next build.
set -euo pipefail

cd "$(dirname "$0")/.."

STORE="$HOME/Library/Application Support/com.apple.container"

free_gb() { df -g / | awk 'NR==2 {print $4}'; }
store_gb() { du -sg "$STORE" 2>/dev/null | awk '{print $1}'; }

before_free=$(free_gb)
before_store=$(store_gb)
echo "before: ${before_free}GB free, store ${before_store}GB"

# Refuse while a build is running. Deleting the builder under one kills it with
# "Stream unexpectedly closed", and a piped `container build` still exits 0, so
# the failure reads as success and the stale image ships. Cost this script its
# first run.
if pgrep -f "container build" >/dev/null 2>&1; then
  echo "a container build is running; not touching the builder" >&2
  echo "re-run this when it finishes" >&2
  exit 1
fi

# The builder first: it is usually most of it, and deleting it is free because
# the next `container build` starts a new one.
if container builder status >/dev/null 2>&1; then
  echo "== builder"
  container builder stop >/dev/null 2>&1 || true
  container builder delete >/dev/null 2>&1 || true
  echo "   deleted (recreated on next build)"
fi

echo "== dangling images"
container image prune 2>&1 | sed 's/^/   /'

# Worktrees the agent tooling leaves behind. Each is a full checkout, and
# removing one deletes the checkout, never the branch, so no commit is at risk.
if [ -d .claude/worktrees ]; then
  echo "== agent worktrees"
  for w in .claude/worktrees/*/; do
    [ -d "$w" ] || continue
    if [ -n "$(git -C "$w" status --porcelain 2>/dev/null)" ]; then
      echo "   skipped $(basename "$w"): uncommitted changes"
      continue
    fi
    git worktree remove --force "$w" >/dev/null 2>&1 && echo "   removed $(basename "$w")"
  done
  git worktree prune
fi

after_free=$(free_gb)
echo
echo "after: ${after_free}GB free, store $(store_gb)GB  (+$((after_free - before_free))GB)"

# Stated rather than left to be discovered: this grows back. The cache is
# rebuilt by the next build and stranded snapshots accumulate per rebuild, so
# this is maintenance to repeat, not a bug to fix once.
echo
echo "The buildkit cache regrows with every build. Re-run this when space gets tight."
