"""Shared fakes.

The HTTP boundary is faked in two places and they test different things.
:class:`StubClient` stands in for the whole :class:`DispatchClient` and is how
the control flow is tested without a server. ``test_client.py`` fakes one layer
lower, at ``httpx.MockTransport``, because the failure table and the deny-event
correlation are properties of the client itself and a stub of the client cannot
show them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from agent_control_dispatcher import dispatch as dispatch_module
from agent_control_dispatcher.client import DispatchHTTPError
from agent_control_models.dispatch import DispatchBudget, DispatchStateSnapshot
from agent_control_models.linear import ListMilestoneIssuesResponse
from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.sessions import TurnResponse

BLOCKED_TURN_TEXT = "Pattern '\\b\\d{3}-\\d{2}-\\d{4}\\b' found"
"""Captured 2026-08-02 from a live post-stage deny by the ``block-ssn`` control.

The whole of what a blocked turn looked like. It is the control's own verdict
string and nothing else, which is the observation section 9.3 was missing."""


def blocked_turn_payload(session_key: str = "sk", trace_id: str = "ac554b66") -> dict[str, Any]:
    """A ``TurnResponse`` in the shape a blocked turn actually arrived in.

    Timestamps are ISO strings rather than ``datetime``s so the same payload can
    be handed to ``httpx.Response(json=...)`` and to ``model_validate``.
    """

    now = dt.datetime.now(dt.UTC)
    return {
        "session_key": session_key,
        "trace_id": trace_id,
        "started_at": (now - dt.timedelta(seconds=3)).isoformat(),
        "completed_at": now.isoformat(),
        "duration_seconds": 3.0,
        "messages": [
            {
                "index": 0,
                "role": "agent",
                "author": "root_agent",
                "parts": [{"kind": "text", "text": BLOCKED_TURN_TEXT}],
            }
        ],
    }


def deny_event_payload(**overrides: Any) -> dict[str, Any]:
    """One control-execution event as the observability query returns it."""

    payload: dict[str, Any] = {
        "control_execution_id": "ce-1",
        "trace_id": "4a6a4583",
        "span_id": "s1",
        "timestamp": dt.datetime.now(dt.UTC).isoformat(),
        "agent_name": "marketing_researcher",
        "control_id": 1,
        "control_name": "block-ssn",
        "check_stage": "post",
        "applies_to": "llm_call",
        "action": "deny",
        "matched": True,
        "confidence": 1.0,
    }
    payload.update(overrides)
    return payload


def open_dispatch_state(
    *,
    paused: bool = False,
    executors_halted: bool = False,
    turns_remaining: int = 60,
    tasks_remaining: int = 20,
    reason: str | None = None,
) -> DispatchStateSnapshot:
    """A namespace nobody has stopped, unless the caller says otherwise."""

    now = dt.datetime.now(dt.UTC)
    return DispatchStateSnapshot(
        paused=paused,
        paused_at=now if paused else None,
        paused_reason=reason if paused else None,
        executors_halted=executors_halted,
        executors_halted_at=now if executors_halted else None,
        executors_halted_reason=reason if executors_halted else None,
        budget=DispatchBudget(
            max_turns_per_hour=60,
            turns_used_this_hour=60 - turns_remaining,
            turns_remaining_this_hour=turns_remaining,
            max_tasks_per_hour=20,
            tasks_created_this_hour=20 - tasks_remaining,
            tasks_remaining_this_hour=tasks_remaining,
            window_started_at=now,
            window_resets_at=now + dt.timedelta(hours=1),
        ),
        updated_at=now,
    )


class StubClient:
    """Stands in for :class:`DispatchClient`. Records every call."""

    def __init__(self, **_: Any) -> None:
        self.turns: list[str] = []
        self.deleted: list[str] = []
        self.created: list[tuple[str, str]] = []
        self.session_task_keys: list[str | None] = []
        self.raise_on_turn: dict[int, DispatchHTTPError] = {}
        self.raise_on_session: dict[int, DispatchHTTPError] = {}
        self.raise_on_deny_query: dict[int, DispatchHTTPError] = {}
        self.raise_on_delete: DispatchHTTPError | None = None
        self.deny_on_turn: set[int] = set()
        self.text_on_turn: dict[int, str] = {}
        self.scope_reads: list[tuple[str, str]] = []
        self.milestone_response: ListMilestoneIssuesResponse | None = None
        self.raise_on_scope_read: Exception | None = None
        self.dispatch_state: DispatchStateSnapshot = open_dispatch_state()
        self.raise_on_dispatch_state: DispatchHTTPError | None = None
        self.dispatch_state_reads = 0

    async def read_dispatch_state(self) -> DispatchStateSnapshot:
        """The namespace's ceilings, as the pre-run header reads them.

        Defaults to a namespace with both switches off and its hour untouched,
        so a test that is about something else does not have to script one.
        """

        self.dispatch_state_reads += 1
        if self.raise_on_dispatch_state is not None:
            raise self.raise_on_dispatch_state
        return self.dispatch_state

    async def fetch_milestone_issues(
        self, *, team_slug: str, milestone_id: str
    ) -> ListMilestoneIssuesResponse:
        """The scope read, as the Linear source sees it.

        Recorded rather than performed, so a test can assert the ordinary case
        no session is opened in: the read refused, and nothing downstream of it
        ever ran.
        """

        self.scope_reads.append((team_slug, milestone_id))
        if self.raise_on_scope_read is not None:
            raise self.raise_on_scope_read
        assert self.milestone_response is not None, "no scope read was scripted"
        return self.milestone_response

    async def __aenter__(self) -> StubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def create_session(
        self, *, agent_name: str, title: str, task_key: str | None = None
    ) -> str:
        index = len(self.created)
        self.session_task_keys.append(task_key)
        self.created.append((agent_name, title))
        if index in self.raise_on_session:
            raise self.raise_on_session[index]
        return f"session-{index}"

    async def start_turn(self, *, session_key: str, message: str) -> TurnResponse:
        index = len(self.turns)
        self.turns.append(message)
        if index in self.raise_on_turn:
            raise self.raise_on_turn[index]
        now = dt.datetime.now(dt.UTC)
        return TurnResponse.model_validate(
            {
                "session_key": session_key,
                "trace_id": f"trace-{index}",
                "started_at": now,
                "completed_at": now,
                "duration_seconds": 1.0,
                "messages": [
                    {
                        "index": 0,
                        "role": "agent",
                        "author": "root_agent",
                        "parts": [
                            {
                                "kind": "text",
                                "text": self.text_on_turn.get(index, f"answer {index}"),
                            }
                        ],
                    }
                ],
            }
        )

    async def deny_events_for_turn(self, **_: Any) -> list[ControlExecutionEvent]:
        index = len(self.turns) - 1
        if index in self.raise_on_deny_query:
            raise self.raise_on_deny_query[index]
        if index not in self.deny_on_turn:
            return []
        return [ControlExecutionEvent.model_validate(deny_event_payload())]

    async def delete_session(self, *, session_key: str) -> None:
        if self.raise_on_delete is not None:
            raise self.raise_on_delete
        self.deleted.append(session_key)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubClient:
    client = StubClient()
    monkeypatch.setattr(dispatch_module, "DispatchClient", lambda **kwargs: client)
    return client
