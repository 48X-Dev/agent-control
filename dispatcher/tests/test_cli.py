"""The invocation section 14 specifies, and the two bounds that hold it down.

``--max-tasks`` is refused above the cap rather than clamped, and ``--dry-run``
is on unless somebody turns it off in writing. Both are tested through
:func:`main` rather than through the parser alone, because an operator meets
them as an exit code and a line on stderr.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from agent_control_dispatcher.cli import API_KEY_ENV, build_parser, main
from agent_control_dispatcher.client import DispatchHTTPError, Disposition
from agent_control_dispatcher.dispatch import (
    MAX_TASKS_CEILING,
    DispatchOptions,
    _describe_ledger,
)
from agent_control_dispatcher.sources.linear import SOURCE_PREFIX
from agent_control_models.linear import (
    ListMilestoneIssuesResponse,
    MilestoneIssue,
    MilestoneIssueCounts,
    MilestonesStatus,
)
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


# --- slice 2: one milestone, read only --------------------------------------


def _milestone_argv(tmp_path: Path, *extra: str, team: str = "operations") -> list[str]:
    return [
        "once",
        "--source",
        f"{SOURCE_PREFIX}3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6",
        "--team",
        team,
        "--agent",
        "ops_runbook_agent",
        "--ledger",
        str(tmp_path / "claims.sqlite3"),
        *extra,
    ]


def _one_issue() -> ListMilestoneIssuesResponse:
    return ListMilestoneIssuesResponse(
        status=MilestonesStatus.OK,
        slug="operations",
        linear_team_key="OPS",
        milestone_id="3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6",
        issues=[
            MilestoneIssue(
                ref="uuid-2",
                identifier="OPS-2",
                title="Review the deck",
                description="Body",
                url="https://linear.app/acme/issue/OPS-2",
                updated_at=dt.datetime(2026, 8, 1, 15, 5, tzinfo=dt.UTC),
            )
        ],
        counts=MilestoneIssueCounts(fetched=1, eligible=1),
        fetched_at=dt.datetime(2026, 8, 3, 8, 19, tzinfo=dt.UTC),
    )


def test_the_team_flag_reaches_the_scope_read(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.milestone_response = _one_issue()

    assert main(_milestone_argv(tmp_path, "--max-tasks", "1")) == 0

    assert stub.scope_reads == [("operations", "3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6")]
    assert "team operations" in capsys.readouterr().out


def test_an_unlinked_team_prints_the_refusal_and_exits_two(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live case: ``marketing`` is deliberately not linked to a Linear team."""

    stub.raise_on_scope_read = DispatchHTTPError(
        disposition=Disposition.BLOCKED,
        status_code=409,
        error_code="TEAM_NOT_LINKED",
        detail="Team 'marketing' is not linked to a Linear team.",
    )

    assert main(_milestone_argv(tmp_path, team="marketing")) == 2

    err = capsys.readouterr().err
    assert "Could not read the source scope" in err
    assert "TEAM_NOT_LINKED" in err
    assert stub.created == []
    assert stub.turns == []


