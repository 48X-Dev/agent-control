"""The ceilings as the dispatcher meets them, with the real dispatcher running.

Everywhere else these controls are provoked by a hand-written ``POST /turns``,
which is the right way to prove they are not in the loop. This file proves the
other half, and it is the half an operator actually cares about: **the shipped
dispatcher, run unmodified against a real server over a real socket, cannot
spend past the namespace's budget even when it is asked to.**

Nothing here stubs the dispatcher's HTTP client, its classification table, its
ledger or its stop logic. ``dispatch_once`` is called exactly as the CLI calls
it. What is substituted is the executor at the far end (turns must not cost real
money) and the settle window on the deny query (ten seconds of polling per turn,
about a question no assertion here asks).

Four properties.

**It cannot overspend.** Given more work than budget, the number of turns that
reach an executor equals the ceiling. Not approximately: the run stops on the
refusal, and the items it did not reach keep their slots.

**The count is in Postgres, so restarting the dispatcher does not refill it.**
A fresh process has no memory of what the previous one spent. If the ceiling
lived in the loop - which is the design this whole phase exists to refuse - a
crash-restart loop would be an unbounded spend, and it would look like normal
operation.

**A stop is reported as a stop.** A namespace that was paused and a namespace
that ran out of hours are two different messages, because they send the next
person to two different dials.

**The delay is machine readable in both 429s.** Section 11.4 exists because the
number used to live only inside an English sentence. The assertion is made
against ``DispatchHTTPError.retry_after_seconds``, which is the field the
dispatcher actually reads, rather than against the JSON body directly.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from agent_control_dispatcher.client import DispatchClient, DispatchHTTPError, Disposition
from agent_control_dispatcher.dispatch import DispatchOptions, dispatch_once
from agent_control_dispatcher.ledger import ClaimStatus
from agent_control_dispatcher.sources.base import SourceItem
from agent_control_dispatcher.sources.file import FileTaskSource
from fastapi import FastAPI
from sqlalchemy import text

from agent_control_server.config import dispatch_settings, executor_settings
from agent_control_server.services.executor_factory import get_executor_client_factory
from agent_control_server.services.turn_quota import reset_turn_quota

from .conftest import TEST_ADMIN_API_KEY, LiveServer, LiveServerFactory
from .test_agent_session_turns import FakeTurnExecutorFactory

RUNTIMES_URL = "/api/v1/agent-runtimes"
TASKS_URL = "/api/v1/agent-tasks"
DISPATCH_URL = "/api/v1/agent-dispatch"

_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_EXECUTOR_APP = "my_agent"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_turn_quota() -> Iterator[None]:
    reset_turn_quota()
    yield
    reset_turn_quota()


@pytest.fixture()
def executor_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)


@pytest.fixture()
def fake_executor(app: FastAPI) -> Iterator[FakeTurnExecutorFactory]:
    factory = FakeTurnExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


@pytest.fixture(autouse=True)
def _no_deny_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ask the deny query once instead of polling it for ten seconds a turn.

    The real method is still the one that runs - the window, the filters and
    the de-duplication are untouched. Only the settle deadline moves, and
    nothing in this file asserts anything about a deny.
    """
    original = DispatchClient.deny_events_for_turn

    async def once(self: DispatchClient, **kwargs: Any) -> Any:
        kwargs["settle_seconds"] = 0.0
        return await original(self, **kwargs)

    monkeypatch.setattr(DispatchClient, "deny_events_for_turn", once)


@pytest.fixture()
async def live(live_server_factory: LiveServerFactory, app: FastAPI) -> LiveServer:
    return await live_server_factory(app)


@pytest.fixture()
async def api(live: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    """A plain admin client, for the setup a dispatcher does not do itself."""
    yield live.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})


@pytest.fixture()
def observability(app: FastAPI) -> Iterator[None]:
    """The deny query needs an event store on ``app.state``; without one it 500s.

    A dispatcher whose deny query fails records the step as *unclassified*
    rather than completed, which would leave every assertion below reading a
    status that is about the wrong thing.

    Bound to the application's own session maker rather than to the test
    module's, because the live server runs inside this test's event loop: a
    second pool filled here and closed in a later test's loop is where the
    ``MissingGreenlet`` noise at teardown comes from.
    """
    from agent_control_server.db import AsyncSessionLocal
    from agent_control_server.observability import DirectEventIngestor, PostgresEventStore

    store = PostgresEventStore(AsyncSessionLocal)
    app.state.event_store = store
    app.state.event_ingestor = DirectEventIngestor(store)
    yield
    del app.state.event_store
    del app.state.event_ingestor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _bound_agent(api: httpx.AsyncClient) -> str:
    agent = f"agent-{uuid.uuid4().hex[:12]}"
    registered = await api.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert registered.status_code == 200, registered.text
    bound = await api.put(
        f"{RUNTIMES_URL}/{agent}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert bound.status_code == 200, bound.text
    return agent


def _tasks_file(tmp_path: Path, count: int, *, name: str = "tasks") -> Path:
    """A YAML source of ``count`` items with refs nothing else will collide on."""
    path = tmp_path / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "ref": f"{name}-{index}-{uuid.uuid4().hex[:8]}",
                    "title": f"item {index}",
                    "body": "Say something short.",
                }
                for index in range(count)
            ]
        ),
        encoding="utf-8",
    )
    return path


