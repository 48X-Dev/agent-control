"""The entry point: which commands exist, and what an exit code means."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_control_fleet import cli as cli_module
from agent_control_fleet.cli import build_parser, main
from agent_control_fleet.settings import CONFIG_PATH_ENV, EXECUTOR_API_KEY_ENV, REGISTER_API_KEY_ENV


@pytest.mark.parametrize("command", ["up", "register", "bind", "doctor"])
def test_every_command_parses(command: str) -> None:
    assert build_parser().parse_args([command]).command == command


@pytest.mark.parametrize("command", ["up", "bind"])
def test_adopt_is_available_where_a_row_gets_rewritten(command: str) -> None:
    assert build_parser().parse_args([command, "--adopt"]).adopt is True


def test_doctor_takes_no_adopt() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["doctor", "--adopt"])


def test_a_refused_config_exits_two_and_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "fleet.yaml"
    config.write_text(
        "version: 1\nimage: e:local\ngroups:\n  - name: g\n"
        "    agents:\n      - agent_name: nope\n"
    )
    monkeypatch.setattr(
        cli_module.os,
        "environ",
        {
            CONFIG_PATH_ENV: str(config),
            REGISTER_API_KEY_ENV: "admin-key",
            EXECUTOR_API_KEY_ENV: "executor-key",
        },
    )
    assert main(["doctor"]) == 2
    assert "nope" in capsys.readouterr().err


def test_a_missing_fleet_file_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module.os, "environ", {CONFIG_PATH_ENV: str(tmp_path / "absent.yaml")})
    assert main(["doctor"]) == 2
    assert "does not exist" in capsys.readouterr().err
