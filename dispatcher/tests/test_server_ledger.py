"""The shipped ledger, against a fake ``agent_tasks`` that answers over the wire.

``ClaimLedger`` had tests from slice 1 and every one of them passes
``--ledger``, so the whole existing dispatcher suite exercises the *local*
adapter. The server ledger is what actually runs, and this file is its
coverage.

Faked at ``httpx.MockTransport`` rather than by stubbing
:class:`DispatchClient`, because half of what is worth pinning here is the
shape of the requests: which route is called, in what order, and what the body
does and does not carry. A stub of the client would agree with whatever the
ledger did.

Three properties are the reason this file exists.

**The task key reaches the session before the session is opened.** The binding
lands in ``agent_sessions.agent_task_id``, which is the column the turn path
reads to tell a fleet turn from a human's chat. A ledger that recorded it
afterwards would leave the column null on every session it opened, and every
ceiling keying off it - the namespace budget, the dispatch pause, the kill
switch - would silently not apply. A column that is always null looks exactly
like a column nobody has needed yet.

**A lost claim is not an error.** Two dispatchers contend and one loses, which
is an ordinary outcome of the design and has to leave the run going.

**The step is written before the task, and it carries the output.** The session
is deleted when the task ends, so ``output_text`` on the step is the only
durable copy of what the agent produced.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest
from agent_control_dispatcher.client import DispatchClient, DispatchHTTPError, Disposition
from agent_control_dispatcher.ledger import ClaimStatus
from agent_control_dispatcher.server_ledger import (
    INSTANCE_ID_ENV,
    ServerTaskLedger,
    default_instance_id,
)
from agent_control_dispatcher.sources.base import SourceItem

SOURCE_KIND = "file"

SESSION_KEY = "5" * 32
"""Session keys are 32 hex characters at the model boundary."""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _task_payload(
    *,
    task_key: str,
    source_ref: str,
    status: str = "queued",
    steps: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_key": task_key,
        "source_kind": SOURCE_KIND,
        "source_ref": source_ref,
        "source_url": None,
        "title": f"title for {source_ref}",
        "body": "",
        "team_slug": None,
        "workflow_key": "default",
        "status": status,
        "dry_run": True,
        "current_step": 0,
        "turns_used": 0,
        "claimed_by": None,
        "deadline_at": _now(),
        "chain_trace_id": "chain-1",
        "failure_code": None,
        "failure_detail": None,
        "created_at": _now(),
        "updated_at": _now(),
        "steps": steps or [],
    }
    payload.update(overrides)
    return payload


def _step_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step_index": 0,
        "agent_name": "marketing_researcher",
        "brief": "",
        "status": "completed",
        "session_key": SESSION_KEY,
        "turn_trace_id": "trace-0",
        "output_text": "what the agent said",
        "output_truncated": False,
        "attempts": 1,
        "started_at": _now(),
        "ended_at": _now(),
    }
    payload.update(overrides)
    return payload


class FakeLedgerServer:
    """The routes the ledger talks to, recorded and answerable.

    Deliberately not a reimplementation of the server: it records what was
    asked and returns what a healthy server would, plus whatever refusal a test
    scripts. What is being tested is the dispatcher's half of the conversation.
    """

    def __init__(self, refs: list[str]) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.keys = {ref: f"{index:032x}" for index, ref in enumerate(refs, start=1)}
        self.queue_status_reads: list[str | None] = []
        self.claim_status: dict[str, int] = {}
        self.claim_error: dict[str, str] = {}
        """Which refusal a claim answers with. Defaults to the lost race; a
        fleet stop is a different answer to a different question and the ledger
        has to tell them apart."""
        self.resume_step_index = 0
        self.prior_status = "queued"
        self.reclaimed = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content)
            self.bodies.append((path, body))

        if path.endswith("/agent-tasks/import"):
            return self._import(body)
        if path.endswith("/agent-tasks") and request.method == "GET":
            return self._queue(request)
        if path.endswith("/claim"):
            return self._claim(path, body)
        if path.endswith("/heartbeat"):
            return httpx.Response(
                200,
                json={
                    "task_key": path.split("/")[-2],
                    "status": "running",
                    "heartbeat_at": _now(),
                    "lease_expires_at": _now(),
                    "deadline_at": _now(),
                },
            )
        if path.endswith("/steps"):
            return httpx.Response(
                200,
                json={
                    "step": _step_payload(
                        step_index=body["step_index"], status="running", output_text=None
                    ),
                    "task": _task_payload(
                        task_key=path.split("/")[-2], source_ref="r", status="running"
                    ),
                },
            )
        if path.endswith("/finish") and "/steps/" in path:
            return httpx.Response(
                200,
                json={
                    "step": _step_payload(),
                    "task": _task_payload(
                        task_key=path.split("/")[-3], source_ref="r", status="running"
                    ),
                },
            )
        if path.endswith("/finish"):
            return httpx.Response(
                200,
                json={
                    "task": _task_payload(
                        task_key=path.split("/")[-2],
                        source_ref="r",
                        status=body.get("status", "completed"),
                    )
                },
            )
        if request.method == "GET":
            key = path.rsplit("/", 1)[-1]
            ref = next((r for r, k in self.keys.items() if k == key), "r")
            return httpx.Response(
                200,
                json={
                    "task": _task_payload(
                        task_key=key,
                        source_ref=ref,
                        status="completed",
                        steps=[_step_payload()],
                    )
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    def _import(self, body: dict[str, Any]) -> httpx.Response:
        refs = [item["source_ref"] for item in body["scope"]["items"]]
        mode = body["mode"]
        return httpx.Response(
            200,
            json={
                "mode": mode,
                "eligible": [
                    {"source_ref": ref, "title": f"title for {ref}", "source_url": None}
                    for ref in refs
                ],
                # The dispatcher quotes this back on the commit; a wrong value
                # here would be a 409 from the real server.
                "refs_digest": "sha256:" + "a" * 64,
                "skipped": {},
                "workflow_key": "default",
                "dry_run": body["dry_run"],
                "created": len(refs) if mode == "commit" else 0,
                "task_keys": [self.keys[ref] for ref in refs] if mode == "commit" else [],
            },
        )

    def _queue(self, request: httpx.Request) -> httpx.Response:
        status = request.url.params.get("status")
        self.queue_status_reads.append(status)
        tasks = (
            [
                _task_payload(task_key=key, source_ref=ref)
                for ref, key in self.keys.items()
            ]
            if status == "queued"
            else []
        )
        return httpx.Response(
            200,
            json={
                "tasks": tasks,
                "pagination": {
                    "limit": 100,
                    "total": len(tasks),
                    "next_cursor": None,
                    "has_more": False,
                },
            },
        )

    def _claim(self, path: str, body: dict[str, Any]) -> httpx.Response:
        key = path.split("/")[-2]
        status = self.claim_status.get(key, 200)
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "error_code": self.claim_error.get(key, "TASK_ALREADY_CLAIMED"),
                    "detail": "another dispatcher holds a live lease on it",
                },
            )
        ref = next((r for r, k in self.keys.items() if k == key), "r")
        return httpx.Response(
            200,
            json={
                "task": _task_payload(
                    task_key=key,
                    source_ref=ref,
                    status="running",
                    claimed_by=body["instance_id"],
                ),
                "prior_status": self.prior_status,
                "resume_step_index": self.resume_step_index,
                "reclaimed": self.reclaimed,
                "abandoned_step_indexes": [],
                "lease_expires_at": _now(),
                "lease_seconds": 1800,
            },
        )


def _items(*refs: str) -> list[SourceItem]:
    return [SourceItem(ref=ref, title=f"title for {ref}", body="") for ref in refs]


def _client(server: FakeLedgerServer) -> DispatchClient:
    return DispatchClient(
        base_url="http://localhost:8000",
        api_key="local-agent-key",
        transport=httpx.MockTransport(server.handler),
    )


def _paths(server: FakeLedgerServer) -> list[str]:
    return [path for _, path in server.calls]


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


async def test_register_previews_then_commits_and_learns_which_key_is_which() -> None:
    """Two import calls, the second quoting the first, then a queue read.

    The map from a source ref to its task key comes from the queue rather than
    from what the import returned, because the queue is the authority on what
    is still claimable and an import's answer is a snapshot another dispatcher
    may already have acted on.
    """
    server = FakeLedgerServer(["t1", "t2"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")

        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1", "t2"), dry_run=True)

    modes = [body["mode"] for path, body in server.bodies if path.endswith("/import")]
    assert modes == ["preview", "commit"]
    commit = next(body for path, body in server.bodies if body.get("mode") == "commit")
    assert commit["expected_refs_digest"] == "sha256:" + "a" * 64
    assert ledger.session_task_key(source_kind=SOURCE_KIND, ref="t1") == server.keys["t1"]
    assert ledger.session_task_key(source_kind=SOURCE_KIND, ref="t2") == server.keys["t2"]


async def test_register_with_no_items_talks_to_nobody() -> None:
    server = FakeLedgerServer([])
    async with _client(server) as client:
        await ServerTaskLedger(client, instance_id="inst-a").register(
            source_kind=SOURCE_KIND, items=[], dry_run=True
        )

    assert server.calls == []


async def test_a_rerun_over_an_already_imported_set_still_finds_its_keys() -> None:
    """Re-running the same source is resumable rather than duplicative.

    The import reports the refs as already queued and creates nothing; the
    queue read that follows is what makes the second run able to pick them up
    anyway. A ledger that trusted ``task_keys`` from the commit would find an
    empty list here and quietly do nothing.
    """
    server = FakeLedgerServer(["t1"])
    original = server._import

    def created_nothing(body: dict[str, Any]) -> httpx.Response:
        response = original(body)
        payload = json.loads(response.content)
        payload["created"] = 0
        payload["task_keys"] = []
        return httpx.Response(200, json=payload)

    server._import = created_nothing  # type: ignore[method-assign]

    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)

    assert ledger.session_task_key(source_kind=SOURCE_KIND, ref="t1") == server.keys["t1"]


async def test_register_asks_for_the_statuses_that_still_hold_their_ref() -> None:
    """A queued poll alone would miss a task its own previous run left stuck.

    ``paused_quota`` and ``running`` both keep the source ref by the partial
    unique index, so a ledger that read only the queued page would find no key
    for that ref, skip it silently, and leave it un-runnable for ever.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        await ServerTaskLedger(client, instance_id="inst-a").register(
            source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True
        )

    assert set(server.queue_status_reads) >= {"queued", "running", "paused_quota"}


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


