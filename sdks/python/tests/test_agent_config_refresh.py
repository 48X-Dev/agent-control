"""Fetching an agent's configuration on the refresh loop, and failing safely.

The load-bearing property here is not that the fetch works. It is that when the
fetch **fails**, controls still reach the agent.

The refresh loop's original body wrapped its whole fetch in one ``try`` whose
handler is ``continue``. Putting a config fetch inside that block would mean a
500, a timeout or a 403 on a low-value new endpoint silently stops newly
authored controls reaching running agents, for as long as the failure lasts,
with only a log line. That is a denial channel into the safety-critical path,
opened by an unrelated feature. So the config fetch is a second, independent
block that runs strictly after controls are published and never uses
``continue``.

The other property worth its test is what a failure leaves behind: the previous
values, and a ``fetched_at`` that is **not** advanced. That timestamp is what the
model staleness ceiling reads, so advancing it on a failed fetch would keep a
managed model alive forever on an unreachable control plane - which is the exact
unbounded-spend case the ceiling exists to close.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import agent_control
import pytest
from agent_control._state import state
from agent_control.agent_config import AgentConfigSnapshot


@pytest.fixture(autouse=True)
def _reset() -> Generator[None, None, None]:
    agent_control._stop_policy_refresh_loop()
    agent_control._reset_state()
    yield
    agent_control._stop_policy_refresh_loop()
    agent_control._reset_state()


def _managed_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "agent_name": "marketing-copywriter",
        "body": "Write like a marketing copywriter.",
        "prompt_enabled": True,
        "prompt_source": "managed",
        "model_id": "gpt-5.4-mini",
        "model_provider": "openai_compatible",
        "model_allowed": True,
        "model_cost_tier": "economy",
        "model_source": "managed",
        "delivery_state": "active",
        "etag": "v3-abc123def456",
        "current_version": 3,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The snapshot decides nothing for itself
# ---------------------------------------------------------------------------


class TestTheSnapshot:
    def test_the_managed_values_come_from_what_the_server_resolved(self) -> None:
        """Sources resolve server-side, once, and the SDK does not re-derive them.

        That keeps the startup delivery gate and the allowlist-membership check
        in one place rather than in every client, and it is why an older SDK
        meeting a newer server degrades to "do nothing" rather than to "guess".
        """
        snapshot = AgentConfigSnapshot.from_response(
            _managed_payload(), fetched_at=dt.datetime.now(dt.UTC)
        )

        assert snapshot.managed_prompt == "Write like a marketing copywriter."
        assert snapshot.managed_model == ("gpt-5.4-mini", "openai_compatible")

    @pytest.mark.parametrize("prompt_source", ["none", "code"])
    def test_a_body_the_server_did_not_call_managed_is_not_applied(
        self, prompt_source: str
    ) -> None:
        """Cleared, disabled and gated have all already collapsed into one field."""
        snapshot = AgentConfigSnapshot.from_response(
            _managed_payload(prompt_source=prompt_source),
            fetched_at=dt.datetime.now(dt.UTC),
        )
        assert snapshot.managed_prompt is None

    def test_a_model_the_server_did_not_call_managed_is_not_applied(self) -> None:
        snapshot = AgentConfigSnapshot.from_response(
            _managed_payload(model_source="code"),
            fetched_at=dt.datetime.now(dt.UTC),
        )
        assert snapshot.managed_model is None

    def test_a_missing_provider_means_no_model_rather_than_a_guessed_one(self) -> None:
        """Inferring the provider from the id string *is* the exfiltration path.

        A bare ``gpt-*`` resolves, in the framework's own registry, to a client
        whose factory takes no base URL - so a guess here sends prompts and tool
        results to whatever the process environment happens to say, or to the
        vendor's own endpoint when it says nothing.
        """
        snapshot = AgentConfigSnapshot.from_response(
            _managed_payload(model_provider=None),
            fetched_at=dt.datetime.now(dt.UTC),
        )
        assert snapshot.model_id == "gpt-5.4-mini"
        assert snapshot.managed_model is None

    def test_an_unknown_field_on_the_wire_is_tolerated(self) -> None:
        """A newer server must not break an older agent process."""
        snapshot = AgentConfigSnapshot.from_response(
            _managed_payload(some_future_field="whatever"),
            fetched_at=dt.datetime.now(dt.UTC),
        )
        assert snapshot.managed_prompt is not None

    def test_a_change_in_delivery_counts_as_a_change(self) -> None:
        """What a caller reacting to configuration actually wants to hear about.

        Comparing etags would miss the gate opening or a model leaving the
        allowlist, neither of which is a write but both of which change what the
        agent runs.
        """
        now = dt.datetime.now(dt.UTC)
        managed = AgentConfigSnapshot.from_response(_managed_payload(), fetched_at=now)
        gated = AgentConfigSnapshot.from_response(
            _managed_payload(prompt_source="code", model_source="code"), fetched_at=now
        )

        assert managed.differs_from(gated) is True
        assert managed.differs_from(managed) is False
        assert managed.differs_from(None) is True


# ---------------------------------------------------------------------------
# The error boundary
# ---------------------------------------------------------------------------


class TestTheErrorBoundary:
    def test_a_failing_config_fetch_never_raises(self) -> None:
        """A configuration feature that can take an agent down is worse than none."""
        with patch.object(
            agent_control,
            "_fetch_agent_config_async",
            new=AsyncMock(side_effect=RuntimeError("control plane is down")),
        ):
            agent_control._refresh_agent_config_once()

        assert state.agent_config is None

    def test_a_failing_config_fetch_keeps_the_last_known_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A control-plane outage must not become an agent behaviour change."""
        first = AgentConfigSnapshot.from_response(
            _managed_payload(), fetched_at=dt.datetime.now(dt.UTC)
        )
        agent_control._publish_agent_config(first)

        with patch.object(
            agent_control,
            "_fetch_agent_config_async",
            new=AsyncMock(side_effect=RuntimeError("timeout")),
        ), caplog.at_level("WARNING"):
            agent_control._refresh_agent_config_once()

        assert state.agent_config is first
        assert "keeping the last known" in caplog.text

    def test_a_failing_config_fetch_does_not_advance_the_fetch_timestamp(self) -> None:
        """The staleness ceiling reads this, so advancing it would disarm it.

        A managed model would then survive indefinitely against an unreachable
        control plane - which is precisely the unbounded spend the ceiling
        exists to bound, because the process that would pick up a clear is the
        one that cannot reach the server.
        """
        original = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        agent_control._publish_agent_config(
            AgentConfigSnapshot.from_response(_managed_payload(), fetched_at=original)
        )

        with patch.object(
            agent_control,
            "_fetch_agent_config_async",
            new=AsyncMock(side_effect=RuntimeError("still down")),
        ):
            agent_control._refresh_agent_config_once()

        assert state.agent_config is not None
        assert state.agent_config.fetched_at == original

    def test_a_config_failure_does_not_stop_controls_reaching_the_agent(self) -> None:
        """The denial channel this separation exists to close.

        If these shared one error boundary, anything that could make the config
        endpoint fail - a 500, a timeout, a mis-provisioned key returning 403 -
        would silently stop newly authored controls reaching running agents for
        as long as it lasted.
        """
        published: list[list[dict[str, object]]] = []

        def _record(controls: list[dict[str, object]]) -> None:
            published.append(controls)

        stop_event = _OneIterationEvent()
        with patch.object(
            agent_control,
            "_fetch_controls_async",
            new=AsyncMock(return_value=[{"name": "a-new-control"}]),
        ), patch.object(
            agent_control, "_publish_server_controls", new=_record
        ), patch.object(
            agent_control,
            "_fetch_agent_config_async",
            new=AsyncMock(side_effect=RuntimeError("config endpoint is down")),
        ):
            agent_control._policy_refresh_worker(stop_event, 0)

        assert published == [[{"name": "a-new-control"}]]

    def test_controls_are_published_before_the_config_fetch_runs(self) -> None:
        """Ordering, not just independence.

        Nothing in the config block may execute before controls are published,
        or a slow config endpoint delays control delivery even when it succeeds.
        """
        order: list[str] = []

        def _publish(controls: list[dict[str, object]]) -> None:
            order.append("controls")

        async def _fetch_config() -> AgentConfigSnapshot:
            order.append("config")
            return AgentConfigSnapshot.from_response(
                _managed_payload(), fetched_at=dt.datetime.now(dt.UTC)
            )

        with patch.object(
            agent_control, "_fetch_controls_async", new=AsyncMock(return_value=[])
        ), patch.object(
            agent_control, "_publish_server_controls", new=_publish
        ), patch.object(
            agent_control, "_fetch_agent_config_async", new=_fetch_config
        ):
            agent_control._policy_refresh_worker(_OneIterationEvent(), 0)

        assert order == ["controls", "config"]