def _options(source: Path, agent: str, live: LiveServer, **overrides: Any) -> DispatchOptions:
    """Exactly what the CLI builds, with the server ledger and no local file."""
    return DispatchOptions(
        source_spec=f"file://{source}",
        agent_name=agent,
        base_url=live.base_url,
        api_key=TEST_ADMIN_API_KEY,
        max_tasks=overrides.pop("max_tasks", 1),
        dry_run=overrides.pop("dry_run", True),
        turn_timeout_seconds=overrides.pop("turn_timeout_seconds", 30.0),
        **overrides,
    )


async def _run(options: DispatchOptions) -> tuple[Any, str]:
    """One dispatcher run, with its terminal output captured for the assertions."""
    out = io.StringIO()
    report = await dispatch_once(options, out=out)
    return report, out.getvalue()


def _counter(db_engine: Any) -> int | None:
    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT turns_in_window FROM agent_dispatch_state "
                " WHERE namespace_key = 'default'"
            )
        ).first()
    return None if row is None else int(row.turns_in_window)


# ---------------------------------------------------------------------------
# It cannot overspend
# ---------------------------------------------------------------------------


async def test_the_dispatcher_cannot_spend_past_the_budget_even_though_it_tries(
    live: LiveServer,
    api: httpx.AsyncClient,
    db_engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,
    observability: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Five items, a ceiling of two, and the shipped loop asked to run all five.

    The dispatcher is not asked to behave. It is asked to do as much work as it
    can, and the server is what stops it - which is the entire claim of section
    3 and the reason the check sits inside ``_acquire_turn`` rather than in this
    process. Two turns reach the executor, the third is refused, and the run
    stops rather than grinding through three more refusals.

    The un-run items matter as much as the refused one. Each keeps its slot, so
    a later run under a fresh hour picks them up: a stop that stranded them at
    ``claimed`` would be a stop that silently dropped work.
    """
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 2)
    agent = await _bound_agent(api)
    source = _tasks_file(tmp_path, 5)

    report, printed = await _run(_options(source, agent, live, max_tasks=5))

    assert len(fake_executor.runs) == 2, "the ceiling is two, so two turns happened"
    assert _counter(db_engine) == 2

    statuses = [result.status for result in report.results]
    assert statuses == [
        ClaimStatus.COMPLETED,
        ClaimStatus.COMPLETED,
        ClaimStatus.PAUSED_QUOTA,
    ], statuses
    assert report.stopped_early
    assert "hourly allowance" in (report.results[-1].detail or "")
    assert "stopping the run" in printed

    # Two of five were worked, so three rows are still queued for a later hour.
    listed = await api.get(TASKS_URL, params={"limit": 50})
    assert listed.status_code == 200, listed.text
    queued = [task for task in listed.json()["tasks"] if task["status"] == "queued"]
    assert len(queued) == 2, listed.text


async def test_the_budget_survives_the_dispatcher_being_restarted(
    live: LiveServer,
    api: httpx.AsyncClient,
    db_engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,
    observability: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Three separate dispatcher processes, one shared hour.

    Each ``dispatch_once`` builds its own client, its own ledger and its own
    in-memory state, which is what a restart is. Because the count lives in
    Postgres rather than in the loop, run three meets the ceiling that runs one
    and two spent - and a crash-restart loop is therefore bounded rather than
    being an unbounded spend that looks like normal operation.

    Run three is also the one that proves the *enforcement* survived and not
    merely the advisory read: it has one turn of allowance left, spends it, and
    is refused on its second.
    """
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 3)
    agent = await _bound_agent(api)

    first, _ = await _run(_options(_tasks_file(tmp_path, 1, name="run-a"), agent, live))
    assert [r.status for r in first.results] == [ClaimStatus.COMPLETED]
    assert _counter(db_engine) == 1

    second, _ = await _run(_options(_tasks_file(tmp_path, 1, name="run-b"), agent, live))
    assert [r.status for r in second.results] == [ClaimStatus.COMPLETED]
    assert _counter(db_engine) == 2

    third, _ = await _run(
        _options(_tasks_file(tmp_path, 2, name="run-c"), agent, live, max_tasks=2)
    )
    assert [r.status for r in third.results] == [
        ClaimStatus.COMPLETED,
        ClaimStatus.PAUSED_QUOTA,
    ]
    assert len(fake_executor.runs) == 3
    assert _counter(db_engine) == 3

    # And a fourth process reading the state cold sees the spent hour, without
    # having been told anything by the three that spent it.
    async with DispatchClient(base_url=live.base_url, api_key=TEST_ADMIN_API_KEY) as cold:
        state = await cold.read_dispatch_state()
    assert state.budget.turns_used_this_hour == 3
    assert state.budget.turns_remaining_this_hour == 0


