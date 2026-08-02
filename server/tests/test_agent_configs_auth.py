"""Who may read an agent's configuration, and who may change one.

The split is not symmetric and neither half is arbitrary.

**Read is AUTHENTICATED because delivery is a read.** The agent process fetches
its own configuration on the refresh loop under an ordinary agent key. Making
this ADMIN would put an admin key in every agent process, which is a worse
posture than the exposure it prevents. The exposure it accepts is real and is
asserted below rather than glossed: any key in a namespace reads every other
agent's prompt and its full history.

**Write is ADMIN on two independent grounds.** The body lands in a field
``extract_request_text`` never reads, so whoever writes here writes text no
control in the deployment will ever evaluate - a lower-privileged write would
override ADMIN-authored control policy with text no guardrail sees. And the
model spends the operator's quota on every turn of every session, indefinitely.

**The allowlist route takes the write operation, not the read one.** It
enumerates the deployment's whole vendor inventory across every namespace. At
read tier one compromised agent process key in any namespace would be
cross-tenant reconnaissance about which vendors the operator has relationships
with.
"""

from __future__ import annotations

import uuid

import pytest
from agent_control_models.agent_configs import AgentModelOption
from fastapi.testclient import TestClient

from agent_control_server.auth_framework.core import Operation
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.config import model_settings

_MODEL = AgentModelOption(
    id="gpt-5.4-mini",
    label="GPT 5.4 mini",
    provider="openai_compatible",
    cost_tier="economy",
)


def _agent_name() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def agent(client: TestClient) -> str:
    name = _agent_name()
    resp = client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": name,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert resp.status_code == 200, resp.text
    return name


@pytest.fixture()
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_settings, "allowlist", [_MODEL])


def _url(agent_name: str, suffix: str = "") -> str:
    return f"/api/v1/agents/{agent_name}/config{suffix}"


# ---------------------------------------------------------------------------
# Registration in the access table
# ---------------------------------------------------------------------------


def test_both_operations_are_registered_at_the_tiers_the_design_names() -> None:
    """An unregistered operation makes the provider raise on first use.

    ``test_auth_framework.py`` already fails on a missing entry, so this case is
    not about coverage. It is about the *tier*: someone lowering the write to
    AUTHENTICATED to make the feature usable for more people would pass every
    other test in the suite.
    """
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_CONFIGS_READ] is (
        AccessLevel.AUTHENTICATED
    )
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_CONFIGS_WRITE] is AccessLevel.ADMIN


def test_the_write_sits_at_the_same_tier_as_the_repos_other_runtime_writes() -> None:
    """Every write that changes what a deployed agent does is ADMIN here."""
    for operation in (
        Operation.CONTROLS_CREATE,
        Operation.AGENT_RUNTIMES_WRITE,
        Operation.AGENT_CONFIGS_WRITE,
    ):
        assert DEFAULT_OPERATION_ACCESS[operation] is AccessLevel.ADMIN


# ---------------------------------------------------------------------------
# A non-admin key
# ---------------------------------------------------------------------------


def test_a_non_admin_key_can_read_the_configuration(
    client: TestClient, non_admin_client: TestClient, agent: str
) -> None:
    """This is the delivery path, not a convenience."""
    client.put(_url(agent), json={"body": "Managed.", "expected_version": 0})

    resp = non_admin_client.get(_url(agent))
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == "Managed."


def test_a_non_admin_key_can_read_the_full_version_history(
    client: TestClient, non_admin_client: TestClient, agent: str
) -> None:
    """Anyone who can see what an agent runs today can see what it ran before.

    Stated rather than glossed: this is the exposure the read tier accepts, and
    because clearing preserves history it outlives the decision to remove a
    prompt.
    """
    client.put(_url(agent), json={"body": "Secret-ish prompt.", "expected_version": 0})
    client.post(_url(agent, ":clear-prompt"), json={"expected_version": 1})

    listed = non_admin_client.get(_url(agent, "/versions"))
    assert listed.status_code == 200, listed.text
    assert [v["version_num"] for v in listed.json()["versions"]] == [2, 1]

    detail = non_admin_client.get(_url(agent, "/versions/1"))
    assert detail.status_code == 200, detail.text
    assert detail.json()["version"]["body"] == "Secret-ish prompt."


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("put", "", {"body": "Mine now.", "expected_version": 0}),
        ("patch", "", {"prompt_enabled": False, "expected_version": 0}),
        ("post", ":clear-prompt", {"expected_version": 0}),
        ("post", ":clear-model", {"expected_version": 0}),
        ("post", "/versions/1:restore", {"expected_version": 0}),
    ],
)
def test_every_write_route_refuses_a_non_admin_key(
    non_admin_client: TestClient,
    agent: str,
    method: str,
    suffix: str,
    payload: dict[str, object],
) -> None:
    resp = getattr(non_admin_client, method)(_url(agent, suffix), json=payload)
    assert resp.status_code == 403, resp.text


def test_a_non_admin_write_changes_nothing(
    client: TestClient, non_admin_client: TestClient, agent: str
) -> None:
    """A 403 that still wrote would be worse than no check at all."""
    client.put(_url(agent), json={"body": "Admin authored.", "expected_version": 0})

    non_admin_client.put(
        _url(agent), json={"body": "Not authorized.", "expected_version": 1}
    )

    assert client.get(_url(agent)).json()["body"] == "Admin authored."
    assert client.get(_url(agent)).json()["current_version"] == 1


def test_the_allowlist_route_refuses_a_non_admin_key(
    non_admin_client: TestClient, allowlist: None
) -> None:
    """Enumerating the operator's vendor inventory is not a read-tier answer."""
    assert non_admin_client.get("/api/v1/agent-models").status_code == 403


def test_an_unauthenticated_caller_reaches_nothing(
    unauthenticated_client: TestClient, agent: str
) -> None:
    assert unauthenticated_client.get(_url(agent)).status_code == 401
    assert (
        unauthenticated_client.put(
            _url(agent), json={"body": "Anonymous.", "expected_version": 0}
        ).status_code
        == 401
    )
    assert unauthenticated_client.get("/api/v1/agent-models").status_code == 401