class _OneIterationEvent:
    """A stop event that permits exactly one pass of the refresh worker."""

    def __init__(self) -> None:
        self._waits = 0
        self._set = False

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self._waits += 1
        # False lets the body run; True on the second call ends the loop.
        if self._waits > 1:
            return True
        return False

    def is_set(self) -> bool:
        return self._set


# ---------------------------------------------------------------------------
# The public accessors
# ---------------------------------------------------------------------------


class TestTheAccessors:
    def test_the_raw_body_is_returned_unwrapped(self) -> None:
        """We store it, version it and hand it to you; applying it is yours.

        Wrapping exists to solve idempotent re-application in a field shared
        with control guidance, which is an ADK-plugin problem. A caller driving
        their own client does not have it, and handing them fence tags would
        make them strip the tags back off.
        """
        agent_control._publish_agent_config(
            AgentConfigSnapshot.from_response(
                _managed_payload(), fetched_at=dt.datetime.now(dt.UTC)
            )
        )

        prompt = agent_control.get_system_prompt()
        assert prompt == "Write like a marketing copywriter."
        assert "<agent_control_system_prompt>" not in (prompt or "")

    def test_the_model_accessors_return_the_id_and_the_provider(self) -> None:
        agent_control._publish_agent_config(
            AgentConfigSnapshot.from_response(
                _managed_payload(), fetched_at=dt.datetime.now(dt.UTC)
            )
        )

        assert agent_control.get_model_id() == "gpt-5.4-mini"
        assert agent_control.get_model_provider() == "openai_compatible"

    def test_the_accessors_return_nothing_before_a_first_fetch(self) -> None:
        assert agent_control.get_system_prompt() is None
        assert agent_control.get_model_id() is None
        assert agent_control.get_model_provider() is None

    def test_a_gated_server_reports_nothing_to_apply(self) -> None:
        agent_control._publish_agent_config(
            AgentConfigSnapshot.from_response(
                _managed_payload(
                    prompt_source="code",
                    model_source="code",
                    delivery_state="blocked_insecure_auth",
                ),
                fetched_at=dt.datetime.now(dt.UTC),
            )
        )

        assert agent_control.get_system_prompt() is None
        assert agent_control.get_model_id() is None