async def test_a_spent_hour_stops_a_fresh_run_before_it_claims_anything(
    live: LiveServer,
    api: httpx.AsyncClient,
    db_engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,
    observability: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """The advisory pre-check, doing the only job it is allowed to have.

    It saves the run from importing rows and opening sessions it cannot use. It
    is emphatically not the ceiling - that has already been proved to be the
    server - so this test asserts only what an optimisation is entitled to
    assert: no turn, no task, and a message that names the hour.
    """
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 1)
    agent = await _bound_agent(api)

    spent, _ = await _run(_options(_tasks_file(tmp_path, 1, name="spend"), agent, live))
    assert [r.status for r in spent.results] == [ClaimStatus.COMPLETED]

    report, printed = await _run(
        _options(_tasks_file(tmp_path, 1, name="after"), agent, live)
    )
    assert report.results == []
    assert report.stopped_early
    assert "hourly turn allowance" in (report.stop_reason or "")
    assert "stopping the run" in printed

    assert len(fake_executor.runs) == 1
    listed = await api.get(TASKS_URL, params={"limit": 50})
    assert len(listed.json()["tasks"]) == 1, "the stopped run imported nothing"


# ---------------------------------------------------------------------------
# A stop is reported as a stop
# ---------------------------------------------------------------------------


