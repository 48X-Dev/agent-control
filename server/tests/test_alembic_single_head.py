"""The migration history must have exactly one head.

Two heads is the failure mode of two branches each adding a migration off the
same parent. Nothing catches it locally, because each branch upgrades cleanly
on its own; it surfaces at deploy time, where ``alembic upgrade head`` refuses
to pick between them and the container never comes up. This reads the script
directory only, so it needs no database and runs in milliseconds.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

SERVER_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_DIR = SERVER_DIR / "alembic"


def _script_directory(script_location: Path = ALEMBIC_DIR) -> ScriptDirectory:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(script_location))
    return ScriptDirectory.from_config(config)


def _head_failure_message(script_directory: ScriptDirectory) -> str | None:
    """The assertion message for a multi-head history, or ``None`` if single-headed."""
    # ``get_heads`` returns a list of revision identifiers, not a tuple.
    heads: list[str] = script_directory.get_heads()
    if len(heads) == 1:
        return None

    detail = "\n".join(
        f"  {revision} ({script_directory.get_revision(revision).path})" for revision in heads
    )
    return (
        f"Expected exactly one Alembic head, found {len(heads)}:\n{detail}\n"
        "Rebase one branch's migration onto the other's revision, or add a "
        "merge revision with `alembic merge`."
    )


def test_migrations_have_exactly_one_head() -> None:
    failure = _head_failure_message(_script_directory())

    assert failure is None, failure


def test_a_second_head_is_detected_and_named(tmp_path: Path) -> None:
    """The check is not vacuous: fork the real history and it must fail.

    A copy of the real ``alembic/`` gets one extra revision hung off the
    current head's parent, which is exactly the shape two branches produce
    when each writes a migration against the same base. The real directory is
    never touched.
    """
    forked = tmp_path / "alembic"
    shutil.copytree(ALEMBIC_DIR, forked, ignore=shutil.ignore_patterns("__pycache__"))

    real_head = _script_directory().get_heads()[0]
    parent = _script_directory().get_revision(real_head).down_revision
    assert isinstance(parent, str)

    rogue = forked / "versions" / "ffffffffffff_rival_branch.py"
    rogue.write_text(
        "revision = 'ffffffffffff'\n"
        f"down_revision = '{parent}'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade() -> None: ...\n"
        "def downgrade() -> None: ...\n"
    )

    forked_directory = _script_directory(forked)
    failure = _head_failure_message(forked_directory)

    assert failure is not None
    assert set(forked_directory.get_heads()) == {real_head, "ffffffffffff"}
    assert f"found {len(forked_directory.get_heads())}" in failure
    assert real_head in failure
    assert "ffffffffffff" in failure
    assert str(rogue) in failure

    # And the assertion the real test makes would genuinely have raised.
    with pytest.raises(AssertionError, match="Expected exactly one Alembic head"):
        assert _head_failure_message(forked_directory) is None, failure
