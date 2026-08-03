"""Pin server settings before any test module imports the application.

The problem this file exists to solve, stated plainly. Every settings class in
``agent_control_server.config`` reads the repository-root ``.env``, and every
settings object is a module-level singleton built at import time. So a
developer who sets ``AGENT_CONTROL_EXECUTOR_ENABLED=true`` in that file to run
the chat feature locally changes what the test suite asserts, and nine tests
that check executor-disabled behaviour start failing on a clean tree. Pinning
the value on the command line works and teaches everybody the wrong lesson: the
suite should not have an opinion that a local file can overwrite.

Two layers, both at module import so nothing races a fixture, and both inside
one scrub that covers the ``config`` import itself:

1. The dotenv file is switched off for the whole run, via
   ``AGENT_CONTROL_SETTINGS_ENV_FILE=""``. This must happen before
   ``agent_control_server.config`` is imported anywhere, which is why it is at
   the top of this file rather than inside a fixture.
2. Every settings singleton except the database configuration is rebuilt from
   its declared defaults with the ``AGENT_CONTROL_*`` environment scrubbed, and
   the new values are copied onto the existing objects. Layer 1 alone would
   still leave an exported shell variable able to move an assertion; this closes
   that too.

The scrub has to be in force *during* the import and not merely after it,
because some settings classes refuse bad input by raising: an exported
``AGENT_CONTROL_MODELS_ALLOWLIST`` with a mislabelled entry aborts collection at
``config.py``'s module scope, before any later cleanup could run.

One thing this cannot reach: a settings object a test constructs for itself
reads the live environment, because the scrub is only held while the singletons
are built. Tests that construct one say so with their own ``monkeypatch.delenv``
(see ``TestModelSettingsRefusesToLoad`` in ``test_agent_configs_models.py``).

The database configuration is deliberately exempt. It is the one thing the test
harness has no other channel for: CI and local runs point the suite at different
hosts, ports and databases through ``AGENT_CONTROL_DB_*``, and pinning it would
mean the suite could only ever talk to one database.

A test that needs a non-default setting says so, with ``monkeypatch.setattr`` on
the singleton, the way ``setup_auth`` in ``tests/conftest.py`` already does.
Fixtures run long after this module is imported, so nothing here fights them.

**The database name is per process, and that is a correctness fix rather than
tidiness.** ``tests/conftest.py`` truncates every table before every test. With
one fixed database name, two pytest processes against the same Postgres delete
each other's rows mid-test, and the failures land on whichever assertions
happened to be running: a different set each time, in modules that pass alone.
Measured on an identical tree, one shared name gave 131 failures against 1580
passes and an isolated name gave 1715 passes against zero. Three separate
people have investigated that as a flake.

So a suffix is appended unconditionally - to the environment's name as much as
to the default - because an override two runs can both set is the same bug with
one more step in front of it. See :data:`TEST_DATABASE_NAME`.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterator

# Scrubbed while the settings singletons are built. ``AGENT_CONTROL_DB_`` and
# the bare ``DB_``/``DATABASE_URL`` aliases stay, because ``db_config`` is not
# pinned and is built from them.
_SCRUBBED_PREFIX = "AGENT_CONTROL_"
_PRESERVED_PREFIXES = ("AGENT_CONTROL_DB_",)

_DEFAULT_TEST_DATABASE_BASE = "agent_control_test"
_MAX_IDENTIFIER_LENGTH = 63
"""Postgres truncates identifiers past this silently, which would put two runs
back on one name after the suffix was cut off. The base is trimmed instead."""

_DATABASE_URL_ALIASES = ("AGENT_CONTROL_DB_URL", "DATABASE_URL", "DB_URL")
"""``AgentControlServerDatabaseConfig.get_url`` prefers any of these over the
host/port/database fields, so a suffix applied only to the name would be
ignored whenever one is exported."""

_DATABASE_NAME_ALIASES = ("AGENT_CONTROL_DB_DATABASE", "DB_DATABASE")
"""Both names ``db_config.database`` accepts, in its own precedence order.

CI exports only the bare ``DB_DATABASE``. Reading the prefixed one alone would
have made every CI run fall back to the default base, which still isolates but
names the database after something nobody configured."""


def _run_token() -> str:
    """A tag no two concurrent runs can share.

    The pid alone is not enough: pids are reused, and a run that died without
    reaching its teardown can leave a database whose name a later run would
    then adopt. Four random hex characters make the collision uninteresting,
    and the xdist worker id keeps a parallel run's databases readable in
    ``\\l`` rather than merely distinct.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    parts = [part for part in (worker, str(os.getpid()), secrets.token_hex(2)) if part]
    return "_".join(parts)


def _derive_database_name(base: str, token: str) -> str:
    suffix = f"_{token}"
    return base[: _MAX_IDENTIFIER_LENGTH - len(suffix)] + suffix


