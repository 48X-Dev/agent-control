"""Every ambiguity the fleet schema refuses, one test each."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_control_fleet.config import (
    FleetConfigError,
    load_fleet_config,
    parse_fleet_config,
)

MINIMAL = """
version: 1
image: agent-control-executor:local
agents:
  - agent_name: marketing_researcher
"""


def _refusal(raw: str) -> FleetConfigError:
    with pytest.raises(FleetConfigError) as caught:
        parse_fleet_config(raw)
    return caught.value


def test_minimal_file_parses() -> None:
    config = parse_fleet_config(MINIMAL)
    assert config.image == "agent-control-executor:local"
    assert [spec.agent_name for spec in config.agents] == ["marketing_researcher"]


def test_web_tools_defaults_on_and_defaults_block_flips_it() -> None:
    assert parse_fleet_config(MINIMAL).agents[0].web_tools is True
    flipped = parse_fleet_config(MINIMAL + "\ndefaults:\n  web_tools: false\n")
    assert flipped.agents[0].web_tools is False


def test_per_agent_web_tools_overrides_the_default() -> None:
    config = parse_fleet_config(
        """
version: 1
image: executor:local
defaults:
  web_tools: true
agents:
  - agent_name: marketing_researcher
  - agent_name: sales_outreach_drafter
    web_tools: false
"""
    )
    assert [spec.web_tools for spec in config.agents] == [True, False]


def test_a_bool_shaped_string_is_refused_rather_than_read_as_true() -> None:
    error = _refusal(MINIMAL.rstrip() + '\n    web_tools: "false"\n')
    assert error.code == "fleet_bad_value"
    assert "web_tools" in str(error)


def test_an_unknown_key_names_itself_and_the_known_set() -> None:
    error = _refusal(MINIMAL.rstrip() + "\n    web_tool: false\n")
    assert error.code == "fleet_unknown_key"
    assert "'web_tool'" in str(error)
    assert "agent_name" in str(error)


def test_an_unknown_top_level_key_is_refused() -> None:
    assert _refusal(MINIMAL + "\nimages: other\n").code == "fleet_unknown_key"


def test_a_duplicate_agent_names_the_first_occurrence() -> None:
    error = _refusal(MINIMAL + "  - agent_name: marketing_researcher\n")
    assert error.code == "fleet_duplicate_agent"
    assert "agents[0]" in str(error)


def test_a_missing_version_is_refused_rather_than_assumed() -> None:
    assert _refusal("image: executor:local\nagents: []\n").code == "fleet_bad_value"


def test_a_string_version_is_not_the_integer_version() -> None:
    assert _refusal('version: "1"\nimage: executor:local\n').code == "fleet_bad_value"


def test_an_unsupported_version_is_refused() -> None:
    assert _refusal("version: 2\nimage: executor:local\n").code == "fleet_bad_value"


def test_a_missing_image_is_refused() -> None:
    assert _refusal("version: 1\nagents: []\n").code == "fleet_bad_value"


@pytest.mark.parametrize(
    "agent_name",
    ["google-adk-plugin", "marketing:researcher", "Marketing_Researcher"],
)
def test_a_name_adk_cannot_route_is_refused(agent_name: str) -> None:
    error = _refusal(f"version: 1\nimage: e:local\nagents:\n  - agent_name: {agent_name}\n")
    assert error.code == "fleet_agent_name"


def test_a_name_shorter_than_the_server_floor_is_refused() -> None:
    error = _refusal("version: 1\nimage: e:local\nagents:\n  - agent_name: short\n")
    assert error.code == "fleet_agent_name"


def test_no_agents_is_a_fleet_of_none_rather_than_an_error() -> None:
    assert parse_fleet_config("version: 1\nimage: executor:local\n").agents == ()


def test_malformed_yaml_names_the_file() -> None:
    error = _refusal("version: 1\n  image: [")
    assert error.code == "fleet_malformed"


def test_an_absent_file_is_a_refusal_not_a_default(tmp_path: Path) -> None:
    with pytest.raises(FleetConfigError) as caught:
        load_fleet_config(tmp_path / "fleet.yaml")
    assert caught.value.code == "fleet_absent"


def test_the_container_name_hyphenates_the_underscored_agent_name() -> None:
    spec = parse_fleet_config(MINIMAL).agents[0]
    assert spec.container_name == "ac-executor-marketing-researcher"


def test_the_checked_in_example_parses() -> None:
    example = Path(__file__).resolve().parents[2] / "fleet.yaml.example"
    config = load_fleet_config(example)
    assert config.agents
    assert any(spec.web_tools is False for spec in config.agents)
