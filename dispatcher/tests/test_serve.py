"""``serve``: the poll-claim-execute-release loop, against a fake control plane.

Driven at ``httpx.MockTransport`` rather than by stubbing the client, for the
reason ``test_dispatch_chain.py`` gives: half of what is worth pinning here is
the *shape* of the traffic, and most of that is traffic that must not happen. A
stub of the client would agree with whatever the loop decided to send.

Six properties, and the first one is a security boundary rather than a
behaviour:

**serve imports nothing.** Section 4 makes the human press over a displayed set
of issues the whole authorization for milestone scope, so a process a scheduler
can start must never reach ``POST /agent-tasks/import`` and must never name a
scope. That is asserted against the recorded traffic, not against the source.

**A queued row runs without anybody reading a source.** Which is the point: the
console writes the row, this claims it, the agent runs.

**A stopped namespace and a stopped server are both survived.** Neither exits
the process, and neither turns into a hot loop against a control plane whose
operator is already having a bad day.

**An outage does not spend the queue.** The rows keep their slots, and the
sweep is what hands them back out once the lease on them lapses. A credential
the server rejects is the opposite case: it exits, because a dispatcher that
idles on one is indistinguishable from a dispatcher with nothing to do.

**The lease is refreshed while a chain runs**, or a long task gets taken out
from under itself.

**A signal stops it claiming and lets the task in flight finish**, so a
container restart does not leave a row at ``running`` with no owner.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import signal
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from agent_control_dispatcher import client as client_module
from agent_control_dispatcher import loop as loop_module
from agent_control_dispatcher.client import DispatchClient
from agent_control_dispatcher.loop import ServeOptions, ShutdownRequest, serve
from test_dispatch_chain import RESEARCHER, WRITER, FakeControlPlane, _open_state, _planned


class ServePlane(FakeControlPlane):
    """The chain fake, plus what a poll loop needs of it.

    Three additions. The queue answers only rows that are still ``queued``, so
    a completed task stops being handed back out and the loop is not tested
    against an impossible server. The dispatch state is settable, so a pause
    can be scripted. And a read counter can ask for shutdown, which is how a
    test ends a loop that is otherwise designed never to end.
    """

    def __init__(self, refs: list[str], *, stop: ShutdownRequest, **kwargs: Any) -> None:
        super().__init__(refs, **kwargs)
        self.stop = stop
        self.state: dict[str, Any] = _open_state()
        self.queue_reads = 0
        self.state_reads = 0
        self.queue_failures = 0
        """How many of the first queue reads answer 503 before one succeeds."""
        self.queue_refusal: tuple[int, str, str] | None = None
        self.state_refusal: tuple[int, str, str] | None = None
        self.turn_refusal: tuple[int, str, str] | None = None
        self.plan_refusal: tuple[int, str, str] | None = None
        self.stop_after_queue_reads: int | None = None
        self.stop_after_state_reads: int | None = None
        self.stop_on_turn: bool = False
        self.on_turn: Callable[[], None] | None = None
        self.dry_run = True
        self.team_for: dict[str, str] = {}
        self.workflow_for: dict[str, str] = {}
        self.body_for: dict[str, str] = {}
        self.lease_for: dict[str, str] = {}
        """Ref -> ``lease_expires_at``. Absent means the row was never claimed."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agent-dispatch"):
            self.calls.append((request.method, path))
            self.state_reads += 1
            if self.state_reads == self.stop_after_state_reads:
                self.stop.request("test")
            if self.state_refusal is not None:
                return _refused(self.state_refusal)
            return httpx.Response(200, json={"state": self.state})
        if path.endswith("/plan") and self.plan_refusal is not None:
            self.calls.append((request.method, path))
            return _refused(self.plan_refusal)
        if path.endswith("/agent-tasks") and request.method == "GET":
            self.calls.append((request.method, path))
            self.queue_reads += 1
            if self.queue_reads == self.stop_after_queue_reads:
                self.stop.request("test")
            if self.queue_refusal is not None:
                status, code, detail = self.queue_refusal
                return httpx.Response(status, json={"error_code": code, "detail": detail})
            if self.queue_reads <= self.queue_failures:
                return httpx.Response(
                    503,
                    json={"error_code": "EXECUTOR_UNAVAILABLE", "detail": "server restarting"},
                )
            return self._queue(request)
        if path.endswith("/turns"):
            if self.stop_on_turn:
                self.stop.request("test")
            if self.on_turn is not None:
                self.on_turn()
            if self.turn_refusal is not None:
                self.calls.append((request.method, path))
                return _refused(self.turn_refusal)
        return super().handler(request)

    def _queue(self, request: httpx.Request) -> httpx.Response:
        wanted = request.url.params.get("status")
        tasks = [self._task(key) for key in self.refs if self.task_status.get(key) == wanted]
        return httpx.Response(
            200,
            json={
                "tasks": tasks,
                "pagination": {
                    "limit": 50,
                    "total": len(tasks),
                    "next_cursor": None,
                    "has_more": False,
                },
            },
        )

    def _task(self, key: str, **overrides: Any) -> dict[str, Any]:
        payload = super()._task(key, **overrides)
        ref = self.refs.get(key, "")
        payload["dry_run"] = self.dry_run
        payload["body"] = self.body_for.get(ref, payload["body"])
        payload["team_slug"] = self.team_for.get(ref, payload["team_slug"])
        payload["workflow_key"] = self.workflow_for.get(ref, payload["workflow_key"])
        if ref in self.lease_for:
            payload["heartbeat_at"] = self.lease_for[ref]
            payload["lease_expires_at"] = self.lease_for[ref]
        return payload


