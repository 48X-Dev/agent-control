"""The two behavioural refusals, and what each of them lets through."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from agent_control_fleet import server as server_module
from agent_control_fleet.server import ServerClient, ServerError

CLIENT = ServerClient(base_url="http://localhost:8000", api_key="admin-key")


class _Response:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> Any:
        return self._payload


def _answer(monkeypatch: pytest.MonkeyPatch, method: str, response: _Response) -> list[dict]:
    seen: list[dict] = []

    def handler(url: str, **kwargs: Any) -> _Response:
        seen.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(server_module.httpx, method, handler)
    return seen


def test_an_uncredentialed_read_that_succeeds_refuses_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _answer(monkeypatch, "get", _Response(200, {"runtimes": []}))
    with pytest.raises(ServerError) as caught:
        CLIENT.refuse_when_credentials_are_off()
    assert caught.value.code == "credentials_disabled"
    assert "headers" not in seen[0], "the probe must carry no credential at all"


@pytest.mark.parametrize("status", [401, 403])
def test_an_uncredentialed_read_that_is_refused_is_the_healthy_case(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _answer(monkeypatch, "get", _Response(status))
    CLIENT.refuse_when_credentials_are_off()


@pytest.mark.parametrize("status", [401, 403])
def test_an_executor_key_the_halt_path_refuses_stops_the_fleet(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _answer(monkeypatch, "post", _Response(status))
    with pytest.raises(ServerError) as caught:
        CLIENT.refuse_when_executor_credential_cannot_halt("executor-key")
    assert caught.value.code == "executor_credential_cannot_halt"
    assert "STOP button" in str(caught.value)


@pytest.mark.parametrize("status", [200, 404, 422])
def test_any_answer_other_than_a_refusal_means_the_credential_serves_the_path(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    seen = _answer(monkeypatch, "post", _Response(status))
    CLIENT.refuse_when_executor_credential_cannot_halt("executor-key")
    assert seen[0]["headers"]["X-API-Key"] == "executor-key"


def test_the_halt_probe_names_a_session_that_cannot_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads an authorization outcome; a real session key would claim a real halt."""

    seen = _answer(monkeypatch, "post", _Response(200))
    CLIENT.refuse_when_executor_credential_cannot_halt("executor-key")
    assert "fleet-credential-probe-session" in seen[0]["url"]


def test_health_refuses_rather_than_polling_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(url: str, **kwargs: Any) -> _Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(server_module.httpx, "get", failing)
    monkeypatch.setattr(server_module.time, "sleep", lambda seconds: None)
    with pytest.raises(ServerError) as caught:
        CLIENT.wait_for_health(timeout_seconds=0.0)
    assert caught.value.code == "server_unreachable"


def test_bind_sends_the_observed_address_and_the_agents_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _answer(monkeypatch, "put", _Response(200))
    CLIENT.bind_runtime(
        agent_name="marketing_researcher",
        base_url="http://192.168.64.7:8000",
        executor_app_name="marketing_researcher",
    )
    assert seen[0]["json"] == {
        "base_url": "http://192.168.64.7:8000",
        "executor_app_name": "marketing_researcher",
    }
    assert seen[0]["headers"]["X-API-Key"] == "admin-key"