async def test_a_lost_claim_is_reported_as_false_rather_than_raised() -> None:
    """Two dispatchers contend and one loses. That is the design working."""
    server = FakeLedgerServer(["t1", "t2"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1", "t2"), dry_run=True)
        server.claim_status[server.keys["t1"]] = 409

        lost = await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="marketing_researcher", dry_run=True
        )
        won = await ledger.claim(
            source_kind=SOURCE_KIND, ref="t2", agent_name="marketing_researcher", dry_run=True
        )

    assert lost is False
    assert won is True


async def test_a_fleet_stop_on_the_claim_is_raised_rather_than_reported_as_a_lost_race(
) -> None:
    """A switch thrown mid-run is not "somebody else has this row".

    The claim gained two refusals that are about the *namespace* and not about
    the item: a dispatch pause and the executor kill switch. Both arrive as a
    409, exactly like a lost race, and swallowing them would send the run
    through every remaining item printing "already held by another dispatcher"
    and then finish reporting a clean pass over a fleet that had been stopped.
    Nothing would have been spent - the turn path refuses too - but the operator
    watching the terminal would have no idea their stop had landed.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)
        server.claim_status[server.keys["t1"]] = 409
        server.claim_error[server.keys["t1"]] = "EXECUTORS_HALTED"

        with pytest.raises(DispatchHTTPError) as raised:
            await ledger.claim(
                source_kind=SOURCE_KIND,
                ref="t1",
                agent_name="marketing_researcher",
                dry_run=True,
            )

    assert raised.value.error_code == "EXECUTORS_HALTED"
    assert raised.value.disposition is Disposition.FLEET_STOPPED


async def test_an_item_with_no_task_row_is_not_claimed() -> None:
    """Nothing was registered for it, so there is nothing to take.

    Claiming anyway would mean running a turn against a task the ledger has no
    row for, which is the whole of what the ledger is supposed to prevent.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")

        claimed = await ledger.claim(
            source_kind=SOURCE_KIND, ref="never-imported", agent_name="a_agent", dry_run=True
        )

    assert claimed is False
    assert server.calls == []


async def test_a_claim_body_carries_the_instance_and_nothing_else() -> None:
    """``dry_run`` was fixed on the row at import and is not a per-claim choice.

    A dispatcher that could flip it would be able to turn a dry run into a live
    one after a human had agreed to the former.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)

        await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="a_agent", dry_run=False
        )

    claim_body = next(body for path, body in server.bodies if path.endswith("/claim"))
    assert claim_body == {"instance_id": "inst-a"}


# ---------------------------------------------------------------------------
# The binding, and the step
# ---------------------------------------------------------------------------


async def test_the_session_binding_is_available_before_the_session_is_opened() -> None:
    """It is sent when the session is created, not recorded afterwards.

    ``agent_task_id`` is what the turn path reads to tell a fleet turn from a
    human's chat. Set after the fact, it would be null on the one request that
    matters and every ceiling keyed off it would not apply.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)
        await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="a_agent", dry_run=True
        )

        key = ledger.session_task_key(source_kind=SOURCE_KIND, ref="t1")

    assert key == server.keys["t1"]
    assert ledger.session_task_key(source_kind=SOURCE_KIND, ref="unknown") is None


