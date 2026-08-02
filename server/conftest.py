"""Pin server settings before any test module imports the application.

The problem this file exists to solve, stated plainly. Every settings class in
``agent_control_server.config`` reads the repository-root ``.env``, and every
settings object is a module-level singleton built at import time. So a
developer who sets ``AGENT_CONTROL_EXECUTOR_ENABLED=true`` in that file to run
the chat feature locally changes what the test suite asserts, and nine tests
that check executor-disabled behaviour start failing on a clean tree. Pinning
the value on the command line works and teaches everybody the wrong lesson: the
suite should not have an opinion that a local file can overwrite.

Two layers, in this order, both at module import so nothing races a fixture:

1. The dotenv file is switched off for the whole run, via
   ``AGENT_CONTROL_SETTINGS_ENV_FILE=""``. This must happen before
   ``agent_control_server.config`` is imported anywhere, which is why it is at
   the top of this file rather than inside a fixture.
2. Every settings singleton except the database configuration is rebuilt from
   its declared defaults with the ``AGENT_CONTROL_*`` environment scrubbed, and
   the new values are copied onto the existing objects. Layer 1 alone would
   still leave an exported shell variable able to move an assertion; this closes
   that too.

The database configuration is deliberately exempt. It is the one thing the test
harness has no other channel for: CI and local runs point the suite at different
hosts, ports and databases through ``AGENT_CONTROL_DB_*``, and pinning it would
mean the suite could only ever talk to one database.

A test that needs a non-default setting says so, with ``monkeypatch.setattr`` on
the singleton, the way ``setup_auth`` in ``tests/conftest.py`` already does.
Fixtures run long after this module is imported, so nothing here fights them.
"""

from __future__ import annotations

import os

# Layer 1. Before the first ``agent_control_server`` import in this process.
os.environ["AGENT_CONTROL_SETTINGS_ENV_FILE"] = ""

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
)

# Scrubbed while the pinned replacements are constructed. ``AGENT_CONTROL_DB_``
# and the bare ``DB_``/``DATABASE_URL`` aliases stay, because ``db_config`` is
# not pinned and is built from them.
_SCRUBBED_PREFIX = "AGENT_CONTROL_"
_PRESERVED_PREFIXES = ("AGENT_CONTROL_DB_",)


def _pin_to_declared_defaults() -> None:
    """Rebuild each pinned singleton from defaults and copy the values across.

    In place rather than by rebinding the module attribute: a dozen modules do
    ``from ..config import executor_settings`` and hold the object, so replacing
    ``config.executor_settings`` would pin nothing that matters.
    """
    saved = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(_SCRUBBED_PREFIX) and not name.startswith(_PRESERVED_PREFIXES)
    }
    for name in saved:
        del os.environ[name]
    try:
        for singleton in _PINNED_SETTINGS:
            fresh = type(singleton)(_env_file=None)
            for field_name in type(singleton).model_fields:
                setattr(singleton, field_name, getattr(fresh, field_name))
            # ``cached_property`` results (parsed key sets, the resolved session
            # secret) live in the instance dict and would otherwise survive the
            # field assignments above.
            for cached in list(singleton.__dict__):
                if cached not in type(singleton).model_fields:
                    del singleton.__dict__[cached]
    finally:
        os.environ.update(saved)


_pin_to_declared_defaults()
