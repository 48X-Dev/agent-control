#!/usr/bin/env python3
"""Fail if a company-knowledge env var is not wired everywhere it has to be.

Parity: the three wiring files declare one set. Reached: the sync reads nothing outside it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Everything the corpus needs, on either side of the read/write split, plus the
# one executor variable the ingest guard in section 11 is keyed on.
NAME = r"(?:AGENT_KNOWLEDGE_[A-Z0-9_]+|AGENT_CONTROL_KNOWLEDGE_[A-Z0-9_]+|AGENT_CONTROL_EXECUTOR_DRIVE_ROOT_ID)"

# One rule per file, matched to how that file actually passes a variable to a
# process. Anchoring on the mechanism rather than on the name is deliberate: a
# variable merely mentioned in prose is what "documented but never wired" looks
# like, and it must not count as wiring.
SOURCES = {
    "docker-compose.yml": re.compile(rf"^\s+({NAME}):\s", re.MULTILINE),
    "scripts/apple-container-up.sh": re.compile(rf"-e\s+\"?({NAME})="),
    "server/.env.example": re.compile(rf"^#?\s*({NAME})=", re.MULTILINE),
}

SYNC_SOURCE_DIR = Path("knowledge_sync/src")
READS = re.compile(NAME)


def declared() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for relative, pattern in SOURCES.items():
        path = REPO_ROOT / relative
        if not path.is_file():
            sys.exit(f"knowledge env parity: {relative} is missing")
        found[relative] = set(pattern.findall(path.read_text()))
    return found


def read_by_sync_source() -> dict[str, set[str]]:
    by_var: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / SYNC_SOURCE_DIR).rglob("*.py")):
        for name in READS.findall(path.read_text()):
            by_var.setdefault(name, set()).add(str(path.relative_to(REPO_ROOT)))
    return by_var


def main() -> int:
    found = declared()
    every = set().union(*found.values())
    failures: list[str] = []

    for relative, names in found.items():
        missing = every - names
        if missing:
            failures.append(f"{relative} is missing: {', '.join(sorted(missing))}")

    wired = set.intersection(*found.values()) if found else set()
    for name, files in sorted(read_by_sync_source().items()):
        if name not in wired:
            failures.append(f"{name} is read by {', '.join(sorted(files))} but is not wired everywhere")

    if failures:
        print("knowledge env parity FAILED\n", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("\nEvery knowledge variable ships all of these in the same commit:", file=sys.stderr)
        for relative in SOURCES:
            print(f"  {relative}", file=sys.stderr)
        print("See docs/plans/company-knowledge.md section 12.", file=sys.stderr)
        return 1

    print(f"knowledge env parity OK: {len(every)} variables wired in all {len(SOURCES)} files")
    for name in sorted(every):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
