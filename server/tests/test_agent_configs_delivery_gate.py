"""The startup gate: storage is always open, delivery is not.

``AuthSettings.api_key_enabled`` defaults false, which resolves the default
authorizer to ``NoAuthProvider`` and authorizes **every** operation - ADMIN
included - for anyone who can open a TCP connection to the server port. So "the
write is ADMIN" is a claim about a configured server, and the shipped default is
not that server. On it, an anonymous caller could otherwise put text in front of
a running agent that no control evaluates, and point every agent in the
namespace at the priciest model on the operator's own quota.

The gate suppresses the one thing that changes a running agent and nothing else.
Editing, versioning and the audit trail keep working, because a laptop with no
credentials configured is how everybody first meets this feature. That asymmetry
is the whole design, so it is asserted from both sides: what stops, and what
does not.

The local-dev override opens the prompt fully and the model **only at the
economy tier**. One boolean a developer sets on day one should not be the whole
distance between a laptop and unbounded spend on a personal subscription.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_models.agent_configs import AgentModelOption
from fastapi.testclient import TestClient

from agent_control_server import config as server_config
from agent_control_server.config import (
    AuthSettings,
    check_agent_config_startup_requirements,
    model_settings,
    resolve_default_auth_mode,
)

_ECONOMY = AgentModelOption(
    id="gpt-5.4-mini",
    label="GPT 5.4 mini",
    provider="openai_compatible",
    cost_tier="economy",
)
_PREMIUM = AgentModelOption(
    id="gpt-5.6-sol",
    label="GPT 5.6 sol",
    provider="openai_compatible",
    cost_tier="premium",
)
_STANDARD = AgentModelOption(
    id="gemini-2.5-flash",
    label="Gemini 2.5 Flash",
    provider="gemini",
    cost_tier="standard",
)

_OVERRIDE_VAR = "AGENT_CONTROL_AGENT_CONFIG_ALLOW_INSECURE_LOCAL_DEV"


@pytest.fixture(autouse=True)
def restore_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the module-level gate back however this test left it.

    It is resolved once at startup and read on every config resolution, so a
    test that closed it would close it for the rest of the session.
    """
    monkeypatch.setattr(server_config, "AGENT_CONFIG_DELIVERY_ALLOWED", True)
    monkeypatch.setattr(server_config, "AGENT_CONFIG_MODEL_TIER_LIMIT", None)


@pytest.fixture()
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY, _STANDARD, _PREMIUM])


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


def _url(agent_name: str, suffix: str = "") -> str:
    return f"/api/v1/agents/{agent_name}/config{suffix}"


def _get(client: TestClient, agent_name: str) -> dict[str, Any]:
    resp = client.get(_url(agent_name))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _close_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_config, "AGENT_CONFIG_DELIVERY_ALLOWED", False)
    monkeypatch.setattr(server_config, "AGENT_CONFIG_MODEL_TIER_LIMIT", None)


def _open_the_gate_for_local_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_config, "AGENT_CONFIG_DELIVERY_ALLOWED", True)
    monkeypatch.setattr(server_config, "AGENT_CONFIG_MODEL_TIER_LIMIT", "economy")


# ---------------------------------------------------------------------------
# Resolving the gate at startup
# ---------------------------------------------------------------------------