async def test_the_step_is_opened_after_a_heartbeat_and_carries_the_session() -> None:
    """The heartbeat is not ceremony: the turn that follows is the longest call.

    The lease started when the claim did, and refreshing here is what stops a
    slow step being reclaimed underneath itself.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)
        await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="a_agent", dry_run=True
        )

        await ledger.record_session(
            source_kind=SOURCE_KIND,
            ref="t1",
            session_key=SESSION_KEY,
            agent_name="a_agent",
            brief="work this task",
        )

    key = server.keys["t1"]
    tail = [path for path in _paths(server) if path.startswith(f"/api/v1/agent-tasks/{key}")]
    assert tail == [
        f"/api/v1/agent-tasks/{key}/claim",
        f"/api/v1/agent-tasks/{key}/heartbeat",
        f"/api/v1/agent-tasks/{key}/steps",
    ]
    step_body = next(body for path, body in server.bodies if path.endswith("/steps"))
    assert step_body == {
        "instance_id": "inst-a",
        "step_index": 0,
        "agent_name": "a_agent",
        "brief": "work this task",
        "session_key": SESSION_KEY,
    }


async def test_a_reclaimed_task_opens_its_step_at_the_index_the_server_reported() -> None:
    """Resume position comes from the claim response, never from a local count.

    The dispatcher that ran the earlier steps is gone, so this process has no
    memory of them at all; the server read them out of the step rows.
    """
    server = FakeLedgerServer(["t1"])
    server.resume_step_index = 2
    server.prior_status = "running"
    server.reclaimed = True
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-b")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)
        await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="a_agent", dry_run=True
        )

        await ledger.record_session(
            source_kind=SOURCE_KIND,
            ref="t1",
            session_key=SESSION_KEY,
            agent_name="a_agent",
            brief="",
        )

    step_body = next(body for path, body in server.bodies if path.endswith("/steps"))
    assert step_body["step_index"] == 2


# ---------------------------------------------------------------------------
# Finish
# ---------------------------------------------------------------------------


async def test_finish_closes_the_step_with_its_output_and_then_the_task() -> None:
    """The step write carries the durable record.

    The session is deleted when the task ends, so a task row that recorded only
    a status would have thrown the agent's work away.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)
        await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="a_agent", dry_run=True
        )
        await ledger.record_session(
            source_kind=SOURCE_KIND,
            ref="t1",
            session_key=SESSION_KEY,
            agent_name="a_agent",
            brief="",
        )

        await ledger.finish(
            source_kind=SOURCE_KIND,
            ref="t1",
            status=ClaimStatus.COMPLETED,
            turn_trace_id="trace-0",
            output_text="the three reports shared one cause",
        )

    key = server.keys["t1"]
    assert _paths(server)[-2:] == [
        f"/api/v1/agent-tasks/{key}/steps/0/finish",
        f"/api/v1/agent-tasks/{key}/finish",
    ]
    step_finish = next(
        body for path, body in server.bodies if path.endswith("/steps/0/finish")
    )
    assert step_finish["status"] == "completed"
    assert step_finish["output_text"] == "the three reports shared one cause"
    assert step_finish["turn_trace_id"] == "trace-0"


