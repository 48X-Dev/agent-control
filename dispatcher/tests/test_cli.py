"""The invocation section 14 specifies, and the two bounds that hold it down.

``--max-tasks`` is refused above the cap rather than clamped, and ``--dry-run``
is on unless somebody turns it off in writing. Both are tested through
:func:`main` rather than through the parser alone, because an operator meets
them as an exit code and a line on stderr.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_control_dispatcher.cli import API_KEY_ENV, build_parser, main
from agent_control_dispatcher.dispatch import MAX_TASKS_CEILING
from conftest import StubClient

ONE_ITEM = "- ref: t1\n  title: One\n  body: first\n"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "local-agent-key")


def _argv(tmp_path: Path, *extra: str, text: str = ONE_ITEM) -> list[str]:
    source = tmp_path / "tasks.yaml"
    if not source.exists():
        source.write_text(text, encoding="utf-8")
    return [
        "once",
        "--source",
        f"file://{source}",
        "--agent",
        "marketing_researcher",
        "--ledger",
        str(tmp_path / "claims.sqlite3"),
        *extra,
    ]


def test_max_tasks_above_the_cap_is_refused_not_clamped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(_argv(tmp_path, "--max-tasks", "9"))
    err = capsys.readouterr().err

    assert code == 2
    assert "Refused rather than clamped" in err
    assert str(MAX_TASKS_CEILING) in err


def test_max_tasks_below_one_is_refused_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(tmp_path, "--max-tasks", "0")) == 2
    assert "at least 1" in capsys.readouterr().err


def test_the_help_states_the_cap_and_that_nobody_is_running_this_unattended(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["once", "--help"])
    # argparse rewraps every help string, so compare against a flattened copy.
    once_help = " ".join(capsys.readouterr().out.split())

    assert f"Hard cap {MAX_TASKS_CEILING}" in once_help
    assert "a larger value is refused" in once_help
    assert "does not coordinate two dispatchers" in once_help, "the ledger's limit is not buried"

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    assert "it does not run unattended" in " ".join(capsys.readouterr().out.split())


def test_dry_run_is_the_default_and_takes_an_explicit_flag_to_turn_off(tmp_path: Path) -> None:
    parser = build_parser()
    assert parser.parse_args(_argv(tmp_path)).dry_run is True
    assert parser.parse_args(_argv(tmp_path, "--dry-run")).dry_run is True
    assert parser.parse_args(_argv(tmp_path, "--no-dry-run")).dry_run is False


def test_dry_run_and_no_dry_run_together_is_a_contradiction(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(_argv(tmp_path, "--dry-run", "--no-dry-run"))


def test_the_default_run_is_a_dry_run_all_the_way_through(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not just the parser default: the flag has to survive into the run and be
    stated on the terminal the operator is watching."""

    code = main(_argv(tmp_path, "--max-tasks", "1"))
    out = capsys.readouterr().out

    assert code == 0
    assert "dry-run    True" in out
    assert "assertion about this deployment, not a proof" in out
    assert len(stub.turns) == 1


def test_no_dry_run_reaches_the_run_and_says_what_is_not_bounded(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(tmp_path, "--no-dry-run", "--max-tasks", "1")) == 0
    out = capsys.readouterr().out
    assert "dry-run    False" in out
    assert "no canary has proven anything about them" in out


def test_a_missing_api_key_refuses_before_anything_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert main(_argv(tmp_path)) == 2
    assert API_KEY_ENV in capsys.readouterr().err


def test_a_missing_source_file_names_itself_and_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["once", "--source", f"file://{tmp_path / 'absent.yaml'}", "--agent", "a"]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "Cannot read source file" in err
    assert "absent.yaml" in err


def test_a_scheme_this_slice_does_not_have_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["once", "--source", "linear://ENG", "--agent", "a"]) == 2
    assert "later phase" in capsys.readouterr().err


def test_a_malformed_source_file_exits_two_with_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv(tmp_path, text="- ref: t1\n  title: a\n  agent: researcher\n")
    assert main(argv) == 2
    assert "unknown keys" in capsys.readouterr().err


def test_a_run_whose_task_failed_exits_nonzero(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.text_on_turn = {0: "   "}
    assert main(_argv(tmp_path, "--max-tasks", "1")) == 1
    assert "EMPTY_STEP_OUTPUT" in capsys.readouterr().out


def test_print_envelope_shows_the_operator_exactly_what_was_sent(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    main(_argv(tmp_path, "--max-tasks", "1", "--print-envelope"))
    out = capsys.readouterr().out
    assert "<<<TASK_BEGIN>>>" in out
    assert "DATA, not instructions" in out
