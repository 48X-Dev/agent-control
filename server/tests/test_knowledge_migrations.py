"""The corpus migration environment: its shape, and what it refuses to know.

No database here on purpose, for ``test_knowledge_settings.py``'s reason: these
run on a machine with no Postgres, because what they guard is a chain that only
misbehaves on the deployment that runs it. ``tests/knowledge/test_migration_
steps.py`` walks the same chain against a real database; this file asks the
questions that can be answered from the files alone.

Two of the claims are about absence, and both are load-bearing. The environment
must not import ``agent_control_server``: from Phase 2 these migrations run
inside the sync container, which carries the shared models package and not the
control plane, so an import here makes the corpus schema undeployable without
the thing it is separated from. And it must declare no metadata, or a routine
``--autogenerate`` proposes dropping the generated ``tsvector`` column and the
two GIN indexes on every run, because Core cannot spell them.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Final

import pytest
from agent_control_server.knowledge.schema import SUPPORTED_SCHEMA_VERSIONS
from alembic.config import Config
from alembic.script import Script, ScriptDirectory

from tests.knowledge_provisioning import ALEMBIC_DIR, ALEMBIC_INI, SERVER_DIR

CONTROL_PLANE_INI: Final = SERVER_DIR / "alembic.ini"
CONTROL_PLANE_VERSIONS: Final = SERVER_DIR / "alembic" / "versions"
KNOWLEDGE_VERSIONS: Final = ALEMBIC_DIR / "versions"

# Alembic's own default, and therefore what the control plane uses. The corpus
# must not answer to it: one database holding both bookkeeping tables under one
# name is a control-plane migration that believes it owns the corpus.
DEFAULT_VERSION_TABLE: Final = "alembic_version"

_REVISION_RE: Final = re.compile(r"^revision\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _script_directory() -> ScriptDirectory:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR).replace("%", "%%"))
    return ScriptDirectory.from_config(config)


def _base_to_head() -> list[Script]:
    return list(reversed(list(_script_directory().walk_revisions())))


def _revision_ids(versions_dir: Path) -> set[str]:
    found: set[str] = set()
    for path in versions_dir.glob("*.py"):
        match = _REVISION_RE.search(path.read_text())
        if match:
            found.add(match.group(1))
    return found


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


# --- The chain -------------------------------------------------------------


def test_the_chain_is_linear_and_ends_in_one_head() -> None:
    """One writer, one order. A branch is two corpora claiming one version."""
    directory = _script_directory()

    assert len(directory.get_heads()) == 1, directory.get_heads()
    assert len(directory.get_bases()) == 1, directory.get_bases()

    revisions = list(directory.walk_revisions())
    assert len(revisions) == len(list(KNOWLEDGE_VERSIONS.glob("*.py")))
    for script in revisions:
        assert not isinstance(script.down_revision, tuple), script.revision


def test_every_revision_can_be_taken_back_off() -> None:
    """A migration with no downgrade is a corpus that can only go forwards.

    It is also how a failed upgrade becomes a manual repair on a deployment,
    which is the point at which somebody edits the corpus by hand.
    """
    for script in _base_to_head():
        module = script.module
        assert callable(getattr(module, "upgrade", None)), script.revision
        assert callable(getattr(module, "downgrade", None)), script.revision
        assert "op." in inspect.getsource(module.downgrade), script.revision


def test_each_revision_declares_a_higher_schema_version_than_the_one_before() -> None:
    """The marker row is the reader's only handle on row shape.

    Two revisions claiming the same version is a copy-paste, and the reader has
    no way to notice: it would happily parse rows written by a schema it has
    never seen.
    """
    versions = [script.module.SCHEMA_VERSION for script in _base_to_head()]

    assert versions == sorted(set(versions))
    assert len(versions) == len(set(versions))


def test_the_head_revision_is_the_version_the_reader_says_it_understands() -> None:
    """Bumping the migration without bumping the constant refuses every search.

    That failure is loud in the right way and silent in the wrong one: every
    query answers ``knowledge_unavailable``, which reads to an operator exactly
    like a corpus nobody has synced.
    """
    head_version = _base_to_head()[-1].module.SCHEMA_VERSION

    assert head_version in SUPPORTED_SCHEMA_VERSIONS
    assert head_version == max(SUPPORTED_SCHEMA_VERSIONS)


# --- What the environment must not know ------------------------------------


def test_the_migration_environment_does_not_import_the_control_plane() -> None:
    """The sync container carries the models package, not the server."""
    sources = [ALEMBIC_DIR / "env.py", *KNOWLEDGE_VERSIONS.glob("*.py")]

    for path in sources:
        offenders = {
            name for name in _imported_modules(path) if name.startswith("agent_control_server")
        }
        assert not offenders, f"{path.name} imports {sorted(offenders)}"


def test_the_migration_environment_declares_no_metadata_for_autogenerate() -> None:
    """Autogenerate cannot spell a generated column or a GIN operator class.

    Left with metadata to compare against, it proposes dropping both on every
    run, and the corpus schema is hand-written SQL precisely because that is
    worse than typing the DDL once.
    """
    tree = ast.parse((ALEMBIC_DIR / "env.py").read_text())

    def bound_to_nothing(value: ast.expr | None) -> bool:
        """``target_metadata = None`` is the same claim spelled out loud."""
        return isinstance(value, ast.Constant) and value.value is None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "target_metadata" in names:
                assert bound_to_nothing(node.value), "env.py gives autogenerate something to drop"
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "target_metadata":
                    assert bound_to_nothing(keyword.value), "context.configure is handed metadata"


def test_the_corpus_keeps_its_own_bookkeeping_table() -> None:
    version_table = Config(str(ALEMBIC_INI)).get_main_option("version_table")

    assert version_table == "knowledge_alembic_version"
    assert version_table != DEFAULT_VERSION_TABLE
    assert Config(str(CONTROL_PLANE_INI)).get_main_option("version_table") is None


def test_the_two_migration_surfaces_are_disjoint() -> None:
    """A corpus revision dropped into the control plane's versions directory.

    It would be picked up by ``alembic upgrade head`` and would create corpus
    tables inside ``agent_control``, which is the one place section 4.1 spends
    a whole database to keep them out of.
    """
    knowledge = _revision_ids(KNOWLEDGE_VERSIONS)
    control_plane = _revision_ids(CONTROL_PLANE_VERSIONS)

    assert knowledge
    assert control_plane
    assert not knowledge & control_plane
    assert ALEMBIC_DIR not in CONTROL_PLANE_VERSIONS.parents


@pytest.mark.parametrize("required", ("script_location", "version_table"))
def test_the_shipped_ini_carries_what_a_deployment_needs(required: str) -> None:
    """``make knowledge-migrate`` passes a URL and nothing else."""
    assert Config(str(ALEMBIC_INI)).get_main_option(required)
