"""A refreshed deep link must get its own page's shell, not the home page.

The exported UI ships a dynamic route as one literal bracket directory -
``teams/[slug]/index.html`` - and the server is the router: every concrete
``/teams/<x>`` must land on that shell. Before this, an unknown slug fell
through to the root ``index.html``, the client booted the home page, and every
refresh of a team page bounced the operator back to the start.
"""

from __future__ import annotations

from pathlib import Path

from agent_control_server.ui_assets import resolve_ui_asset_path


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "ui-dist"
    (dist / "teams" / "[slug]").mkdir(parents=True)
    (dist / "index.html").write_text("root")
    (dist / "teams" / "index.html").write_text("teams list")
    (dist / "teams" / "[slug]" / "index.html").write_text("team shell")
    return dist


def test_a_concrete_slug_resolves_to_the_bracket_shell(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    for path in ("/teams/engineering", "/teams/engineering/", "/teams/marketing"):
        resolved = resolve_ui_asset_path(dist, path)
        assert resolved is not None and resolved.read_text() == "team shell", path


def test_the_static_routes_keep_winning_over_the_dynamic_one(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    assert resolve_ui_asset_path(dist, "/teams").read_text() == "teams list"
    assert resolve_ui_asset_path(dist, "/").read_text() == "root"


def test_an_unknown_root_path_still_falls_back_to_the_entrypoint(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    assert resolve_ui_asset_path(dist, "/nonsense").read_text() == "root"


def test_two_bracket_directories_refuse_rather_than_guess(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    (dist / "teams" / "[other]").mkdir()
    (dist / "teams" / "[other]" / "index.html").write_text("ambiguous")
    assert resolve_ui_asset_path(dist, "/teams/engineering").read_text() == "root"


def test_a_deeper_path_under_the_shell_resolves_too(tmp_path: Path) -> None:
    """/teams/<x>/anything has no deeper shell, so the slug shell serves it."""
    dist = _dist(tmp_path)
    resolved = resolve_ui_asset_path(dist, "/teams/engineering/settings")
    assert resolved is not None and resolved.read_text() == "root"
