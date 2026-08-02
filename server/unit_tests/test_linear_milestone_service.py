"""Unit coverage for the caching / back-off layer over the Linear adapter.

The clock is faked by swapping the module's ``time`` reference, so TTL,
staleness and cooldown windows are exercised without sleeping.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from agent_control_models.linear import MilestonesStatus
from pydantic import SecretStr

from agent_control_server.config import linear_settings
from agent_control_server.services import linear_milestones
from agent_control_server.services.linear_client import (
    HttpLinearClient,
    LinearError,
    LinearMilestone,
)
from agent_control_server.services.linear_milestones import (
    LinearMilestoneService,
    build_milestone_service,
    get_milestone_service,
    shutdown_milestone_service,
)

NS = "ns-one"
KEY = "ENG"


class FakeClock:
    """Stand-in for the ``time`` module, exposing only what the service uses."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(linear_milestones, "time", fake)
    return fake


class FakeLinearClient:
    """Records calls and replays a scripted sequence of results."""

    def __init__(self, *results: object) -> None:
        self._results = list(results)
        self.calls: list[str] = []
        self.closed = False
        self.gate: asyncio.Event | None = None

    async def fetch_milestones(self, team_key: str) -> list[LinearMilestone]:
        self.calls.append(team_key)
        if self.gate is not None:
            await self.gate.wait()
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, list)
        return list(result)

    async def aclose(self) -> None:
        self.closed = True


def _milestone(identifier: str = "m1", name: str = "Beta") -> LinearMilestone:
    return LinearMilestone(
        id=identifier,
        name=name,
        description=None,
        target_date=dt.date(2026, 9, 1),
        status="unstarted",
        progress=0.5,
        project_id="p1",
        project_name="Platform",
        project_url="https://linear.app/acme/project/platform",
    )


class TestConfigurationStates:
    async def test_missing_client_reports_not_configured(self) -> None:
        service = LinearMilestoneService(client=None)

        result = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert result.status is MilestonesStatus.NOT_CONFIGURED
        assert result.milestones == []
        assert result.error is None

    async def test_not_configured_wins_over_not_linked(self) -> None:
        service = LinearMilestoneService(client=None)

        result = await service.get_milestones(namespace_key=NS, linear_team_key=None)

        assert result.status is MilestonesStatus.NOT_CONFIGURED

    @pytest.mark.parametrize("team_key", [None, ""])
    async def test_unlinked_team_reports_not_linked_without_calling_linear(
        self, team_key: str | None
    ) -> None:
        client = FakeLinearClient([_milestone()])
        service = LinearMilestoneService(client=client)

        result = await service.get_milestones(namespace_key=NS, linear_team_key=team_key)

        assert result.status is MilestonesStatus.NOT_LINKED
        assert client.calls == []


class TestSuccessfulReads:
    async def test_returns_milestones_with_a_fetch_timestamp(self, clock: FakeClock) -> None:
        service = LinearMilestoneService(client=FakeLinearClient([_milestone()]))

        result = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert result.status is MilestonesStatus.OK
        assert [m.id for m in result.milestones] == ["m1"]
        assert result.cached is False
        assert isinstance(result.fetched_at, dt.datetime)

    async def test_no_milestones_reports_empty_not_error(self, clock: FakeClock) -> None:
        service = LinearMilestoneService(client=FakeLinearClient([]))

        result = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert result.status is MilestonesStatus.EMPTY
        assert result.milestones == []
        assert result.error is None

    async def test_mutating_the_result_does_not_change_what_is_cached(
        self, clock: FakeClock
    ) -> None:
        service = LinearMilestoneService(client=FakeLinearClient([_milestone()]))

        first = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        first.milestones.clear()
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert [m.id for m in second.milestones] == ["m1"]