def _refused(refusal: tuple[int, str, str]) -> httpx.Response:
    status, code, detail = refusal
    return httpx.Response(status, json={"error_code": code, "detail": detail})


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the wall-clock out of the loop and out of the deny query.

    Every interval this module exercises is a real one; only its length is
    replaced. The backoff curve, the jitter and the hold on a stopped namespace
    all still run, and the settle window still polls the observability store -
    a fake transport just never delays the flush the real one waits for.
    """
    monkeypatch.setattr(loop_module, "HELD_POLL_SECONDS", 0.001)
    monkeypatch.setattr(loop_module, "MAX_BACKOFF_SECONDS", 0.01)
    original = DispatchClient.deny_events_for_turn

    async def once(self: DispatchClient, **kwargs: Any) -> Any:
        kwargs.setdefault("settle_seconds", 0.0)
        return await original(self, **kwargs)

    monkeypatch.setattr(DispatchClient, "deny_events_for_turn", once)


def _options(**overrides: Any) -> ServeOptions:
    defaults: dict[str, Any] = {
        "base_url": "http://localhost:8000",
        "api_key": "k",
        "poll_seconds": 0.001,
    }
    defaults.update(overrides)
    return ServeOptions(**defaults)


def _serve(
    plane: ServePlane,
    options: ServeOptions,
    monkeypatch: pytest.MonkeyPatch,
    stop: ShutdownRequest | None,
) -> tuple[int, str]:
    """Run the loop against ``plane``. ``stop`` of ``None`` installs the real
    signal handlers, which is the only way to exercise them."""
    real = DispatchClient.__init__

    def with_transport(self: DispatchClient, **kwargs: Any) -> None:
        real(self, transport=httpx.MockTransport(plane.handler), **kwargs)

    monkeypatch.setattr(DispatchClient, "__init__", with_transport)
    out = io.StringIO()

    async def go() -> int:
        # A loop designed never to end needs a deadline in a test, or a bug in
        # the stop condition hangs the suite instead of failing it.
        return await asyncio.wait_for(serve(options, out=out, shutdown=stop), timeout=10)

    return asyncio.run(go()), out.getvalue()


def _one_step_plane(stop: ShutdownRequest, refs: list[str] | None = None) -> ServePlane:
    plane = ServePlane(refs or ["t1"], stop=stop, plan_steps=[_planned(0, RESEARCHER)])
    plane.stop_after_queue_reads = 2
    return plane


# ---------------------------------------------------------------------------
# What serve must not do
# ---------------------------------------------------------------------------


def test_serve_never_reaches_the_import_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The press is the authorization; a scheduler must not be able to forge it.

    Milestone scope is reachable only from an interactive ``mode: "commit"``
    import. If ``serve`` could import, a process started by cron or by a
    container restart could put issues in front of the fleet that no human ever
    saw, which is the one thing section 4's confirm exists to prevent.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)

    code, _ = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert plane.turns, "the queued row did run; this is not a vacuous pass"
    assert not [path for _, path in plane.calls if path.endswith("/agent-tasks/import")]
    assert not [path for _, path in plane.calls if "/milestones/" in path]


def test_serve_never_sends_a_scope_or_a_dry_run_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """It cannot widen what a human agreed to, because it never states it.

    ``dry_run`` was fixed on the row when the row was created. A serve that
    sent one anywhere would be a serve that could turn somebody's dry run into
    a live one after the fact.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.dry_run = False

    code, _ = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert plane.turns, "a row created live still runs; it is not silently skipped"
    assert not [path for path, body in plane.bodies if "dry_run" in body]
    assert not [path for path, body in plane.bodies if "scope" in body]