async def test_a_pause_reaches_the_dispatcher_as_a_stop_and_not_as_a_quota(
    live: LiveServer,
    api: httpx.AsyncClient,
    tmp_path: Path,
    executor_enabled: None,
    observability: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Somebody pressed a button; the terminal has to say so.

    ``paused_quota`` is the ledger status for both a spent budget and a thrown
    switch, because both mean "more turns cannot help right now". What must not
    be shared is the *message*: telling an operator their namespace is over
    quota when an admin paused it sends them to the wrong dial during the one
    hour they can least afford it.
    """
    agent = await _bound_agent(api)
    paused = await api.post(f"{DISPATCH_URL}/pause", json={"reason": "incident 12"})
    assert paused.status_code == 200, paused.text

    report, printed = await _run(_options(_tasks_file(tmp_path, 2, name="paused"), agent, live))

    assert report.results == []
    assert report.stopped_early
    assert "paused" in (report.stop_reason or "")
    assert "incident 12" in (report.stop_reason or "")
    assert "incident 12" in printed
    assert fake_executor.runs == []


async def test_a_halt_thrown_mid_run_stops_the_run_and_is_named_as_a_stop(
    live: LiveServer,
    api: httpx.AsyncClient,
    tmp_path: Path,
    executor_enabled: None,
    observability: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Pressed while the run is inside its first turn, so no pre-check can help.

    The advisory read at the top of the run has already returned clear. Item one
    is mid-flight in the executor when an operator throws the switch, which is
    exactly the shape of a real incident and exactly the case the dispatcher's
    own pre-check is useless for. What stops item two is the server, on the way
    in, and the loop's job is only to notice and stop rather than retry.

    The turn that was already running finishes, because no level short of
    killing the process reaches an invocation that is already under way.
    """
    agent = await _bound_agent(api)
    source = _tasks_file(tmp_path, 3, name="halted")

    fake_executor.gate = asyncio.Event()
    run = asyncio.create_task(_run(_options(source, agent, live, max_tasks=3)))
    await asyncio.wait_for(fake_executor.entered.wait(), 5.0)

    halted = await api.post(f"{DISPATCH_URL}/halt-executors", json={"reason": "runaway"})
    assert halted.status_code == 200, halted.text
    fake_executor.gate.set()

    report, printed = await asyncio.wait_for(run, 15.0)

    # One item worked, and then the run ended rather than walking through the
    # rest. Item two produces no result at all, which is the honest record: the
    # claim was refused, so nothing was taken out of the queue and nothing about
    # that item happened.
    assert [r.status for r in report.results] == [ClaimStatus.COMPLETED], [
        (r.status, r.detail) for r in report.results
    ]
    assert report.stopped_early
    assert "fleet ceiling" in (report.stop_reason or "")
    assert "runaway" in (report.stop_reason or ""), (
        "the reason an admin typed has to reach the terminal, or the next person "
        "starts the run again"
    )
    assert len(fake_executor.runs) == 1, "nothing new ran after the switch went on"
    assert "stopping the run" in printed
    assert "already" not in printed, (
        "a fleet stop must not be reported as an ordinary lost race for the row"
    )

    # And the two it never reached are still queued for whoever releases it.
    listed = await api.get(TASKS_URL, params={"limit": 50})
    queued = [task for task in listed.json()["tasks"] if task["status"] == "queued"]
    assert len(queued) == 2, listed.text


# ---------------------------------------------------------------------------
# Section 11.4: the delay, in the field the dispatcher reads
# ---------------------------------------------------------------------------


async def test_both_429s_hand_the_dispatcher_a_number_rather_than_a_sentence(
    live: LiveServer,
    api: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,
    observability: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Asserted through ``DispatchHTTPError``, which is what actually reads it.

    Section 11.4's complaint was that the delay existed only inside prose, so a
    dispatcher had to regex an English sentence or invent a sleep. Both refusals
    - the hourly turn ceiling and the hourly task ceiling - are provoked here
    with the dispatcher's own client, and both must hand back a usable number.
    The ``Retry-After`` header is checked to agree with it, because a body and a
    header that disagree is worse than either alone.
    """
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 1)
    monkeypatch.setattr(dispatch_settings, "default_max_tasks_per_hour", 2)
    agent = await _bound_agent(api)

    async with DispatchClient(base_url=live.base_url, api_key=TEST_ADMIN_API_KEY) as client:
        # The turn ceiling. One dispatch turn is allowed; the second is not.
        first = await _spend_one_turn(client, agent, tmp_path, name="delay-a")
        assert first is None, first

        turn_refusal = await _spend_one_turn(client, agent, tmp_path, name="delay-b")
        assert turn_refusal is not None
        assert turn_refusal.status_code == 429
        assert turn_refusal.error_code == "DISPATCH_BUDGET_EXCEEDED"
        assert turn_refusal.disposition is Disposition.FLEET_STOPPED
        assert isinstance(turn_refusal.retry_after_seconds, float)
        assert turn_refusal.retry_after_seconds > 0

        # The import ceiling. Two tasks exist already, and the ceiling is two.
        with pytest.raises(DispatchHTTPError) as raised:
            await client.import_tasks(
                items=await _items(tmp_path, 2, name="delay-c"),
                source_kind="file",
                dry_run=True,
            )
    import_refusal = raised.value
    assert import_refusal.status_code == 429
    assert import_refusal.error_code == "DISPATCH_BUDGET_EXCEEDED"
    assert isinstance(import_refusal.retry_after_seconds, float)
    assert import_refusal.retry_after_seconds > 0

    # And the header agrees with the body, read straight off the wire. The
    # refusal is on the commit and not on the preview, because the preview
    # inserts nothing: a ceiling counted in the transaction that inserts cannot
    # fire in a transaction that does not.
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": "raw-1", "title": "raw"}],
    }
    preview = await api.post(f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview"})
    assert preview.status_code == 200, preview.text
    raw = await api.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )
    assert raw.status_code == 429, raw.text
    assert raw.headers["Retry-After"] == str(raw.json()["details"]["retry_after_seconds"])


async def _items(tmp_path: Path, count: int, *, name: str) -> list[SourceItem]:
    """Read a throwaway YAML file through the real file source."""
    return await FileTaskSource(_tasks_file(tmp_path, count, name=name)).poll(cursor=None)


async def _spend_one_turn(
    client: DispatchClient, agent: str, tmp_path: Path, *, name: str
) -> DispatchHTTPError | None:
    """Import one item, claim it, open its session and start its turn.

    The dispatcher's own methods, in the dispatcher's own order. Returns the
    refusal if there was one, so a test can assert on the error the loop would
    have classified.
    """
    imported = await client.import_tasks(
        items=await _items(tmp_path, 1, name=name), source_kind="file", dry_run=True
    )
    task_key = str(imported.task_keys[0])
    await client.claim_task(task_key=task_key, instance_id="tester")
    session_key = await client.create_session(
        agent_name=agent, title=f"dispatch {name}", task_key=task_key
    )
    try:
        await client.start_turn(session_key=session_key, message="go")
    except DispatchHTTPError as exc:
        return exc
    return None
