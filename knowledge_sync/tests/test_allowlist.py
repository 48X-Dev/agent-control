"""The allowlist refuses, because under a classic PAT nothing downstream of it does."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_control_knowledge_sync.allowlist import (
    AllowlistError,
    RepoRef,
    load_allowlist,
    parse_allowlist,
    parse_repo,
)

GOOD = """
github:
  repos:
    - repo: earlycore/agent-control
    - repo: earlycore/handbook
      include_paths:
        - runbooks
        - "reference/policies/"
      github_issues_enabled: true
"""


def _code(raw: str) -> str:
    with pytest.raises(AllowlistError) as caught:
        parse_allowlist(raw)
    return caught.value.code


def test_a_good_file_parses_into_repo_configs() -> None:
    configs = parse_allowlist(GOOD)
    assert [item.repo.full_name for item in configs] == [
        "earlycore/agent-control",
        "earlycore/handbook",
    ]
    assert configs[0].include_paths == ()
    assert configs[0].github_issues_enabled is False
    assert configs[1].include_paths == ("runbooks", "reference/policies")
    assert configs[1].github_issues_enabled is True


def test_full_name_is_the_owner_and_the_name() -> None:
    assert RepoRef(owner="earlycore", name="agent-control").full_name == "earlycore/agent-control"


class TestNothingMeansNothing:
    """An absent, empty or sectionless allowlist indexes nothing. Never everything."""

    def test_a_missing_file_indexes_nothing(self, tmp_path: Path) -> None:
        assert load_allowlist(tmp_path / "absent.yaml") == ()

    def test_an_empty_file_indexes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "knowledge.yaml"
        path.write_text("", encoding="utf-8")
        assert load_allowlist(path) == ()

    def test_a_comment_only_file_indexes_nothing(self) -> None:
        assert parse_allowlist("# nothing listed yet\n") == ()

    def test_no_github_section_indexes_nothing(self) -> None:
        assert parse_allowlist("github:\n") == ()

    def test_an_empty_repo_list_indexes_nothing(self) -> None:
        assert parse_allowlist("github:\n  repos: []\n") == ()

    def test_an_unreadable_path_is_refused_rather_than_read_as_empty(self, tmp_path: Path) -> None:
        with pytest.raises(AllowlistError) as caught:
            load_allowlist(tmp_path)
        assert caught.value.code == "allowlist_unreadable"


class TestUnknownKeysAreRefused:
    """A misspelled filter that is ignored is a filter that is off."""

    def test_an_unknown_top_level_key(self) -> None:
        assert _code("gihtub:\n  repos: []\n") == "allowlist_unknown_key"

    def test_an_unknown_key_under_github(self) -> None:
        assert _code("github:\n  repositories: []\n") == "allowlist_unknown_key"

    def test_a_misspelled_include_paths_does_not_silently_widen(self) -> None:
        raw = "github:\n  repos:\n    - repo: a/b\n      include_path: [docs]\n"
        assert _code(raw) == "allowlist_unknown_key"

    def test_the_message_names_the_key_and_the_known_ones(self) -> None:
        with pytest.raises(AllowlistError) as caught:
            parse_allowlist("github:\n  repos:\n    - repo: a/b\n      trust: workspace\n")
        message = str(caught.value)
        assert "trust" in message
        assert "include_paths" in message


class TestOnlyAnExplicitOwnerName:
    @pytest.mark.parametrize(
        "value",
        [
            "earlycore/*",
            "earlycore/agent-*",
            "*/agent-control",
            "earlycore/[a-z]*",
            "earlycore",
            "earlycore/",
            "/agent-control",
            "earlycore/agent/control",
            "",
        ],
    )
    def test_a_wildcard_or_org_wide_form_is_refused(self, value: str) -> None:
        with pytest.raises(AllowlistError) as caught:
            parse_repo(value)
        assert caught.value.code == "allowlist_repo_form"

    @pytest.mark.parametrize("value", ["a b/c", "earlycore/..", "-lead/repo", "owner/re po"])
    def test_a_name_that_is_not_a_github_name_is_refused(self, value: str) -> None:
        with pytest.raises(AllowlistError) as caught:
            parse_repo(value)
        assert caught.value.code == "allowlist_repo_form"

    @pytest.mark.parametrize(
        "value", ["earlycore/agent-control", "a/b", "Early-Core/agent_control.v2"]
    )
    def test_an_explicit_pair_is_admitted(self, value: str) -> None:
        owner, _, name = value.partition("/")
        assert parse_repo(value) == RepoRef(owner=owner, name=name)

    def test_a_non_string_repo_is_refused(self) -> None:
        assert _code("github:\n  repos:\n    - repo: 12\n") == "allowlist_repo_form"

    def test_an_entry_without_a_repo_key_is_refused(self) -> None:
        assert _code("github:\n  repos:\n    - include_paths: [docs]\n") == "allowlist_bad_value"

    def test_the_same_repo_twice_is_refused(self) -> None:
        raw = "github:\n  repos:\n    - repo: a/b\n    - repo: A/B\n"
        assert _code(raw) == "allowlist_duplicate_repo"


class TestIncludePaths:
    @pytest.mark.parametrize(
        "value",
        ["../secrets", "docs/../../etc", "runbooks/*", "a\\b", "docs//plans", "docs/."],
    )
    def test_a_traversal_or_glob_is_refused(self, value: str) -> None:
        raw = f"github:\n  repos:\n    - repo: a/b\n      include_paths: ['{value}']\n"
        assert _code(raw) == "allowlist_include_path"

    @pytest.mark.parametrize("value", ["node_modules", "packages/x/vendor", "dist"])
    def test_naming_an_always_refused_directory_fails_loudly(self, value: str) -> None:
        raw = f"github:\n  repos:\n    - repo: a/b\n      include_paths: ['{value}']\n"
        assert _code(raw) == "allowlist_include_path"

    def test_surrounding_slashes_are_normalized_away(self) -> None:
        raw = "github:\n  repos:\n    - repo: a/b\n      include_paths: ['/runbooks/']\n"
        assert parse_allowlist(raw)[0].include_paths == ("runbooks",)

    def test_a_non_list_is_refused(self) -> None:
        raw = "github:\n  repos:\n    - repo: a/b\n      include_paths: docs\n"
        assert _code(raw) == "allowlist_bad_value"


class TestMalformedDocuments:
    def test_broken_yaml_is_refused(self) -> None:
        assert _code("github:\n  repos:\n  - repo: 'a/b\n") == "allowlist_malformed"

    def test_a_top_level_list_is_refused(self) -> None:
        assert _code("- earlycore/agent-control\n") == "allowlist_malformed"

    def test_a_repos_value_that_is_not_a_list_is_refused(self) -> None:
        assert _code("github:\n  repos: earlycore/agent-control\n") == "allowlist_bad_value"

    def test_an_entry_that_is_not_a_mapping_is_refused(self) -> None:
        assert _code("github:\n  repos:\n    - earlycore/agent-control\n") == "allowlist_bad_value"

    def test_a_stringly_typed_flag_is_refused_rather_than_read_as_true(self) -> None:
        raw = "github:\n  repos:\n    - repo: a/b\n      github_issues_enabled: 'false'\n"
        assert _code(raw) == "allowlist_bad_value"


def test_the_example_at_the_repo_root_parses() -> None:
    """The shipped example is the operator's starting point, so it must load."""
    example = Path(__file__).resolve().parents[2] / "knowledge.yaml.example"
    assert load_allowlist(example) == ()
