"""A blocked task must be clearable, or its issue is pinned for ever.

``blocked`` is in neither ``TERMINAL_TASK_STATUSES`` (so it holds the source ref
and the issue cannot be queued again) nor ``RECLAIMABLE_TASK_STATUSES`` (so no
dispatcher takes it back). Before this, cancel refused it and resolve refused it,
so a task blocked on a configuration mistake could only be cleared by editing the
database - contradicting what ``TERMINAL_TASK_STATUSES`` says about itself.
"""

from __future__ import annotations

from agent_control_models.tasks import (
    RECLAIMABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    AgentTaskStatus,
)


def test_blocked_is_recoverable_by_neither_set_which_is_why_cancel_takes_it() -> None:
    """The premise, pinned. If either set ever gains BLOCKED, revisit cancel."""
    assert AgentTaskStatus.BLOCKED not in TERMINAL_TASK_STATUSES
    assert AgentTaskStatus.BLOCKED not in RECLAIMABLE_TASK_STATUSES


def test_cancel_accepts_queued_and_blocked_and_nothing_else() -> None:
    from agent_control_server.services.agent_tasks import _CANCELLABLE_TASK_STATUSES

    assert _CANCELLABLE_TASK_STATUSES == {
        AgentTaskStatus.QUEUED,
        AgentTaskStatus.BLOCKED,
    }
    # Nothing that ran or is running: those end at an outcome or at a halt.
    for status in (
        AgentTaskStatus.RUNNING,
        AgentTaskStatus.COMPLETED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.RUNNING_UNKNOWN,
    ):
        assert status not in _CANCELLABLE_TASK_STATUSES