# ---------------------------------------------------------------------------
# The loop itself
# ---------------------------------------------------------------------------


def test_a_queued_row_runs_with_no_source_and_no_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.body_for["t1"] = "the issue as somebody filed it"

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert [turn["agent_name"] for turn in plane.turns] == [RESEARCHER]
    assert "the issue as somebody filed it" in plane.turns[0]["message"]
    assert plane.task_status[plane.keys["t1"]] == "completed"
    assert "t1" in text


def test_an_idle_loop_does_not_narrate_every_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line every five seconds is a log nobody reads."""
    stop = ShutdownRequest()
    plane = ServePlane([], stop=stop)
    plane.stop_after_queue_reads = 6

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert plane.queue_reads == 6
    assert text.count("claiming queued tasks") == 1


def test_max_tasks_bounds_one_pass_rather_than_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["t1", "t2", "t3"])
    plane.stop_after_queue_reads = 2

    code, _ = _serve(plane, _options(max_tasks=2), monkeypatch, stop)

    assert code == 0
    assert len(plane.turns) == 2, "one pass ran two, and the loop stopped before the next"


def test_only_the_named_teams_rows_are_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["mine", "theirs"])
    plane.team_for = {"mine": "marketing", "theirs": "operations"}

    code, _ = _serve(plane, _options(team_slug="marketing"), monkeypatch, stop)

    assert code == 0
    assert plane.task_status[plane.keys["mine"]] == "completed"
    assert plane.task_status[plane.keys["theirs"]] == "queued"


def test_a_row_carrying_another_workflow_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["mine", "theirs"])
    plane.workflow_for = {"mine": "research", "theirs": "support"}

    code, _ = _serve(plane, _options(workflow_key="research"), monkeypatch, stop)

    assert code == 0
    assert plane.task_status[plane.keys["mine"]] == "completed"
    assert plane.task_status[plane.keys["theirs"]] == "queued"


def test_the_lease_is_refreshed_while_a_chain_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty minutes of chain under a claim taken once needs a heartbeat."""
    stop = ShutdownRequest()
    plane = ServePlane(["t1"], stop=stop, plan_steps=[_planned(0, RESEARCHER), _planned(1, WRITER)])
    plane.stop_after_queue_reads = 2

    code, _ = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    beats = [path for _, path in plane.calls if path.endswith("/heartbeat")]
    assert len(beats) == 2, "one before each hop, which is where section 5.4 puts it"


