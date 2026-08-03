"""The harness's own invariant: no developer environment reaches an assertion.

Every other file in this directory takes it for granted that
``executor_settings.enabled`` is false and that the credential flag is off,
because those are the declared defaults. They are *not* what the repository
``.env`` says on a machine where somebody is running the chat feature, and they
are not what an exported shell variable says either. ``server/conftest.py``
pins them, and until this file existed that pinning was asserted only by the
suite happening to pass - which means deleting the conftest would have been
caught as a hundred confusing failures somewhere else, or not at all if the
developer's ``.env`` happened to agree with the defaults that day.

Two shapes of check here, because the two failure modes are different:

* In-process assertions that the singletons hold their declared defaults. These
  catch a pin that stopped working.
* A subprocess that runs pytest under a deliberately hostile environment. This
  catches the narrower case where a bad exported value stops the suite from
  *collecting* at all, which no in-process assertion can observe: by the time
  any test runs, the import that would have failed has already succeeded.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_control_server import config as server_config

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The pin holds
# ---------------------------------------------------------------------------


def test_the_dotenv_file_is_switched_off_for_the_whole_run() -> None:
    """Layer 1, asserted rather than assumed.

    ``_resolve_env_file`` returns ``None`` for the empty string, so any settings
    object constructed from here on reads no file. The conftest sets this before
    the first ``agent_control_server`` import; if the ordering ever slipped, the
    variable would still be set by the time this ran, so the real proof is the
    default-valued singletons below. This is the cheap half.
    """
    assert os.environ.get("AGENT_CONTROL_SETTINGS_ENV_FILE") == ""
    assert server_config._resolve_env_file() is None


def test_the_repository_env_file_disagrees_with_the_pinned_values() -> None:
    """Without this, the pin could be a no-op and every check would still pass.

    The whole point of the conftest is that the developer's ``.env`` says one
    thing and the suite asserts another. If somebody sets that file back to the
    defaults, these assertions stop proving anything - so the test states the
    precondition instead of quietly depending on it.
    """
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        pytest.skip("no repository .env on this machine; nothing for the pin to override")

    declared = dict(
        line.split("=", 1)
        for line in env_file.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    overridden = {
        name: value
        for name, value in declared.items()
        if name.startswith("AGENT_CONTROL_") and not name.startswith("AGENT_CONTROL_DB_")
    }
    if not overridden:
        pytest.skip(".env sets no non-database AGENT_CONTROL_* value to override")

    # At least one of them has to differ from the default, or the file is not
    # exercising the pin at all.
    assert any(value.strip().lower() not in {"", "false", "0"} for value in overridden.values()), (
        f"nothing in .env would move a default: {sorted(overridden)}"
    )


def test_the_executor_flag_holds_its_declared_default() -> None:
    """The nine tests that assert executor-disabled behaviour depend on this."""
    assert server_config.executor_settings.enabled is False


def test_the_credential_flag_is_owned_by_the_suite_and_not_by_the_environment() -> None:
    """This one is deliberately *not* left at its default, and that is the point.

    ``setup_auth`` in ``tests/conftest.py`` is autouse and turns credentials on
    with known test keys, so every test in this directory runs against a
    configured server. The pin still matters underneath it: without the pin the
    starting value would be whatever ``.env`` said, and any test that clears the
    fixture's monkeypatch, or any module imported before it, would see the
    developer's value instead of the declared one.

    So the assertion is about ownership. The keys are the fixture's, not the
    machine's.
    """
    from .conftest import TEST_ADMIN_API_KEY, TEST_API_KEY

    assert server_config.auth_settings.api_key_enabled is True
    assert server_config.auth_settings.api_keys == TEST_API_KEY
    assert server_config.auth_settings.admin_api_keys == TEST_ADMIN_API_KEY


def test_the_model_allowlist_holds_its_declared_default() -> None:
    """Empty, so the "no models configured" path is what tests see by default."""
    assert server_config.model_settings.allowlist == []


def test_the_database_settings_are_deliberately_not_pinned() -> None:
    """The one exemption, asserted so nobody "fixes" it into the pinned tuple.

    CI and local runs point the suite at different hosts through
    ``AGENT_CONTROL_DB_*``. Pinning this would mean the suite could only ever
    talk to one database, which is why the conftest preserves the prefix.
    """
    conftest = (_REPO_ROOT / "server" / "conftest.py").read_text()
    assert '_PRESERVED_PREFIXES = ("AGENT_CONTROL_DB_",)' in conftest
    assert "_config.db_config" not in conftest


# ---------------------------------------------------------------------------
# The pin survives a hostile environment, including one that would abort import
# ---------------------------------------------------------------------------


# The probe lives at ``server/`` rather than at ``server/tests/``, deliberately.
# ``server/conftest.py`` is the thing under test and covers both, but this
# directory's own conftest adds an autouse ``setup_auth`` that turns credentials
# on. A probe underneath it could not assert ``api_key_enabled is False``,
# which is the single most load-bearing pinned value: it is the input to the
# agent-config delivery gate. One directory up, only the pin applies.
_PROBE = """
from agent_control_server import config

def test_probe():
    assert config.executor_settings.enabled is False
    assert config.auth_settings.api_key_enabled is False
    assert config.model_settings.allowlist == []
    assert config.linear_settings.api_key.get_secret_value() == ""
"""

_PROBE_PATH = _REPO_ROOT / "server" / "test_zz_settings_pin_probe.py"


def _run_probe(hostile: dict[str, str]) -> subprocess.CompletedProcess[str]:
    _PROBE_PATH.write_text(_PROBE)
    env = {**os.environ, **hostile}
    # Not inherited. This process is already inside a pinned run, and handing the
    # marker down would let the child skip the work under test.
    env.pop("AGENT_CONTROL_SETTINGS_ENV_FILE", None)
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(_PROBE_PATH), "-q", "-p", "no:cacheprovider"],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        _PROBE_PATH.unlink(missing_ok=True)


def test_exported_variables_do_not_move_the_pinned_values() -> None:
    """Layer 2. A shell that disagrees with the defaults loses.

    Each of these would otherwise change what a test asserts: the executor gate,
    the credential flag that drives the delivery gate, and a third-party key
    whose presence decides whether the Linear client is live.
    """
    result = _run_probe(
        {
            "AGENT_CONTROL_EXECUTOR_ENABLED": "true",
            "AGENT_CONTROL_API_KEY_ENABLED": "true",
            "AGENT_CONTROL_LINEAR_API_KEY": "lin_api_bogus_exported_value",
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_exported_value_that_would_abort_the_import_is_scrubbed_first() -> None:
    """The narrow case, and the reason the scrub wraps the import.

    ``ModelSettings`` refuses to construct when an entry's id disagrees with its
    declared provider - correctly, because a mislabelled row sends traffic to a
    vendor nobody chose. But it refuses by *raising*, at ``config.py``'s module
    scope, so an exported allowlist takes the whole suite down at collection time
    with a stack trace naming pydantic rather than the developer's shell. A scrub
    that ran only after the import could never catch it, and this is the exact
    value that caught the earlier arrangement out.
    """
    result = _run_probe(
        {
            "AGENT_CONTROL_MODELS_ALLOWLIST": (
                '[{"id":"gpt-5.4-mini","label":"Exported","provider":"gemini",'
                '"cost_tier":"premium","recommended":true}]'
            ),
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ValidationError" not in result.stdout + result.stderr