class TestResolvingTheGate:
    @staticmethod
    def _resolve(
        monkeypatch: pytest.MonkeyPatch, *, api_key_enabled: bool, override: str | None
    ) -> tuple[bool, str | None]:
        monkeypatch.delenv("AGENT_CONTROL_AUTH_MODE", raising=False)
        if override is None:
            monkeypatch.delenv(_OVERRIDE_VAR, raising=False)
        else:
            monkeypatch.setenv(_OVERRIDE_VAR, override)
        auth = AuthSettings(_env_file=None)
        auth.api_key_enabled = api_key_enabled
        check_agent_config_startup_requirements(auth=auth)
        return (
            server_config.AGENT_CONFIG_DELIVERY_ALLOWED,
            server_config.AGENT_CONFIG_MODEL_TIER_LIMIT,
        )

    def test_credentials_off_and_no_override_closes_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shipped default. ``api_key_enabled`` is false out of the box."""
        assert self._resolve(monkeypatch, api_key_enabled=False, override=None) == (False, None)

    def test_credentials_on_opens_delivery_with_no_tier_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._resolve(monkeypatch, api_key_enabled=True, override=None) == (True, None)

    @pytest.mark.parametrize("truthy", ["true", "1", "yes", "on", "TRUE"])
    def test_the_local_dev_override_opens_delivery_capped_at_economy(
        self, monkeypatch: pytest.MonkeyPatch, truthy: str
    ) -> None:
        assert self._resolve(monkeypatch, api_key_enabled=False, override=truthy) == (
            True,
            "economy",
        )

    @pytest.mark.parametrize("falsey", ["", "false", "0", "no", "maybe"])
    def test_anything_that_is_not_an_affirmative_leaves_the_gate_closed(
        self, monkeypatch: pytest.MonkeyPatch, falsey: str
    ) -> None:
        """A typo in the env var must not open it."""
        assert self._resolve(monkeypatch, api_key_enabled=False, override=falsey) == (False, None)

    def test_the_override_does_not_apply_a_tier_limit_on_a_credentialed_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting it on a properly configured server must not degrade it."""
        assert self._resolve(monkeypatch, api_key_enabled=True, override="true") == (True, None)

    def test_an_explicit_auth_mode_wins_over_the_api_key_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate has to resolve the authorizer the same way the framework does.

        Duplicating the rule is the risk; disagreeing with it would mean a
        deployment where the gate is open and every operation is anonymous.
        """
        monkeypatch.delenv(_OVERRIDE_VAR, raising=False)
        auth = AuthSettings(_env_file=None)
        auth.api_key_enabled = True

        monkeypatch.setenv("AGENT_CONTROL_AUTH_MODE", "none")
        assert resolve_default_auth_mode(auth) == "none"
        check_agent_config_startup_requirements(auth=auth)
        assert server_config.AGENT_CONFIG_DELIVERY_ALLOWED is False

        monkeypatch.setenv("AGENT_CONTROL_AUTH_MODE", "api_key")
        auth.api_key_enabled = False
        assert resolve_default_auth_mode(auth) == "api_key"
        check_agent_config_startup_requirements(auth=auth)
        assert server_config.AGENT_CONFIG_DELIVERY_ALLOWED is True

    def test_it_warns_rather_than_refusing_to_start(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deliberately unlike the executor, which refuses outright.

        A gated executor is a useless executor. A gated configuration store is
        still a working configuration store, so refusing to start would take the
        editor away from the person who most needs it.
        """
        with caplog.at_level("WARNING"):
            self._resolve(monkeypatch, api_key_enabled=False, override=None)
        assert "AGENT_CONTROL_API_KEY_ENABLED" in caplog.text


# ---------------------------------------------------------------------------
# What the gate suppresses
# ---------------------------------------------------------------------------


