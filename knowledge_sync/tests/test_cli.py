"""The three invocations, met the way an operator meets them.

Through :func:`main`, because the exit code is what a cron wrapper reads.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pytest
from agent_control_knowledge_sync import cli
from agent_control_knowledge_sync.config import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    DATABASE_URL_ENV,
    REFRESH_TOKEN_ENV,
    ROOT_FOLDER_ENV,
)
from agent_control_knowledge_sync.drive_client import DriveRootUnreachableError
from agent_control_knowledge_sync.lease import LeaseHeldError
from agent_control_knowledge_sync.sync import CorpusStatus, RunCounters, SyncFailedError
from sqlalchemy.exc import OperationalError

COUNTED = RunCounters(
    seen=9, indexed=4, unchanged=3, tombstoned=1, refused=1, refusals_by_code={"oversize": 1}
)

FRESH = CorpusStatus(
    documents=1204,
    chunks=4318,
    sources_enabled=1,
    sources_failing=0,
    last_verified_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
    stale_seconds=900,
    last_run_status="ok",
    last_run_finished_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
    last_run_error_code=None,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLIENT_ID_ENV, "123.apps.googleusercontent.com")
    monkeypatch.setenv(CLIENT_SECRET_ENV, "GOCSPX-not-real")
    monkeypatch.setenv(REFRESH_TOKEN_ENV, "1//not-real")
    monkeypatch.setenv(ROOT_FOLDER_ENV, "folder-root")
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql+psycopg://u:p@localhost/agent_knowledge")


def _returns(value: Any) -> Any:
    async def _call(*args: Any, **kwargs: Any) -> Any:
        return value

    return _call


def _raises(error: Exception) -> Any:
    async def _call(*args: Any, **kwargs: Any) -> Any:
        raise error

    return _call


class FakeSessions:
    """Stands in for the engine context manager; nothing here opens a socket."""

    def __init__(self, config: Any) -> None:
        self._config = config

    async def __aenter__(self) -> Any:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _stub_status(monkeypatch: pytest.MonkeyPatch, status: CorpusStatus) -> None:
    monkeypatch.setattr(cli, "read_status", _returns(status))
    monkeypatch.setattr(cli, "corpus_sessions", FakeSessions)


def test_once_prints_the_counters_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run_once", _returns(COUNTED))

    code = cli.main(["once"])
    out = capsys.readouterr().out

    assert code == 0
    assert "seen 9" in out
    assert "indexed 4" in out
    assert "unchanged 3" in out
    assert "tombstoned 1" in out


def test_a_refused_document_is_named_but_does_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cron reads the exit code; a corpus with one unreadable PDF is not an outage."""
    monkeypatch.setattr(cli, "run_once", _returns(COUNTED))

    assert cli.main(["once"]) == 0
    assert "oversize 1" in capsys.readouterr().out


def test_a_held_lease_exits_two_and_says_no_cursor_moved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    held = LeaseHeldError(holder="run-abc", expires_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC))
    monkeypatch.setattr(cli, "run_once", _raises(held))

    code = cli.main(["once"])
    err = capsys.readouterr().err

    assert code == 2
    assert "run-abc" in err
    assert "no cursor moved" in err


def test_an_unreachable_root_exits_two_rather_than_reporting_an_empty_corpus(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run_once", _raises(DriveRootUnreachableError("not shared")))

    assert cli.main(["once"]) == 2
    assert "not shared" in capsys.readouterr().err


def test_an_unsupported_schema_exits_two_with_the_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = SyncFailedError("The corpus reports schema version 2", code="schema_unsupported")
    monkeypatch.setattr(cli, "run_once", _raises(failure))

    assert cli.main(["once"]) == 2
    assert "schema version 2" in capsys.readouterr().err


def test_an_unreachable_database_is_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Postgres being down is an operator's ordinary Tuesday, not a crash report."""
    monkeypatch.setattr(cli, "run_once", _raises(OperationalError("SELECT 1", {}, Exception())))

    assert cli.main(["once"]) == 2
    assert "Could not reach the corpus database" in capsys.readouterr().err


def test_status_says_so_too_when_the_corpus_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "corpus_sessions", FakeSessions)
    monkeypatch.setattr(cli, "read_status", _raises(OperationalError("SELECT 1", {}, Exception())))

    assert cli.main(["status"]) == 2
    assert "Could not read the corpus" in capsys.readouterr().err


def test_missing_configuration_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ROOT_FOLDER_ENV)

    assert cli.main(["once"]) == 2
    assert ROOT_FOLDER_ENV in capsys.readouterr().err


def test_status_prints_the_corpus_and_exits_zero_when_fresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_status(monkeypatch, FRESH)

    code = cli.main(["status"])
    out = capsys.readouterr().out

    assert code == 0
    assert "documents 1204 in 4318 chunks" in out
    assert "verified 15m ago" in out
    assert "last run ok" in out
    assert "WARNING" not in out


def test_status_exits_one_and_warns_when_the_mirror_is_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_status(monkeypatch, dataclasses.replace(FRESH, stale_seconds=200_000))

    code = cli.main(["status"])
    out = capsys.readouterr().out

    assert code == 1
    assert "WARNING" in out
    assert "verified 2d ago" in out


def test_status_exits_one_when_a_source_is_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, dataclasses.replace(FRESH, sources_failing=1))

    assert cli.main(["status"]) == 1


def test_a_source_that_never_verified_is_stale_rather_than_fresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Zero would read as "just synced", which is the opposite of the truth."""
    _stub_status(monkeypatch, dataclasses.replace(FRESH, stale_seconds=None))

    assert cli.main(["status"]) == 1
    assert "never" in capsys.readouterr().out


def test_a_missing_subcommand_is_refused() -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2


def test_serve_runs_the_loop_and_returns_what_it_exits_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    async def loop(config: Any) -> int:
        seen.append(config)
        return 0

    monkeypatch.setattr(cli, "serve", loop)

    assert cli.main(["serve"]) == 0
    assert seen[0].root_folder_id == "folder-root", "the loop gets the config from the env"


def test_serve_passes_a_configuration_fault_out_as_a_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart policy needs to see the difference between a clean stop and a fault."""
    monkeypatch.setattr(cli, "serve", _returns(2))

    assert cli.main(["serve"]) == 2


def test_serve_refuses_before_the_first_pass_when_the_environment_is_short(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ROOT_FOLDER_ENV)

    assert cli.main(["serve"]) == 2
    assert ROOT_FOLDER_ENV in capsys.readouterr().err


def test_a_second_interrupt_stops_the_loop_with_the_conventional_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first is caught and asks for a clean stop; the second is somebody not waiting."""
    monkeypatch.setattr(cli, "serve", _raises(KeyboardInterrupt()))

    assert cli.main(["serve"]) == 130
    assert "cursor stays" in capsys.readouterr().out