# ---------------------------------------------------------------------------
# Stopping, and being stopped
# ---------------------------------------------------------------------------


def test_a_paused_namespace_claims_nothing_and_says_so_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.stop_after_queue_reads = None
    plane.stop_after_state_reads = 4
    plane.state = {**_open_state(), "paused": True, "paused_reason": "incident 4"}

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert plane.queue_reads == 0, "a paused namespace is not polled for work"
    assert not [path for _, path in plane.calls if path.endswith("/claim")]
    assert text.count("incident 4") == 1, "four passes, one line"


def test_a_server_that_is_briefly_down_is_survived_rather_than_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed case: a 503 while the server restarts.

    A dispatcher that exits here is a dispatcher somebody has to go and start
    again, and the operator will not know that until they wonder why nothing
    ran.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.queue_failures = 3
    plane.stop_after_queue_reads = 5

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert plane.turns, "it recovered and ran the work once the server came back"
    assert text.count("the server did not answer") == 1, "one line, not one per retry"
    assert "the server is answering again" in text


def test_a_queue_read_the_credential_cannot_make_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission fault is not a blip, and sleeping on it hides it."""
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.stop_after_queue_reads = None
    plane.queue_refusal = (403, "AUTH_INSUFFICIENT_PRIVILEGES", "not permitted")

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 1
    assert "the server refused this dispatcher" in text
    assert not plane.turns


def test_a_key_the_server_does_not_recognise_exits_rather_than_idling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 is the failure that looks exactly like an idle dispatcher.

    The container is up, the restart policy never fires, one line scrolled past
    at startup, and every press of play sits in the queue forever. A key the
    server rejects is not going to start being accepted.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.stop_after_queue_reads = None
    plane.queue_refusal = (401, "AUTH_INVALID_KEY", "unknown key")

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 1
    assert "the server refused this dispatcher" in text
    assert not plane.turns


def test_a_stop_mid_task_lets_the_task_in_flight_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container restart must not strand a claimed row at ``running``.

    The signal stops it *claiming*. The task already in flight runs to its own
    ``finish`` call, which is the only thing that moves the row off
    ``running``; a loop that dropped it would leave the work invisible until a
    lease lapsed.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["t1", "t2"])
    plane.stop_after_queue_reads = None
    plane.stop_on_turn = True

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert plane.task_status[plane.keys["t1"]] == "completed"
    assert plane.task_status[plane.keys["t2"]] == "queued", "no new work was claimed"
    assert "nothing is claimed" in text


def test_an_executor_outage_parks_the_queue_rather_than_burning_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed 503, one layer in: the server answers, the executor does not.

    Nothing reaches a model, so nothing is any task's fault. A loop that
    recorded each one as ``failed`` and polled again immediately would walk the
    whole queue at HTTP speed and destroy every row anybody pressed play on,
    for as long as the outage lasted. The first row keeps its slot and the rest
    are not touched.
    """
    # The client's own three attempts are `test_dispatch_chain`'s subject and
    # six seconds of real sleep here. What this test is about starts after they
    # are spent.
    monkeypatch.setattr(client_module, "_UNAVAILABLE_RETRIES", 1)
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["t1", "t2", "t3"])
    plane.stop_after_queue_reads = 2
    plane.turn_refusal = (503, "EXECUTOR_UNAVAILABLE", "Cannot reach http://executor:8001")

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    touched = [key for key, status in plane.task_status.items() if status != "queued"]
    assert len(touched) == 1, "one row was attempted, not the page"
    assert plane.task_status[touched[0]] == "paused_quota", "it kept its slot"
    assert "the executor is not answering" in text


