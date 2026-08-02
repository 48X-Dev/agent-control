"""The closed write path on ``model_id``, held as an invariant rather than a habit.

Section 6 of the design deliberately refuses a database constraint enumerating
valid model ids: the allowlist is server configuration an operator edits without
a migration, and a membership constraint would turn removing one env line into a
deployment that will not start against existing rows. Shape is invariant,
membership is not.

The cost of that decision is that "every write is validated" becomes a property
of the *code*. So it is stated as a rule with tests behind it: ``model_id`` is
writable only through the set route and the restore route, both of which call
``AgentConfigService.validate_model_allowed``. Any future template, clone,
team-provisioning or import path routes through the same validator or it does
not ship.

This matters more than it looks. On the shipped default configuration
``NoAuthProvider`` is installed and authorizes ADMIN for everyone, so a missed
call site is not an admin write of an arbitrary model id. It is an
**anonymous** one, and an arbitrary model id is a choice of vendor.

``control_templates`` already exists as the shape somebody will copy.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest
from agent_control_models.agent_configs import AgentModelOption
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from agent_control_server.config import model_settings
from agent_control_server.services import agent_configs as agent_configs_service

_ECONOMY = AgentModelOption(
    id="gpt-5.4-mini",
    label="GPT 5.4 mini",
    provider="openai_compatible",
    cost_tier="economy",
)

_SERVER_PACKAGE = Path(agent_configs_service.__file__).resolve().parents[1]
_SERVICE_MODULE = Path(agent_configs_service.__file__).resolve()

#: The only two methods permitted to assign the column, and the validator both
#: of them must call.
_PERMITTED_WRITERS = {"set_config", "restore_version"}
_VALIDATOR = "validate_model_allowed"


@pytest.fixture(autouse=True)
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY])


def _agent_name() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


def _init_agent(client: TestClient, agent_name: str, **extra: Any) -> Any:
    payload: dict[str, Any] = {
        "agent": {
            "agent_name": agent_name,
            "agent_description": "test agent",
            "agent_version": "1.0",
        },
        "steps": [],
    }
    payload.update(extra)
    return client.post("/api/v1/agents/initAgent", json=payload)


@pytest.fixture()
def agent(client: TestClient) -> str:
    name = _agent_name()
    resp = _init_agent(client, name)
    assert resp.status_code == 200, resp.text
    return name


def _url(agent_name: str, suffix: str = "") -> str:
    return f"/api/v1/agents/{agent_name}/config{suffix}"


# ---------------------------------------------------------------------------
# Registration never touches the column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conflict_mode", [None, "overwrite", "strict"])
def test_re_registering_an_agent_leaves_its_configuration_untouched(
    client: TestClient, agent: str, conflict_mode: str | None
) -> None:
    """The agent process restarting must not disturb what an admin authored.

    ``initAgent`` is AUTHENTICATED and the SDK's ``init()`` defaults to
    overwriting, so it runs on every process start. If it could reach this row,
    an ordinary agent key would be able to change the model on every restart,
    and an operator's saved prompt would evaporate the next time somebody
    redeployed.
    """
    client.put(
        _url(agent),
        json={
            "body": "Admin authored.",
            "model_id": _ECONOMY.id,
            "expected_version": 0,
        },
    )
    before = client.get(_url(agent)).json()

    extra: dict[str, Any] = {}
    if conflict_mode is not None:
        extra["conflict_mode"] = conflict_mode
    resp = _init_agent(client, agent, **extra)
    assert resp.status_code in (200, 409), resp.text

    after = client.get(_url(agent)).json()
    assert after["model_id"] == before["model_id"] == _ECONOMY.id
    assert after["body"] == before["body"] == "Admin authored."
    assert after["current_version"] == before["current_version"]
    assert after["etag"] == before["etag"]


def test_a_forced_replacement_registration_leaves_the_configuration_untouched(
    client: TestClient, agent: str
) -> None:
    """``force_replace`` is the most destructive thing registration can do.

    It still has no business here: the configuration belongs to the operator,
    not to whatever the process last declared.
    """
    client.put(
        _url(agent),
        json={"model_id": _ECONOMY.id, "expected_version": 0},
    )

    resp = _init_agent(client, agent, force_replace=True)
    assert resp.status_code == 200, resp.text

    assert client.get(_url(agent)).json()["model_id"] == _ECONOMY.id


def test_the_registration_payload_cannot_smuggle_a_model_id(
    client: TestClient, agent: str, db_engine: Engine
) -> None:
    """``initAgent`` never carries ``model_id``, and adding it later must fail.

    Routing the field through registration would put it behind an AUTHENTICATED
    operation, which is the whole thing the ADMIN write tier exists to prevent.
    """
    resp = _init_agent(
        client,
        agent,
        agent_model_id="bedrock/anthropic.claude-v2",
    )
    assert resp.status_code in (200, 422), resp.text

    with db_engine.begin() as conn:
        stored = conn.execute(
            text("SELECT model_id FROM agent_configs WHERE agent_name = :n"),
            {"n": agent},
        ).fetchall()
    assert all(row[0] is None for row in stored)


# ---------------------------------------------------------------------------
# The invariant itself
# ---------------------------------------------------------------------------


def _assignments_to_model_id(path: Path) -> list[tuple[str, int, bool]]:
    """Every ``<something>.model_id = ...``, its function, and whether it nulls."""
    tree = ast.parse(path.read_text())
    found: list[tuple[str, int, bool]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def _visit_function(self, node: Any) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "model_id":
                    writes_none = (
                        isinstance(node.value, ast.Constant) and node.value.value is None
                    )
                    found.append(
                        (
                            self.scope[-1] if self.scope else "<module>",
                            node.lineno,
                            writes_none,
                        )
                    )
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def test_only_two_methods_in_the_whole_server_assign_model_id() -> None:
    """A future template or clone path that forgets the validator fails here.

    This is the test the design calls the highest-leverage one in the server
    suite, and the reason is the tier collapse: under the shipped default
    provider a missed call site is an anonymous write of an arbitrary vendor
    selector.

    If you are reading this because it failed: the fix is to route the new write
    through ``AgentConfigService.validate_model_allowed`` and add its method
    name to ``_PERMITTED_WRITERS`` - not to delete the assertion.
    """
    offenders: list[str] = []
    for path in sorted(_SERVER_PACKAGE.rglob("*.py")):
        for function_name, lineno, writes_none in _assignments_to_model_id(path):
            # Nulling is always safe: it selects no vendor and it is exactly what
            # the clear route means. Only a write that can name a destination has
            # to have gone through the validator.
            if writes_none:
                continue
            if path == _SERVICE_MODULE and function_name in _PERMITTED_WRITERS:
                continue
            offenders.append(f"{path.relative_to(_SERVER_PACKAGE)}:{lineno} in {function_name}")

    assert offenders == [], (
        "model_id is assigned outside the two validated write paths: "
        + ", ".join(offenders)
    )


@pytest.mark.parametrize("method_name", sorted(_PERMITTED_WRITERS))
def test_each_permitted_writer_calls_the_validator(method_name: str) -> None:
    """Being the only writer is not enough; each one must actually validate."""
    tree = ast.parse(_SERVICE_MODULE.read_text())
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )
    calls = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert _VALIDATOR in calls, (
        f"{method_name} assigns model_id without calling {_VALIDATOR}"
    )


def test_the_restore_path_validates_before_it_writes_anything(
    client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore that partially applied would be a rewind nobody could see.

    The refusal has to land before the row is touched, or the prompt half moves
    while the model half does not and the version history records a state that
    never fully existed.
    """
    monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY])
    client.put(
        _url(agent),
        json={"body": "One.", "model_id": _ECONOMY.id, "expected_version": 0},
    )
    client.put(_url(agent), json={"body": "Two.", "expected_version": 1})

    monkeypatch.setattr(model_settings, "allowlist", [])
    resp = client.post(
        _url(agent, "/versions/1:restore"), json={"expected_version": 2}
    )

    assert resp.status_code == 409, resp.text
    after = client.get(_url(agent)).json()
    assert after["body"] == "Two."
    assert after["current_version"] == 2


def test_a_write_that_is_refused_leaves_no_version_row_behind(
    client: TestClient, agent: str
) -> None:
    """Validation runs before the row lock and before any version is appended."""
    client.put(_url(agent), json={"body": "One.", "expected_version": 0})

    refused = client.put(
        _url(agent),
        json={"body": "Two.", "model_id": "not-on-the-list", "expected_version": 1},
    )
    assert refused.status_code == 400, refused.text

    versions = client.get(_url(agent, "/versions")).json()["versions"]
    assert [v["version_num"] for v in versions] == [1]
    assert client.get(_url(agent)).json()["body"] == "One."
