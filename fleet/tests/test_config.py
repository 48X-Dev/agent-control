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
groups:
  - name: marketing
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


def test_per_group_web_tools_overrides_the_default() -> None:
    config = parse_fleet_config(
        """
version: 1
image: executor:local
defaults:
  web_tools: true
groups:
  - name: marketing
    agents:
      - agent_name: marketing_researcher
  - name: sales
    agents:
      - agent_name: sales_outreach_drafter
        web_tools: false
"""
    )
    assert [spec.web_tools for spec in config.agents] == [True, False]
    assert [group.web_tools for group in config.groups] == [True, False]


def test_a_group_whose_members_disagree_on_egress_is_refused() -> None:
    """Section 3.3: they share a network and a PID namespace, so the difference is not real."""

    error = _refusal(
        """
version: 1
image: executor:local
groups:
  - name: marketing
    agents:
      - agent_name: marketing_researcher
      - agent_name: marketing_copywriter
        web_tools: false
"""
    )
    assert error.code == "fleet_group_mixed_egress"
    assert "'marketing'" in str(error)
    assert "marketing_researcher" in str(error)
    assert "marketing_copywriter" in str(error)


def test_a_bool_shaped_string_is_refused_rather_than_read_as_true() -> None:
    error = _refusal(MINIMAL.rstrip() + '\n        web_tools: "false"\n')
    assert error.code == "fleet_bad_value"
    assert "web_tools" in str(error)


def test_an_unknown_key_names_itself_and_the_known_set() -> None:
    error = _refusal(MINIMAL.rstrip() + "\n        web_tool: false\n")
    assert error.code == "fleet_unknown_key"
    assert "'web_tool'" in str(error)
    assert "agent_name" in str(error)


def test_an_unknown_group_key_is_refused() -> None:
    error = _refusal(MINIMAL.rstrip() + "\n    agent: other\n")
    assert error.code == "fleet_unknown_key"
    assert "'agent'" in str(error)


def test_an_unknown_top_level_key_is_refused() -> None:
    assert _refusal(MINIMAL + "\nimages: other\n").code == "fleet_unknown_key"


def test_a_duplicate_agent_names_the_first_occurrence_across_groups() -> None:
    error = _refusal(
        MINIMAL + "  - name: sales\n    agents:\n      - agent_name: marketing_researcher\n"
    )
    assert error.code == "fleet_duplicate_agent"
    assert "groups[0].agents[0]" in str(error)


def test_a_duplicate_group_name_is_refused() -> None:
    error = _refusal(MINIMAL + "  - name: marketing\n    agents:\n      - agent_name: a_agent_01\n")
    assert error.code == "fleet_duplicate_group"


def test_a_group_with_no_agents_is_refused() -> None:
    error = _refusal("version: 1\nimage: e:local\ngroups:\n  - name: marketing\n    agents: []\n")
    assert error.code == "fleet_bad_value"


def test_a_group_with_no_name_is_refused() -> None:
    error = _refusal(
        "version: 1\nimage: e:local\ngroups:\n  - agents:\n      - agent_name: a_agent_01\n"
    )
    assert error.code == "fleet_group_name"


@pytest.mark.parametrize("name", ["Marketing", "marketing team", "-marketing", "marketing:one"])
def test_a_group_name_no_container_could_carry_is_refused(name: str) -> None:
    error = _refusal(
        f"version: 1\nimage: e:local\ngroups:\n  - name: {name!r}\n"
        "    agents:\n      - agent_name: a_agent_01\n"
    )
    assert error.code == "fleet_group_name"


def test_a_missing_version_is_refused_rather_than_assumed() -> None:
    assert _refusal("image: executor:local\ngroups: []\n").code == "fleet_bad_value"


def test_a_string_version_is_not_the_integer_version() -> None:
    assert _refusal('version: "1"\nimage: executor:local\n').code == "fleet_bad_value"


def test_an_unsupported_version_is_refused() -> None:
    assert _refusal("version: 2\nimage: executor:local\n").code == "fleet_bad_value"


def test_a_missing_image_is_refused() -> None:
    assert _refusal("version: 1\ngroups: []\n").code == "fleet_bad_value"


@pytest.mark.parametrize(
    "agent_name",
    ["google-adk-plugin", "marketing:researcher", "Marketing_Researcher"],
)
def test_a_name_adk_cannot_route_is_refused(agent_name: str) -> None:
    error = _refusal(
        f"version: 1\nimage: e:local\ngroups:\n  - name: g\n"
        f"    agents:\n      - agent_name: {agent_name}\n"
    )
    assert error.code == "fleet_agent_name"


def test_a_name_shorter_than_the_server_floor_is_refused() -> None:
    error = _refusal(
        "version: 1\nimage: e:local\ngroups:\n  - name: g\n"
        "    agents:\n      - agent_name: short\n"
    )
    assert error.code == "fleet_agent_name"


def test_no_groups_is_a_fleet_of_none_rather_than_an_error() -> None:
    assert parse_fleet_config("version: 1\nimage: executor:local\n").groups == ()


def test_malformed_yaml_names_the_file() -> None:
    error = _refusal("version: 1\n  image: [")
    assert error.code == "fleet_malformed"


def test_an_absent_file_is_a_refusal_not_a_default(tmp_path: Path) -> None:
    with pytest.raises(FleetConfigError) as caught:
        load_fleet_config(tmp_path / "fleet.yaml")
    assert caught.value.code == "fleet_absent"


def test_one_agent_per_group_reproduces_the_per_agent_topology() -> None:
    """The generalization has to leave the old shape reachable, byte for byte."""

    config = parse_fleet_config(
        """
version: 1
image: executor:local
groups:
  - name: marketing_researcher
    agents:
      - agent_name: marketing_researcher
  - name: sales_outreach_drafter
    agents:
      - agent_name: sales_outreach_drafter
"""
    )
    assert [group.container_name for group in config.groups] == [
        "ac-executor-marketing-researcher",
        "ac-executor-sales-outreach-drafter",
    ]
    assert [spec.port for spec in config.agents] == [8000, 8000]


def test_ports_are_allocated_per_group_from_8000() -> None:
    config = parse_fleet_config(
        """
version: 1
image: executor:local
groups:
  - name: marketing
    agents:
      - agent_name: marketing_researcher
      - agent_name: marketing_copywriter
  - name: sales
    agents:
      - agent_name: sales_outreach_drafter
"""
    )
    assert [(spec.agent_name, spec.port) for spec in config.agents] == [
        ("marketing_researcher", 8000),
        ("marketing_copywriter", 8001),
        ("sales_outreach_drafter", 8000),
    ]


def test_a_placement_addresses_the_group_and_ports_the_agent() -> None:
    config = parse_fleet_config(
        """
version: 1
image: executor:local
groups:
  - name: marketing
    agents:
      - agent_name: marketing_researcher
      - agent_name: marketing_copywriter
"""
    )
    assert [placement.base_url("192.168.64.7") for placement in config.placements] == [
        "http://192.168.64.7:8000",
        "http://192.168.64.7:8001",
    ]


def test_the_checked_in_example_parses() -> None:
    example = Path(__file__).resolve().parents[2] / "fleet.yaml.example"
    config = load_fleet_config(example)
    assert config.agents
    assert any(spec.web_tools is False for spec in config.agents)
    assert any(len(group.agents) > 1 for group in config.groups)