class TestGateClosed:
    def test_a_stored_prompt_and_model_both_resolve_to_the_agents_own_code(
        self,
        client: TestClient,
        agent: str,
        allowlist: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client.put(
            _url(agent),
            json={
                "body": "Operator prompt.",
                "model_id": _ECONOMY.id,
                "expected_version": 0,
            },
        )
        _close_the_gate(monkeypatch)

        config = _get(client, agent)
        assert config["body"] == "Operator prompt."
        assert config["model_id"] == _ECONOMY.id
        assert config["prompt_source"] == "code"
        assert config["model_source"] == "code"
        assert config["delivery_state"] == "blocked_insecure_auth"

    def test_storage_versioning_and_history_all_keep_working(
        self,
        client: TestClient,
        agent: str,
        allowlist: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The editor stays fully usable on a laptop with no credentials."""
        _close_the_gate(monkeypatch)

        first = client.put(_url(agent), json={"body": "One.", "expected_version": 0})
        assert first.status_code == 200, first.text
        assert first.json()["prompt_source"] == "code"
        assert first.json()["delivery_state"] == "blocked_insecure_auth"

        second = client.put(_url(agent), json={"body": "Two.", "expected_version": 1})
        assert second.status_code == 200, second.text
        assert second.json()["version_num"] == 2

        versions = client.get(_url(agent, "/versions")).json()["versions"]
        assert [v["version_num"] for v in versions] == [2, 1]

        restored = client.post(_url(agent, "/versions/1:restore"), json={"expected_version": 2})
        assert restored.status_code == 200, restored.text
        assert _get(client, agent)["body"] == "One."

    def test_clearing_still_works_while_delivery_is_gated(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client.put(_url(agent), json={"body": "One.", "expected_version": 0})
        _close_the_gate(monkeypatch)

        resp = client.post(_url(agent, ":clear-prompt"), json={"expected_version": 1})
        assert resp.status_code == 200, resp.text
        assert resp.json()["cleared"] is True

    def test_an_agent_with_no_configuration_still_reports_the_gate(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The banner has to appear before somebody types a prompt, not after."""
        _close_the_gate(monkeypatch)
        assert _get(client, agent)["delivery_state"] == "blocked_insecure_auth"


# ---------------------------------------------------------------------------
# The local-dev override
# ---------------------------------------------------------------------------


class TestLocalDevOverride:
    def test_the_prompt_is_delivered_in_full(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A prompt has no spend attached, so there is no tier to limit."""
        client.put(_url(agent), json={"body": "Local prompt.", "expected_version": 0})
        _open_the_gate_for_local_dev(monkeypatch)

        config = _get(client, agent)
        assert config["prompt_source"] == "managed"

    def test_an_economy_model_is_delivered(
        self,
        client: TestClient,
        agent: str,
        allowlist: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client.put(_url(agent), json={"model_id": _ECONOMY.id, "expected_version": 0})
        _open_the_gate_for_local_dev(monkeypatch)

        config = _get(client, agent)
        assert config["model_source"] == "managed"
        assert config["delivery_state"] == "active"

    @pytest.mark.parametrize("entry", [_STANDARD, _PREMIUM])
    def test_a_model_above_the_economy_tier_is_suppressed(
        self,
        client: TestClient,
        agent: str,
        allowlist: None,
        monkeypatch: pytest.MonkeyPatch,
        entry: AgentModelOption,
    ) -> None:
        """One boolean must not be the whole distance to unbounded spend.

        On an unauthenticated server with the override on, anyone who can reach
        the port is an admin. Without this cap they could point every agent in
        the namespace at the priciest model and leave it there, on somebody's
        personal subscription, with nothing else to turn off.
        """
        client.put(_url(agent), json={"model_id": entry.id, "expected_version": 0})
        _open_the_gate_for_local_dev(monkeypatch)

        config = _get(client, agent)
        assert config["model_id"] == entry.id
        assert config["model_allowed"] is True
        assert config["model_source"] == "code"
        assert config["delivery_state"] == "blocked_insecure_auth"

    def test_the_tier_limit_suppresses_the_model_without_touching_the_prompt(
        self,
        client: TestClient,
        agent: str,
        allowlist: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The banner has to be able to say "prompt yes, model no", truthfully."""
        client.put(
            _url(agent),
            json={
                "body": "Local prompt.",
                "model_id": _PREMIUM.id,
                "expected_version": 0,
            },
        )
        _open_the_gate_for_local_dev(monkeypatch)

        config = _get(client, agent)
        assert config["prompt_source"] == "managed"
        assert config["model_source"] == "code"

    def test_a_credentialed_server_delivers_a_premium_model(
        self,
        client: TestClient,
        agent: str,
        allowlist: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The incentive the tier limit exists to create.

        Turning credentials on is thirty seconds of work and it is the behaviour
        worth encouraging, so it has to actually be the thing that unlocks the
        premium tier.
        """
        client.put(_url(agent), json={"model_id": _PREMIUM.id, "expected_version": 0})
        monkeypatch.setattr(server_config, "AGENT_CONFIG_DELIVERY_ALLOWED", True)
        monkeypatch.setattr(server_config, "AGENT_CONFIG_MODEL_TIER_LIMIT", None)

        assert _get(client, agent)["model_source"] == "managed"


# ---------------------------------------------------------------------------
# The gate is only worth having if startup runs it
# ---------------------------------------------------------------------------


async def test_running_startup_closes_the_gate_on_an_uncredentialed_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate nobody calls is a module-level ``True`` that never moves.

    Every other test in this file monkeypatches the resolved flags, so all of
    them would still pass with the call deleted from startup - and the shipped
    default would then deliver managed prompts and models on a server where
    every operation succeeds unauthenticated.

    So this one runs the real ``lifespan`` rather than reading it. The flags are
    poisoned to the permissive value first, credentials are turned off, and the
    assertion is that startup itself moved them. Nothing here is stubbed: if the
    call site is removed, the poisoned values survive and this fails.
    """
    from agent_control_server.main import app, lifespan

    monkeypatch.setattr(server_config.auth_settings, "api_key_enabled", False)
    monkeypatch.delenv("AGENT_CONTROL_AUTH_MODE", raising=False)
    monkeypatch.delenv(_OVERRIDE_VAR, raising=False)
    # The permissive default, restored by monkeypatch when the test ends so the
    # rest of the session is unaffected.
    monkeypatch.setattr(server_config, "AGENT_CONFIG_DELIVERY_ALLOWED", True)
    monkeypatch.setattr(server_config, "AGENT_CONFIG_MODEL_TIER_LIMIT", None)

    async with lifespan(app):
        assert server_config.AGENT_CONFIG_DELIVERY_ALLOWED is False
        assert server_config.AGENT_CONFIG_MODEL_TIER_LIMIT is None


async def test_running_startup_leaves_the_gate_open_on_a_credentialed_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, so the test above cannot pass by always closing.

    Without this pair, a gate hard-wired to ``False`` would look correct and
    would quietly stop every real deployment from ever delivering a prompt.
    """
    from agent_control_server.main import app, lifespan

    monkeypatch.setattr(server_config.auth_settings, "api_key_enabled", True)
    monkeypatch.delenv("AGENT_CONTROL_AUTH_MODE", raising=False)
    monkeypatch.setattr(server_config, "AGENT_CONFIG_DELIVERY_ALLOWED", False)
    monkeypatch.setattr(server_config, "AGENT_CONFIG_MODEL_TIER_LIMIT", "economy")

    async with lifespan(app):
        assert server_config.AGENT_CONFIG_DELIVERY_ALLOWED is True
        assert server_config.AGENT_CONFIG_MODEL_TIER_LIMIT is None


def test_the_gate_is_resolved_before_the_authorizer_is_installed() -> None:
    """Ordering, which the two lifespan runs above cannot observe.

    Both of them look at the flags after startup has finished, so a gate
    resolved at the very end of ``lifespan`` would satisfy them while leaving a
    window in which the authorizer is live and the gate is not. Startup is not
    concurrent with request serving under ``TestClient``, so the window is
    theoretical today; it stops being theoretical the moment anything in between
    starts serving or spawns a worker that does.

    Reading the source is the only way to assert an ordering that leaves no
    runtime trace. It is the one implementation-coupled check here and it is
    narrow: two names and their relative position.
    """
    import inspect

    from agent_control_server import main

    source = inspect.getsource(main.lifespan)

    assert "check_agent_config_startup_requirements" in source, (
        "the agent config delivery gate is no longer resolved during startup"
    )
    assert source.index("check_agent_config_startup_requirements") < source.index(
        "configure_auth_from_env"
    )


def test_the_module_flags_start_permissive_so_the_gate_must_close_them() -> None:
    """Fail-open by default is deliberate and worth stating.

    A deployment with credentials on is the common case, and it must not need
    the gate to have run in order to work. That is precisely why the lifespan
    tests above exist: the default is the unsafe direction, so "the gate ran" is
    the thing that has to be proved rather than assumed.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(server_config))
    defaults = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"AGENT_CONFIG_DELIVERY_ALLOWED"}
    }
    assert isinstance(defaults["AGENT_CONFIG_DELIVERY_ALLOWED"], ast.Constant)
    assert defaults["AGENT_CONFIG_DELIVERY_ALLOWED"].value is True