async def test_a_failure_before_the_session_existed_writes_only_the_task() -> None:
    """There is no step to close, and asking the server to close one would turn
    one failure into a crash."""
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)
        await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="a_agent", dry_run=True
        )

        await ledger.finish(
            source_kind=SOURCE_KIND,
            ref="t1",
            status=ClaimStatus.BLOCKED,
            outcome_code="NO_ENABLED_BINDING",
            detail="the agent has no runtime binding",
        )

    key = server.keys["t1"]
    assert f"/api/v1/agent-tasks/{key}/steps/0/finish" not in _paths(server)
    assert _paths(server)[-1] == f"/api/v1/agent-tasks/{key}/finish"


@pytest.mark.parametrize(
    "claim_status, expected",
    [
        (ClaimStatus.COMPLETED, "completed"),
        (ClaimStatus.FAILED, "failed"),
        (ClaimStatus.BLOCKED, "blocked"),
        (ClaimStatus.PAUSED_QUOTA, "paused_quota"),
        (ClaimStatus.RUNNING_UNKNOWN, "running_unknown"),
    ],
)
async def test_every_ending_keeps_its_own_name_across_the_wire(
    claim_status: ClaimStatus, expected: str
) -> None:
    """``blocked`` and ``failed`` are not synonyms on either side of the wire.

    ``failed`` means the work was attempted and did not work; ``blocked`` means
    it was never attempted because the configuration is wrong, and a loop
    retrying it produces the same result forever. Collapsing them would make
    the second look like bad luck.
    """
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)
        await ledger.claim(
            source_kind=SOURCE_KIND, ref="t1", agent_name="a_agent", dry_run=True
        )

        await ledger.finish(source_kind=SOURCE_KIND, ref="t1", status=claim_status)

    task_finish = next(
        body
        for path, body in reversed(server.bodies)
        if path.endswith("/finish") and "/steps/" not in path
    )
    assert task_finish["status"] == expected


