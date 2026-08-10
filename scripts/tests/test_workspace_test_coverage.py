"""`make test` must run every workspace member, and CI refuses when it does not.

Twice now a workspace member has shipped with a suite nothing ran. The
dispatcher was first, and `dispatcher-test` carries a comment saying so. Then
knowledge_sync did the same thing one package later, with 157 tests invisible to
CI including the only check holding the sync's table metadata to the server's
migrations.

Both were caught by a person noticing. This is the check that means the third
one cannot happen: it reads the members out of the root ``pyproject.toml`` and
asserts that expanding the Makefile's ``test`` target actually reaches each one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Members that genuinely have no suite. An entry needs its reason on the same
# line, because the only other reading of a name here is "somebody forgot".
NO_SUITE: dict[str, str] = {}

HOW_TO_FIX = """
Add a `<name>-test` target to the Makefile and list it in the `test` target's
prerequisites, next to dispatcher-test and knowledge-sync-test. If the member
genuinely has no suite, add it to NO_SUITE in {this_file} with the reason.
""".strip()


def _workspace_members() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["uv"]["workspace"]["members"]


def _expanded_test_target() -> str:
    """What `make test` would actually run, with every variable expanded."""
    env = {key: value for key, value in os.environ.items() if key not in {"MAKEFLAGS", "MAKELEVEL"}}
    result = subprocess.run(
        ["make", "--dry-run", "test"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"`make --dry-run test` failed with {result.returncode}:\n{result.stderr}")
    return result.stdout


def _runs(member: str, expanded: str) -> bool:
    """True when the member appears as a real path, not inside another word.

    The boundaries matter: `coverage-models.xml` is an artifact name, not proof
    that anything runs `models`, and counting it would let the check pass on the
    exact package it is supposed to catch.
    """
    return re.search(rf"(?<![\w/-]){re.escape(member)}(?![\w-])", expanded) is not None


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not on PATH")
def test_make_test_runs_every_workspace_member() -> None:
    expanded = _expanded_test_target()
    missing = [
        member
        for member in _workspace_members()
        if member not in NO_SUITE and not _runs(member, expanded)
    ]
    assert not missing, (
        f"`make test` never runs: {', '.join(missing)}\n\n"
        + HOW_TO_FIX.format(this_file=Path(__file__).relative_to(REPO_ROOT))
    )


def test_every_no_suite_entry_is_still_a_member() -> None:
    """An opt-out for a member that no longer exists is a stale exemption."""
    members = set(_workspace_members())
    stale = sorted(set(NO_SUITE) - members)
    assert not stale, f"NO_SUITE names members that are gone: {', '.join(stale)}"
