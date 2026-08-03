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
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

# Scrubbed while the settings singletons are built. ``AGENT_CONTROL_DB_`` and
# the bare ``DB_``/``DATABASE_URL`` aliases stay, because ``db_config`` is not
# pinned and is built from them.
_SCRUBBED_PREFIX = "AGENT_CONTROL_"
_PRESERVED_PREFIXES = ("AGENT_CONTROL_DB_",)


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