def test_a_plan_read_that_fails_does_not_leave_the_row_it_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one call in a claimed task's path that used to escape the outcome.

    Under ``once`` an abandoned ``running`` row cost one item and the process
    exited. A loop would claim a fresh row every pass and strand that one too,
    at a rate no lease reclaim can keep up with.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["t1", "t2"])
    plane.stop_after_queue_reads = 2
    plane.plan_refusal = (503, "EXECUTOR_UNAVAILABLE", "server restarting")

    code, _ = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert not plane.turns
    stranded = [key for key, status in plane.task_status.items() if status == "running"]
    assert not stranded, "the claim was closed rather than abandoned"
    assert plane.task_status[plane.keys["t2"]] == "queued"


def test_a_lapsed_lease_is_swept_back_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whoever held this row is gone, and nothing else would ever look for it.

    ``?status=queued`` cannot see a row at ``running``, so without the sweep a
    dispatcher killed mid-task leaves work that no restart and no second
    dispatcher recovers - the lease expiring means nothing if nobody asks.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.stop_after_state_reads = 3
    plane.stop_after_queue_reads = None
    plane.task_status[plane.keys["t1"]] = "running"
    plane.lease_for["t1"] = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()

    code, _ = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert plane.task_status[plane.keys["t1"]] == "completed"


def test_a_live_lease_is_left_to_the_dispatcher_holding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.stop_after_state_reads = 3
    plane.stop_after_queue_reads = None
    plane.task_status[plane.keys["t1"]] = "running"
    plane.lease_for["t1"] = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()

    code, _ = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert not [path for _, path in plane.calls if path.endswith("/claim")]
    assert plane.task_status[plane.keys["t1"]] == "running"


def test_a_failed_state_read_is_not_reported_as_a_clear_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` from the fleet read means two things and only one is good news.

    An operator scanning a log during an outage should not find a line saying
    polling is happening, printed on the strength of a read that failed.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop)
    plane.stop_after_queue_reads = None
    plane.stop_after_state_reads = 3
    plane.state_refusal = (503, "EXECUTOR_UNAVAILABLE", "server restarting")

    code, text = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert "claiming queued tasks" not in text
    assert plane.queue_reads == 0, "no queue read on a namespace whose state is unknown"
    assert "the server did not answer" in text


def test_a_finished_task_is_forgotten_rather_than_accumulated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger held for weeks must not keep an issue body per task ever run."""
    ledgers: list[Any] = []
    real = loop_module.ServerTaskLedger.__init__

    def remember(self: Any, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)
        ledgers.append(self)

    monkeypatch.setattr(loop_module.ServerTaskLedger, "__init__", remember)
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["t1", "t2"])
    plane.stop_after_queue_reads = 3

    code, _ = _serve(plane, _options(), monkeypatch, stop)

    assert code == 0
    assert len(plane.turns) == 2, "both rows ran; this is not a vacuous pass"
    ledger = ledgers[0]
    assert ledger._task_keys == {}
    assert ledger._claimed == {}
    assert ledger._open_steps == set()


def test_a_real_sigterm_lets_the_task_in_flight_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler the compose ``stop_grace_period`` is sized around.

    Every other test here sets the event directly, which proves the loop stops
    but says nothing about the signal path: ``_install_signal_handlers`` only
    runs when no ``ShutdownRequest`` is passed in. This one raises the real
    signal, from inside the turn, at a loop that installed its own handlers.
    """
    stop = ShutdownRequest()
    plane = _one_step_plane(stop, refs=["t1", "t2"])
    plane.stop_after_queue_reads = None
    plane.on_turn = lambda: signal.raise_signal(signal.SIGTERM)

    code, text = _serve(plane, _options(), monkeypatch, None)

    assert code == 0
    assert plane.task_status[plane.keys["t1"]] == "completed"
    assert plane.task_status[plane.keys["t2"]] == "queued", "no new work was claimed"
    assert "SIGTERM received" in text


# ---------------------------------------------------------------------------
# The options
# ---------------------------------------------------------------------------


def test_max_tasks_above_the_cap_is_refused_not_clamped() -> None:
    with pytest.raises(ValueError, match="hard cap"):
        _options(max_tasks=9)


def test_a_poll_interval_of_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        _options(poll_seconds=0)