def _rewrite_database_in_url(url: str, database: str) -> str | None:
    """Point a Postgres URL at ``database``. ``None`` for anything else.

    SQLite and other backends are left exactly as they were: a file path is
    not a database name, and guessing at one would break a local run that is
    deliberately pointed at a file.
    """
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    if not parsed.get_backend_name().startswith("postgresql"):
        return None
    return parsed.set(database=database).render_as_string(hide_password=False)


def _isolate_test_database() -> str:
    """Give this process its own database name and export it. Returns the name.

    Runs at import, before ``agent_control_server.config`` is read anywhere:
    ``db_config`` is a module-level singleton and ``db.py`` builds its engines
    from it at import time, so a fixture would be far too late.
    """
    base = next(
        (os.environ[name] for name in _DATABASE_NAME_ALIASES if os.environ.get(name)),
        _DEFAULT_TEST_DATABASE_BASE,
    )
    database = _derive_database_name(base, _run_token())
    # Written to the prefixed name, which ``db_config`` prefers, so this wins
    # over a bare ``DB_DATABASE`` left in the environment by CI or a shell.
    os.environ["AGENT_CONTROL_DB_DATABASE"] = database
    for alias in _DATABASE_URL_ALIASES:
        url = os.environ.get(alias)
        if not url:
            continue
        rewritten = _rewrite_database_in_url(url, database)
        if rewritten is None:
            # A non-Postgres URL wins over the name, so the isolation this
            # module promises does not hold. Say so rather than let the suite
            # look isolated while two runs share one file.
            raise RuntimeError(
                f"{alias} points at a non-Postgres database, which overrides "
                f"AGENT_CONTROL_DB_DATABASE. The server test suite truncates every "
                f"table before every test, so two runs sharing one database "
                f"delete each other's rows. Unset {alias}, or point it at Postgres."
            )
        os.environ[alias] = rewritten
    return database


TEST_DATABASE_NAME = _isolate_test_database()
"""The database this pytest process owns, created and dropped around the session.

``tests/conftest.py`` does the creating and the dropping; this module only names
it, because the name has to be in the environment before anything imports
``agent_control_server.config``.
"""


@contextlib.contextmanager
def _scrubbed_environment() -> Iterator[None]:
    """Hide every non-database ``AGENT_CONTROL_*`` variable, then put it back.

    This wraps the *import* as well as the rebuild below, and that is not
    belt-and-braces. ``config.py`` constructs its singletons at module scope, and
    several of them reject bad input by raising rather than by falling back: an
    exported ``AGENT_CONTROL_MODELS_ALLOWLIST`` naming a mislabelled entry makes
    ``ModelSettings()`` raise ``ValidationError`` on line one of the import, so
    a scrub that ran only afterwards would never get the chance. The symptom is
    not a moved assertion, it is the whole suite failing to collect, which is a
    different way for a developer's shell to decide what CI means.
    """
    saved = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(_SCRUBBED_PREFIX) and not name.startswith(_PRESERVED_PREFIXES)
    }
    for name in saved:
        del os.environ[name]
    # Layer 1: no dotenv file either, so the repository ``.env`` is inert for the
    # whole run rather than only for the objects built inside this block.
    os.environ["AGENT_CONTROL_SETTINGS_ENV_FILE"] = ""
    try:
        yield
    finally:
        os.environ.update(saved)
        os.environ["AGENT_CONTROL_SETTINGS_ENV_FILE"] = ""


with _scrubbed_environment():
    from agent_control_server import config as _config  # noqa: E402

    # The database settings read the environment on purpose; see the module
    # docstring. Everything else is pinned.
    _PINNED_SETTINGS = (
        _config.auth_settings,
        _config.settings,
        _config.observability_settings,
        _config.ui_settings,
        _config.linear_settings,
        _config.executor_settings,
        _config.model_settings,
        # After ``executor_settings``: the lease refusal on ``DispatchSettings``
        # reads the turn timeout off it, so it has to be back at its default
        # before this one is rebuilt.
        _config.dispatch_settings,
    )

    # Rebuild each pinned singleton from its declared defaults and copy the
    # values across. In place rather than by rebinding the module attribute: a
    # dozen modules do ``from ..config import executor_settings`` and hold the
    # object, so replacing ``config.executor_settings`` would pin nothing that
    # matters. The import above may itself have been the first one in the
    # process or may not have been, so this runs either way.
    for _singleton in _PINNED_SETTINGS:
        _fresh = type(_singleton)(_env_file=None)
        for _field_name in type(_singleton).model_fields:
            setattr(_singleton, _field_name, getattr(_fresh, _field_name))
        # ``cached_property`` results (parsed key sets, the resolved session
        # secret) live in the instance dict and would otherwise survive the
        # field assignments above.
        for _cached in list(_singleton.__dict__):
            if _cached not in type(_singleton).model_fields:
                del _singleton.__dict__[_cached]