async def test_finishing_an_item_the_ledger_never_registered_writes_nothing() -> None:
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")

        await ledger.finish(
            source_kind=SOURCE_KIND, ref="never-imported", status=ClaimStatus.FAILED
        )

    assert server.calls == []


async def test_get_reads_the_task_back_and_reports_its_last_step() -> None:
    server = FakeLedgerServer(["t1"])
    async with _client(server) as client:
        ledger = ServerTaskLedger(client, instance_id="inst-a")
        await ledger.register(source_kind=SOURCE_KIND, items=_items("t1"), dry_run=True)

        claim = await ledger.get(source_kind=SOURCE_KIND, ref="t1")

    assert claim is not None
    assert claim.status is ClaimStatus.COMPLETED
    assert claim.session_key == SESSION_KEY
    assert claim.turn_trace_id == "trace-0"
    assert await ledger.get(source_kind=SOURCE_KIND, ref="unknown") is None


# ---------------------------------------------------------------------------
# The instance id, which every fence on the server side compares against
# ---------------------------------------------------------------------------


def test_two_instances_in_one_process_do_not_share_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server fences step and finish writes on this value.

    Two instances sharing one id would be able to write to each other's claimed
    tasks, which is the exact thing the lease exists to arbitrate. A hostname
    and a pid are not enough: two containers can share both.
    """
    monkeypatch.delenv(INSTANCE_ID_ENV, raising=False)

    assert default_instance_id() != default_instance_id()
    assert len(default_instance_id()) <= 64


def test_an_operator_supplied_instance_id_is_used_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(INSTANCE_ID_ENV, "  runner-seven  ")
    assert default_instance_id() == "runner-seven"

    monkeypatch.setenv(INSTANCE_ID_ENV, "x" * 200)
    assert len(default_instance_id()) == 64


def test_a_run_that_names_no_ledger_gets_the_server_one(tmp_path: Any) -> None:
    """The default moved with the ledger and the flag did not.

    ``--ledger`` used to be how you configured the claim; it is now how you opt
    out of it, back to a local file that coordinates nothing. A run that names
    nothing must get the claim two dispatchers can contend for, because that is
    the difference between a dispatcher that can run unattended and one that
    cannot.
    """
    from agent_control_dispatcher.dispatch import DispatchOptions, _build_ledger
    from agent_control_dispatcher.ledger import LocalTaskLedger

    server = FakeLedgerServer([])
    base = {
        "source_spec": "file://tasks.yaml",
        "agent_name": "researcher",
        "base_url": "http://localhost:8000",
        "api_key": "local-agent-key",
    }

    client = _client(server)
    default = _build_ledger(DispatchOptions(**base), client=client)
    opted_out = _build_ledger(
        DispatchOptions(**base, ledger_path=tmp_path / "claims.sqlite3"), client=client
    )

    assert isinstance(default, ServerTaskLedger)
    assert isinstance(opted_out, LocalTaskLedger)
    assert default.session_task_key(source_kind=SOURCE_KIND, ref="t1") is None
    assert opted_out.session_task_key(source_kind=SOURCE_KIND, ref="t1") is None, (
        "a local run has no server row for a session to bind to, and says so"
    )
