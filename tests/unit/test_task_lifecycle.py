"""Unit tests for the task lifecycle state machine."""

from __future__ import annotations

import pytest

from agent_squad.errors import ValidationError
from agent_squad.models.task import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    TaskState,
    TaskStatus,
    assert_transition,
)


def test_every_status_has_a_transition_entry() -> None:
    for status in TaskStatus:
        assert status in LEGAL_TRANSITIONS


def test_terminal_states_have_no_successors() -> None:
    for status in TERMINAL_STATUSES:
        assert LEGAL_TRANSITIONS[status] == frozenset()


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.RETRY_SCHEDULED),
        (TaskStatus.RETRY_SCHEDULED, TaskStatus.READY),
        (TaskStatus.TIMED_OUT, TaskStatus.RETRY_SCHEDULED),
        (TaskStatus.FAILED, TaskStatus.READY),
        (TaskStatus.BLOCKED, TaskStatus.PENDING),
    ],
)
def test_legal_transitions_are_allowed(current: TaskStatus, target: TaskStatus) -> None:
    assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.RUNNING),
        (TaskStatus.PENDING, TaskStatus.SUCCEEDED),
        (TaskStatus.READY, TaskStatus.SUCCEEDED),
        (TaskStatus.SUCCEEDED, TaskStatus.RUNNING),
        (TaskStatus.CANCELLED, TaskStatus.READY),
        (TaskStatus.FAILED, TaskStatus.SUCCEEDED),
    ],
)
def test_illegal_transitions_are_rejected(current: TaskStatus, target: TaskStatus) -> None:
    with pytest.raises(ValidationError):
        assert_transition(current, target)


def test_transition_to_a_self_transition_is_allowed() -> None:
    assert_transition(TaskStatus.RUNNING, TaskStatus.RUNNING)


def test_task_state_transition_updates_the_timestamp() -> None:
    state = TaskState(id="t", run_id="r", status=TaskStatus.PENDING)
    before = state.updated_at
    state.transition_to(TaskStatus.READY)
    assert state.status is TaskStatus.READY
    assert state.updated_at >= before


def test_task_state_rejects_an_illegal_transition() -> None:
    state = TaskState(id="t", run_id="r", status=TaskStatus.PENDING)
    with pytest.raises(ValidationError):
        state.transition_to(TaskStatus.SUCCEEDED)


def test_is_terminal() -> None:
    assert TaskState(id="t", run_id="r", status=TaskStatus.SUCCEEDED).is_terminal
    assert TaskState(id="t", run_id="r", status=TaskStatus.CANCELLED).is_terminal
    assert not TaskState(id="t", run_id="r", status=TaskStatus.RUNNING).is_terminal