class TestTheChangeCallback:
    def test_it_fires_on_a_change_to_either_field(self) -> None:
        seen: list[AgentConfigSnapshot] = []
        agent_control.on_config_change(seen.append)
        now = dt.datetime.now(dt.UTC)

        agent_control._publish_agent_config(
            AgentConfigSnapshot.from_response(_managed_payload(), fetched_at=now)
        )
        agent_control._publish_agent_config(
            AgentConfigSnapshot.from_response(
                _managed_payload(model_id="gpt-5.6-sol"), fetched_at=now
            )
        )

        assert len(seen) == 2

    def test_it_does_not_fire_when_nothing_changed(self) -> None:
        """One fetch per interval forever; firing every time would be noise."""
        seen: list[AgentConfigSnapshot] = []
        agent_control.on_config_change(seen.append)
        now = dt.datetime.now(dt.UTC)

        for _ in range(3):
            agent_control._publish_agent_config(
                AgentConfigSnapshot.from_response(_managed_payload(), fetched_at=now)
            )

        assert len(seen) == 1

    def test_a_callback_that_raises_does_not_take_the_refresh_thread_down(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Matching how the violation callback is already handled."""
        seen: list[str] = []

        def _explodes(snapshot: AgentConfigSnapshot) -> None:
            raise RuntimeError("caller's bug")

        agent_control.on_config_change(_explodes)
        agent_control.on_config_change(lambda snapshot: seen.append("ran"))

        with caplog.at_level("ERROR"):
            agent_control._publish_agent_config(
                AgentConfigSnapshot.from_response(
                    _managed_payload(), fetched_at=dt.datetime.now(dt.UTC)
                )
            )

        assert seen == ["ran"]
        assert "on_config_change callback failed" in caplog.text
