"""The model half: the allowlist, its four refusals, and safe degradation.

A model id is not a name. It is a destination selector: a slash prefix
re-selects the underlying provider and a configured ``api_base`` is ignored for
routing, so an id like ``bedrock/anthropic.claude-v2`` sends every prompt, every
tool result and every piece of customer data to AWS while the dashboard, the
badge and the banner all say the traffic goes to the configured endpoint.

That is why the id is refused at four layers, and why this file checks all four
rather than trusting that the outermost one always runs:

1. ``ModelSettings`` at load, so a bad entry never becomes offerable.
2. The write boundary, before the allowlist lookup, so somebody who pasted a URL
   is told they pasted a URL.
3. ``ck_agent_configs_model_id_shape``, so a write path nobody has reviewed yet
   cannot get one into the column.
4. The SDK before construction - covered in the SDK suite.

The other half of this file is what happens when an operator edits server config
under a stored row. Removing an allowlist entry must degrade the *read* and must
never rewrite the row, because the alternative is one mistyped env line silently
wiping model choices across a namespace with nothing in the history recording it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_models.agent_configs import AgentModelOption
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError

from agent_control_server.config import ModelSettings, db_config, model_settings
from agent_control_server.errors import BadRequestError, ConflictError
from agent_control_server.services.agent_configs import AgentConfigService

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


def _agent_name() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


def _register_agent(client: TestClient, agent_name: str) -> None:
    resp = client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent_name,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture()
def agent(client: TestClient) -> str:
    name = _agent_name()
    _register_agent(client, name)
    return name


@pytest.fixture()
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY, _PREMIUM])


def _url(agent_name: str, suffix: str = "") -> str:
    return f"/api/v1/agents/{agent_name}/config{suffix}"


def _get(client: TestClient, agent_name: str) -> dict[str, Any]:
    resp = client.get(_url(agent_name))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Layer 1: settings load
# ---------------------------------------------------------------------------


class TestModelSettingsRefusesToLoad:
    """The server refuses to start rather than offering an unsafe entry.

    Same posture as ``check_executor_startup_requirements``: a configuration
    mistake on this field is not something to log and continue past, because the
    consequence is customer data arriving at a vendor nobody chose.

    ``hermetic`` is doing real work here. ``server/conftest.py`` pins the
    settings *singletons* against the repository ``.env`` and against an
    exported shell variable, but a settings object constructed **inside** a test
    still reads the ambient environment: the scrub is only in force while the
    singletons are being rebuilt at import. So a developer with
    ``AGENT_CONTROL_MODELS_ALLOWLIST`` exported would otherwise move the
    assertion in ``test_the_allowlist_is_empty_by_default``.
    """

    @pytest.fixture(autouse=True)
    def hermetic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_CONTROL_MODELS_ALLOWLIST", raising=False)

    @staticmethod
    def _settings(entries: list[dict[str, Any]]) -> ModelSettings:
        return ModelSettings(_env_file=None, allowlist=entries)

    def test_an_id_containing_a_slash_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            self._settings(
                [
                    {
                        "id": "bedrock/anthropic.claude-v2",
                        "label": "Claude",
                        "provider": "openai_compatible",
                        "cost_tier": "premium",
                    }
                ]
            )

    def test_an_id_containing_a_scheme_is_refused(self) -> None:
        """Redundant given the slash rule, and it catches a different mistake.

        Somebody who reads "model" and thinks "endpoint" writes a URL, and the
        error they get should not be about slashes.
        """
        with pytest.raises(ValidationError):
            self._settings(
                [
                    {
                        "id": "https://api.openai.com/v1",
                        "label": "OpenAI",
                        "provider": "openai_compatible",
                        "cost_tier": "premium",
                    }
                ]
            )

    def test_an_id_that_disagrees_with_its_provider_is_refused(self) -> None:
        """``{"id": "gpt-5.6-sol", "provider": "gemini"}`` is a plausible slip.

        It would take the Gemini construction branch for a name the framework's
        own registry resolves to an OpenAI client, and that mismatch is only
        visible three layers away.
        """
        with pytest.raises(ValidationError, match="gemini"):
            self._settings(
                [
                    {
                        "id": "gpt-5.6-sol",
                        "label": "Mislabelled",
                        "provider": "gemini",
                        "cost_tier": "premium",
                    }
                ]
            )

    def test_a_gemini_named_id_declared_openai_compatible_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="gemini"):
            self._settings(
                [
                    {
                        "id": "gemini-2.5-flash",
                        "label": "Mislabelled the other way",
                        "provider": "openai_compatible",
                        "cost_tier": "standard",
                    }
                ]
            )

    def test_a_duplicate_id_is_refused(self) -> None:
        """Two rows for one id means a picker showing two different providers."""
        entry = {
            "id": "gpt-5.4-mini",
            "label": "GPT 5.4 mini",
            "provider": "openai_compatible",
            "cost_tier": "economy",
        }
        with pytest.raises(ValidationError, match="more than once"):
            self._settings([entry, dict(entry, label="Again")])

    def test_a_well_formed_allowlist_loads(self) -> None:
        settings = self._settings(
            [
                {
                    "id": "gpt-5.4-mini",
                    "label": "GPT 5.4 mini",
                    "provider": "openai_compatible",
                    "cost_tier": "economy",
                    "recommended": True,
                },
                {
                    "id": "gemini-2.5-flash",
                    "label": "Gemini 2.5 Flash",
                    "provider": "gemini",
                    "cost_tier": "standard",
                },
            ]
        )
        assert [entry.id for entry in settings.allowlist] == [
            "gpt-5.4-mini",
            "gemini-2.5-flash",
        ]
        assert settings.find("gemini-2.5-flash") is not None
        assert settings.find("nothing-like-this") is None

    def test_the_allowlist_is_empty_by_default(self) -> None:
        """So the model half is inert on every existing deployment."""
        assert ModelSettings(_env_file=None).allowlist == []


# ---------------------------------------------------------------------------
# Layer 2: the write boundary
# ---------------------------------------------------------------------------


class TestTheWriteBoundary:
    def test_an_id_outside_the_allowlist_is_400_naming_what_is_allowed(
        self, client: TestClient, agent: str, allowlist: None
    ) -> None:
        """A typo must not be storable.

        An unresolvable name raises inside the framework's model lookup on every
        model call, forever - an agent offline with no signal at save time. The
        allowlist is what turns that into a rejection the operator can read.
        """
        resp = client.put(
            _url(agent), json={"model_id": "gpt-4o-typo", "expected_version": 0}
        )
        assert resp.status_code == 400, resp.text
        payload = resp.json()
        assert payload["error_code"] == "MODEL_NOT_ALLOWED"
        assert "gpt-5.4-mini" in payload["detail"]
        assert _get(client, agent)["current_version"] == 0

    def test_with_no_allowlist_configured_every_id_is_refused(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(model_settings, "allowlist", [])
        resp = client.put(
            _url(agent), json={"model_id": "gpt-5.4-mini", "expected_version": 0}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error_code"] == "MODEL_NOT_ALLOWED"

    @pytest.mark.parametrize(
        "model_id",
        [
            "bedrock/anthropic.claude-v2",
            "openai/gpt-5.4-mini",
            "http://127.0.0.1:10531/v1",
        ],
    )
    def test_a_slashed_or_url_shaped_id_is_refused_and_never_stored(
        self, client: TestClient, agent: str, allowlist: None, model_id: str
    ) -> None:
        resp = client.put(
            _url(agent), json={"model_id": model_id, "expected_version": 0}
        )
        assert resp.status_code in (400, 422), resp.text
        assert resp.json().get("error_code") != "MODEL_NOT_ALLOWED"
        assert _get(client, agent)["model_id"] is None

    def test_the_shape_check_runs_before_the_allowlist_lookup(
        self, allowlist: None
    ) -> None:
        """Whoever pasted a URL is told they pasted a URL.

        Told "not in the allowlist" instead, they would go and add it to the
        allowlist, which is the one action that must not help.
        """
        with pytest.raises(BadRequestError) as excinfo:
            AgentConfigService.validate_model_allowed("https://evil.example.com/v1")
        assert excinfo.value.error_code.value == "VALIDATION_ERROR"
        assert "endpoint" in str(excinfo.value.detail).lower()

    def test_the_validator_accepts_an_allowlisted_id_and_returns_its_entry(
        self, allowlist: None
    ) -> None:
        entry = AgentConfigService.validate_model_allowed("gpt-5.4-mini")
        assert entry is not None
        assert entry.provider == "openai_compatible"
        assert entry.cost_tier == "economy"

    def test_the_validator_treats_none_as_no_change(self, allowlist: None) -> None:
        """Omitting the field is not the same as choosing nothing from the list."""
        assert AgentConfigService.validate_model_allowed(None) is None


# ---------------------------------------------------------------------------
# Layer 3: the database constraint
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    make_url(db_config.get_url()).get_backend_name() != "postgresql",
    reason="The shape check constraint is only enforced on PostgreSQL here.",
)
class TestTheDatabaseConstraint:
    """Defence in depth on the one field where a mistake picks the vendor.

    Section 6 of the design deliberately refuses a constraint on allowlist
    *membership*, because removing one env line would then break startup against
    existing rows. Shape is different in kind: it is invariant, so it belongs in
    the schema, and it is what stops a future write path nobody has reviewed
    from putting a destination selector in this column.
    """

    @staticmethod
    def _insert(engine: Engine, agent_name: str, model_id: str) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agents (namespace_key, name, data) "
                    "VALUES ('default', :name, '{}'::json) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"name": agent_name},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_configs (namespace_key, agent_name, model_id) "
                    "VALUES ('default', :name, :model_id)"
                ),
                {"name": agent_name, "model_id": model_id},
            )

    @pytest.mark.parametrize(
        "model_id", ["bedrock/anthropic.claude-v2", "https://evil.example.com/v1"]
    )
    def test_a_direct_insert_of_a_slashed_id_is_rejected(
        self, db_engine: Engine, model_id: str
    ) -> None:
        with pytest.raises(IntegrityError, match="ck_agent_configs_model_id_shape"):
            self._insert(db_engine, _agent_name(), model_id)

    def test_a_direct_insert_of_a_well_shaped_id_is_accepted(
        self, db_engine: Engine
    ) -> None:
        """The constraint has to admit every id the allowlist could hold."""
        self._insert(db_engine, _agent_name(), "gpt-5.4-mini")


# ---------------------------------------------------------------------------
# Degrading when an operator edits server config under a stored row
# ---------------------------------------------------------------------------


class TestAModelLeavingTheAllowlist:
    def test_a_delisted_model_stops_being_applied_without_the_row_being_rewritten(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Membership is a property of the read, never of the row.

        Nulling stored ids when server config changes would mean one mistyped
        env line silently wiped model choices across a namespace, with no
        version row recording it and nothing to restore from.
        """
        monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY])
        resp = client.put(
            _url(agent),
            json={"body": "Body.", "model_id": _ECONOMY.id, "expected_version": 0},
        )
        assert resp.status_code == 200, resp.text
        before = _get(client, agent)
        assert before["model_source"] == "managed"

        monkeypatch.setattr(model_settings, "allowlist", [_PREMIUM])
        after = _get(client, agent)

        assert after["model_id"] == _ECONOMY.id
        assert after["model_allowed"] is False
        assert after["model_provider"] is None
        assert after["model_cost_tier"] is None
        assert after["model_source"] == "code"
        assert after["current_version"] == before["current_version"]
        assert after["etag"] == before["etag"]
        assert after["body"] == "Body."
        assert after["prompt_source"] == "managed"

    def test_re_adding_the_entry_restores_the_behaviour_with_no_write(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY])
        client.put(
            _url(agent), json={"model_id": _ECONOMY.id, "expected_version": 0}
        )
        monkeypatch.setattr(model_settings, "allowlist", [])
        assert _get(client, agent)["model_source"] == "code"

        monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY])
        restored = _get(client, agent)
        assert restored["model_source"] == "managed"
        assert restored["current_version"] == 1

    def test_restoring_a_version_naming_a_delisted_model_is_409_and_writes_nothing(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restore that quietly dropped the model half would be an invisible rewind.

        409 rather than 400 because the request was well formed and would have
        been correct before an operator edited server config, which is the same
        shape ``SCHEMA_INCOMPATIBLE`` already uses.
        """
        monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY, _PREMIUM])
        client.put(
            _url(agent),
            json={"body": "One.", "model_id": _PREMIUM.id, "expected_version": 0},
        )
        client.put(
            _url(agent),
            json={"body": "Two.", "model_id": _ECONOMY.id, "expected_version": 1},
        )

        monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY])
        resp = client.post(
            _url(agent, "/versions/1:restore"), json={"expected_version": 2}
        )

        assert resp.status_code == 409, resp.text
        payload = resp.json()
        assert payload["error_code"] == "MODEL_NOT_ALLOWED"
        assert _PREMIUM.id in payload["detail"]

        current = _get(client, agent)
        assert current["body"] == "Two."
        assert current["model_id"] == _ECONOMY.id
        assert current["current_version"] == 2

    def test_the_keep_current_model_alternative_is_an_ordinary_save(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The explicit escape hatch from the 409 above, and it is not a new route.

        "Restore the prompt text and keep the current model" is a save carrying
        the old body and the current id, so it lands in the history as an
        ordinary version rather than as a rewind that dropped half its payload.
        """
        monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY, _PREMIUM])
        client.put(
            _url(agent),
            json={"body": "One.", "model_id": _PREMIUM.id, "expected_version": 0},
        )
        client.put(
            _url(agent),
            json={"body": "Two.", "model_id": _ECONOMY.id, "expected_version": 1},
        )
        monkeypatch.setattr(model_settings, "allowlist", [_ECONOMY])

        old_body = client.get(_url(agent, "/versions/1")).json()["version"]["body"]
        resp = client.put(
            _url(agent),
            json={"body": old_body, "model_id": _ECONOMY.id, "expected_version": 2},
        )

        assert resp.status_code == 200, resp.text
        current = _get(client, agent)
        assert current["body"] == "One."
        assert current["model_id"] == _ECONOMY.id

    def test_the_restore_of_a_prompt_only_version_is_unaffected_by_the_allowlist(
        self, client: TestClient, agent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A version that never named a model has nothing to be refused about."""
        monkeypatch.setattr(model_settings, "allowlist", [])
        client.put(_url(agent), json={"body": "One.", "expected_version": 0})
        client.put(_url(agent), json={"body": "Two.", "expected_version": 1})

        resp = client.post(
            _url(agent, "/versions/1:restore"), json={"expected_version": 2}
        )
        assert resp.status_code == 200, resp.text
        assert _get(client, agent)["body"] == "One."


# ---------------------------------------------------------------------------
# The allowlist route
# ---------------------------------------------------------------------------


def test_the_allowlist_route_returns_what_the_server_offers(
    client: TestClient, allowlist: None
) -> None:
    resp = client.get("/api/v1/agent-models")
    assert resp.status_code == 200, resp.text
    models = resp.json()["models"]
    assert [m["id"] for m in models] == [_ECONOMY.id, _PREMIUM.id]
    assert models[0]["cost_tier"] == "economy"


def test_the_allowlist_route_is_empty_when_nothing_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI renders "no models configured" rather than an empty dropdown."""
    monkeypatch.setattr(model_settings, "allowlist", [])
    assert client.get("/api/v1/agent-models").json()["models"] == []


def test_a_read_only_viewer_still_learns_their_own_agents_model_details(
    non_admin_client: TestClient, client: TestClient, agent: str, allowlist: None
) -> None:
    """Losing the enumeration route must not blind a viewer to their own agent.

    The per-agent response carries the provider and the cost tier at read tier,
    so the only thing an admin key buys is the rest of the operator's vendor
    inventory.
    """
    client.put(
        _url(agent), json={"model_id": _ECONOMY.id, "expected_version": 0}
    )

    config = non_admin_client.get(_url(agent)).json()
    assert config["model_id"] == _ECONOMY.id
    assert config["model_provider"] == "openai_compatible"
    assert config["model_cost_tier"] == "economy"


def test_service_validation_raises_a_conflict_on_the_restore_path(
    allowlist: None,
) -> None:
    """Same rule, different status, and the difference is deliberate."""
    with pytest.raises(ConflictError):
        AgentConfigService.validate_model_allowed("gone-from-the-list", on_restore=True)
    with pytest.raises(BadRequestError):
        AgentConfigService.validate_model_allowed("gone-from-the-list")