class TestCaching:
    async def test_second_read_inside_the_ttl_is_served_from_cache(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient([_milestone()])
        service = LinearMilestoneService(client=client, ttl_seconds=60)

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(59)
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert client.calls == [KEY]
        assert second.cached is True

    async def test_read_after_the_ttl_refetches(self, clock: FakeClock) -> None:
        client = FakeLinearClient([_milestone("m1")], [_milestone("m2")])
        service = LinearMilestoneService(client=client, ttl_seconds=60)

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(61)
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert client.calls == [KEY, KEY]
        assert [m.id for m in second.milestones] == ["m2"]
        assert second.cached is False

    async def test_cache_is_keyed_by_namespace(self, clock: FakeClock) -> None:
        client = FakeLinearClient([_milestone()])
        service = LinearMilestoneService(client=client)

        await service.get_milestones(namespace_key="ns-one", linear_team_key=KEY)
        await service.get_milestones(namespace_key="ns-two", linear_team_key=KEY)

        assert client.calls == [KEY, KEY]

    async def test_cache_is_keyed_by_team(self, clock: FakeClock) -> None:
        client = FakeLinearClient([_milestone()])
        service = LinearMilestoneService(client=client)

        await service.get_milestones(namespace_key=NS, linear_team_key="ENG")
        await service.get_milestones(namespace_key=NS, linear_team_key="SALES")

        assert client.calls == ["ENG", "SALES"]

    async def test_concurrent_cold_readers_produce_one_upstream_call(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient([_milestone()])
        client.gate = asyncio.Event()
        service = LinearMilestoneService(client=client)

        tasks = [
            asyncio.create_task(
                service.get_milestones(namespace_key=NS, linear_team_key=KEY)
            )
            for _ in range(10)
        ]
        await asyncio.sleep(0)
        client.gate.set()
        results = await asyncio.gather(*tasks)

        assert client.calls == [KEY]
        assert all(r.status is MilestonesStatus.OK for r in results)

    async def test_evicts_the_oldest_entry_past_the_ceiling(self, clock: FakeClock) -> None:
        client = FakeLinearClient([_milestone()])
        service = LinearMilestoneService(client=client, ttl_seconds=10_000)

        for index in range(linear_milestones._MAX_CACHE_ENTRIES + 1):
            clock.advance(1)
            await service.get_milestones(namespace_key=NS, linear_team_key=f"T{index}")

        assert len(service._cache) == linear_milestones._MAX_CACHE_ENTRIES
        assert (NS, "T0") not in service._cache
        assert (NS, f"T{linear_milestones._MAX_CACHE_ENTRIES}") in service._cache


class TestFailureHandling:
    async def test_failure_with_no_cache_reports_error(self, clock: FakeClock) -> None:
        client = FakeLinearClient(LinearError("Linear could not be reached."))
        service = LinearMilestoneService(client=client)

        result = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert result.status is MilestonesStatus.ERROR
        assert result.error == "Linear could not be reached."
        assert result.milestones == []
        assert result.retry_after_seconds is None

    async def test_rate_limit_passes_the_upstream_retry_after_through(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(
            LinearError("Linear is rate-limiting this server.", retry_after_seconds=42)
        )
        service = LinearMilestoneService(client=client)

        result = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert result.status is MilestonesStatus.ERROR
        assert result.retry_after_seconds == 42

    async def test_stale_cache_is_preferred_over_an_error_panel(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(
            [_milestone()], LinearError("Linear could not be reached.")
        )
        service = LinearMilestoneService(client=client, ttl_seconds=60, stale_ttl_seconds=900)

        first = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(120)
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert second.status is MilestonesStatus.OK
        assert second.cached is True
        assert second.fetched_at == first.fetched_at
        assert second.error is None

    async def test_cache_older_than_the_stale_window_is_dropped(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(
            [_milestone()], LinearError("Linear could not be reached.")
        )
        service = LinearMilestoneService(client=client, ttl_seconds=60, stale_ttl_seconds=900)

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(1000)
        result = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert result.status is MilestonesStatus.ERROR
        assert (NS, KEY) not in service._cache

    async def test_cooldown_stops_a_second_call_to_a_failing_linear(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(LinearError("Linear could not be reached."))
        service = LinearMilestoneService(client=client, error_cooldown_seconds=30)

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(5)
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert client.calls == [KEY]
        assert second.status is MilestonesStatus.ERROR
        assert second.error == "Linear could not be reached."

    async def test_cooldown_from_a_server_side_wait_reports_no_retry_after(
        self, clock: FakeClock
    ) -> None:
        """Only a delay Linear actually asked for is reported to a client."""
        client = FakeLinearClient(LinearError("Linear could not be reached."))
        service = LinearMilestoneService(client=client, error_cooldown_seconds=30)

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(5)
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert second.retry_after_seconds is None

    async def test_cooldown_from_a_429_reports_the_remaining_wait(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(
            LinearError("Linear is rate-limiting this server.", retry_after_seconds=60)
        )
        service = LinearMilestoneService(client=client)

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(20)
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert client.calls == [KEY]
        assert second.retry_after_seconds == 41

    async def test_linear_is_called_again_once_the_cooldown_expires(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(
            LinearError("Linear could not be reached."), [_milestone()]
        )
        service = LinearMilestoneService(client=client, error_cooldown_seconds=30)

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(31)
        second = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert client.calls == [KEY, KEY]
        assert second.status is MilestonesStatus.OK

    async def test_a_successful_read_clears_a_previous_cooldown(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(
            LinearError("Linear could not be reached."), [_milestone()]
        )
        service = LinearMilestoneService(
            client=client, ttl_seconds=1, error_cooldown_seconds=30
        )

        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        clock.advance(31)
        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        assert (NS, KEY) not in service._cooldowns

    async def test_one_namespace_cooldown_does_not_silence_another(
        self, clock: FakeClock
    ) -> None:
        client = FakeLinearClient(
            LinearError("Linear could not be reached."), [_milestone()]
        )
        service = LinearMilestoneService(client=client, error_cooldown_seconds=30)

        first = await service.get_milestones(namespace_key="ns-one", linear_team_key=KEY)
        second = await service.get_milestones(namespace_key="ns-two", linear_team_key=KEY)

        assert first.status is MilestonesStatus.ERROR
        assert second.status is MilestonesStatus.OK
        assert client.calls == [KEY, KEY]


class TestLifecycle:
    async def test_aclose_closes_the_client_and_clears_state(self, clock: FakeClock) -> None:
        client = FakeLinearClient([_milestone()])
        service = LinearMilestoneService(client=client)
        await service.get_milestones(namespace_key=NS, linear_team_key=KEY)

        await service.aclose()

        assert client.closed is True
        assert service._cache == {}
        assert service._cooldowns == {}
        assert service._locks == {}

    async def test_aclose_without_a_client_is_a_no_op(self) -> None:
        await LinearMilestoneService(client=None).aclose()

    async def test_stale_ttl_is_never_shorter_than_the_ttl(self) -> None:
        service = LinearMilestoneService(client=None, ttl_seconds=120, stale_ttl_seconds=10)
        assert service._stale_ttl_seconds == 120


class TestServiceConstruction:
    async def test_no_api_key_builds_a_service_with_no_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(linear_settings, "api_key", SecretStr(""))

        service = build_milestone_service()

        assert service._client is None
        result = await service.get_milestones(namespace_key=NS, linear_team_key=KEY)
        assert result.status is MilestonesStatus.NOT_CONFIGURED

    async def test_whitespace_only_api_key_counts_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(linear_settings, "api_key", SecretStr("   "))

        assert build_milestone_service()._client is None

    async def test_a_configured_key_builds_an_http_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(linear_settings, "api_key", SecretStr("lin_api_UNITTEST"))

        service = build_milestone_service()
        try:
            assert isinstance(service._client, HttpLinearClient)
        finally:
            await service.aclose()

    async def test_settings_repr_does_not_print_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = "lin_api_REPRSENTINEL"
        monkeypatch.setattr(linear_settings, "api_key", SecretStr(sentinel))

        assert sentinel not in repr(linear_settings)
        assert sentinel not in str(linear_settings.model_dump())

    async def test_dependency_returns_one_process_wide_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(linear_settings, "api_key", SecretStr(""))
        monkeypatch.setattr(linear_milestones, "_service", None)

        first = get_milestone_service()
        second = get_milestone_service()
        try:
            assert first is second
        finally:
            await shutdown_milestone_service()

    async def test_shutdown_closes_and_forgets_the_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(linear_milestones, "_service", None)
        client = FakeLinearClient([])
        monkeypatch.setattr(
            linear_milestones,
            "build_milestone_service",
            lambda: LinearMilestoneService(client=client),
        )

        service = get_milestone_service()
        await shutdown_milestone_service()

        assert client.closed is True
        assert linear_milestones._service is None
        assert service is not None

    async def test_shutdown_without_a_service_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(linear_milestones, "_service", None)
        await shutdown_milestone_service()