def test_an_unreadable_milestone_exits_two_rather_than_reporting_success(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    stub.milestone_response = _one_issue().model_copy(
        update={
            "status": MilestonesStatus.ERROR,
            "issues": [],
            "error": "Linear could not be reached.",
        }
    )

    assert main(_milestone_argv(tmp_path)) == 2

    err = capsys.readouterr().err
    assert "an empty run and a failed read must not look the same" in err
    assert stub.turns == []


def test_a_milestone_spec_without_a_team_names_the_missing_flag(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [a for a in _milestone_argv(tmp_path) if a not in ("--team", "operations")]

    assert main(argv) == 2
    assert "--team" in capsys.readouterr().err
    assert stub.scope_reads == []


def test_a_malformed_milestone_id_is_refused_before_the_server_is_asked(
    tmp_path: Path, stub: StubClient, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _milestone_argv(tmp_path)
    argv[argv.index("--source") + 1] = f"{SOURCE_PREFIX}../../../agent-sessions"

    assert main(argv) == 2
    assert "not a milestone id" in capsys.readouterr().err
    assert stub.scope_reads == []


def test_the_help_offers_a_milestone_and_promises_no_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    once = build_parser()._subparsers._group_actions[0].choices["once"]  # type: ignore[union-attr]
    helps = {action.dest: action.help or "" for action in once._actions}

    with pytest.raises(SystemExit):
        main(["once", "--help"])
    # argparse rewraps and hyphen-breaks, so the flag strings are read from the
    # parser and only the sentences that survive wrapping are read from stdout.
    rendered = " ".join(capsys.readouterr().out.split())

    assert f"{SOURCE_PREFIX}<id>" in helps["source"]
    assert "never writes to Linear" in helps["source"]
    assert "--team" in rendered
    assert "409" in helps["team"]
    assert "Nothing is written back to the source" in " ".join(
        (once.description or "").split()
    )


# ---------------------------------------------------------------------------
# The invocation section 14 published, after the ledger moved onto the server
# ---------------------------------------------------------------------------


SLICE_ONE_ARGV = [
    "once",
    "--source",
    "file://tasks.yaml",
    "--agent",
    "researcher",
    "--max-tasks",
    "3",
    "--dry-run",
]
"""Section 14's slice-1 invocation, character for character.

The plan promised this would still be the invocation once ``agent_tasks``
replaced the SQLite ledger. A published command line is a contract with
whoever wrote it into a cron entry, and "the upgrade path is additive" is only
true if this still parses to the same thing.
"""


def test_the_slice_one_invocation_still_parses_to_the_same_run() -> None:
    parsed = build_parser().parse_args(SLICE_ONE_ARGV)

    assert parsed.command == "once"
    assert parsed.source == "file://tasks.yaml"
    assert parsed.agent == "researcher"
    assert parsed.max_tasks == 3
    assert parsed.dry_run is True
    assert parsed.ledger is None, "the ledger is no longer something slice 1 had to name"


def test_the_default_ledger_is_the_server_and_the_flag_opts_out_of_it(
    tmp_path: Path,
) -> None:
    """``--ledger`` inverted its meaning; it did not change the signature.

    Without it the claim is a row in ``agent_tasks``: atomic, leased, and
    reclaimable from a dispatcher that died. With it the claim is a local file
    that coordinates nothing, which is what slice 1 shipped and what an
    offline poke at a YAML file still wants.
    """
    default = DispatchOptions(
        source_spec="file://tasks.yaml",
        agent_name="researcher",
        base_url="http://localhost:8000",
        api_key="local-agent-key",
    )
    opted_out = DispatchOptions(
        source_spec="file://tasks.yaml",
        agent_name="researcher",
        base_url="http://localhost:8000",
        api_key="local-agent-key",
        ledger_path=tmp_path / "claims.sqlite3",
    )

    assert default.ledger_path is None
    assert "agent_tasks" in _describe_ledger(default)
    assert "coordinates nothing" in _describe_ledger(opted_out)


def test_the_slice_one_flags_all_still_exist_with_the_same_names() -> None:
    """A flag that quietly disappeared is a cron entry that quietly stops."""
    once = build_parser()._subparsers._group_actions[0].choices["once"]  # type: ignore[union-attr]
    flags = {
        option
        for action in once._actions
        for option in action.option_strings
    }

    assert {
        "--source",
        "--agent",
        "--max-tasks",
        "--dry-run",
        "--no-dry-run",
        "--ledger",
        "--server",
        "--brief",
        "--print-envelope",
        "--delete-sessions",
    } <= flags


# =============================================================================
# --workflow, the flag a chain is asked for by
# =============================================================================
#
# ``--agent`` did not go away and did not change meaning for the run it already
# had. What it lost is the ability to be the *only* way an agent is chosen: a
# configured workflow names its own agents server-side, and this flag now fills
# exactly one gap, the implicit one-step plan.


def test_the_workflow_flag_reaches_the_run() -> None:
    parsed = build_parser().parse_args(
        ["once", "--source", "file://tasks.yaml", "--workflow", "research-then-write"]
    )

    assert parsed.workflow == "research-then-write"
    assert parsed.agent is None


def test_neither_an_agent_nor_a_workflow_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A task whose workflow pins no agent and whose team has no default has
    nobody to run it, and this process will not choose one."""
    source = tmp_path / "tasks.yaml"
    source.write_text(ONE_ITEM, encoding="utf-8")

    code = main(["once", "--source", f"file://{source}"])

    assert code == 2
    assert "Pass --agent, --workflow, or both" in capsys.readouterr().err


def test_a_workflow_with_the_local_ledger_is_refused_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stub: StubClient
) -> None:
    """A chain needs ``agent_task_steps`` to carry one agent's report to the
    next. The local file records one session and one output per item, so the
    second agent would be handed a report its own had already overwritten."""
    source = tmp_path / "tasks.yaml"
    source.write_text(ONE_ITEM, encoding="utf-8")

    code = main(
        [
            "once",
            "--source",
            f"file://{source}",
            "--workflow",
            "research-then-write",
            "--ledger",
            str(tmp_path / "claims.sqlite3"),
        ]
    )

    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err
    assert stub.turns == [], "nothing was spent on a run that could not be recorded"


def test_both_flags_together_are_allowed(tmp_path: Path) -> None:
    """``--agent`` still fills a single unresolved step of an implicit plan, so
    an operator running a mixed batch may legitimately pass both."""
    parsed = build_parser().parse_args(
        [
            "once",
            "--source",
            "file://tasks.yaml",
            "--agent",
            "marketing_researcher",
            "--workflow",
            "research-then-write",
        ]
    )
    options = DispatchOptions(
        source_spec=parsed.source,
        agent_name=parsed.agent,
        workflow_key=parsed.workflow,
        base_url="http://localhost:8000",
        api_key="local-agent-key",
    )

    assert options.agent_name == "marketing_researcher"
    assert options.workflow_key == "research-then-write"


def test_the_help_says_the_workflow_is_where_the_agents_come_from(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["once", "--help"])

    # argparse rewraps help text, so the phrasing is checked against a
    # single-spaced version rather than against the terminal layout.
    out = " ".join(capsys.readouterr().out.split())
    assert "--workflow" in out
    assert "PUT /agent-workflows/{key}" in out
    assert "never from here and never from the issue" in out
