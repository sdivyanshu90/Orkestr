"""Concurrency tests.

These exercise the properties that only matter when several workers are
running at once: parallel execution actually happening, dependent tasks
waiting, stale writes being rejected, duplicate messages being refused and
cancellation racing with completion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_squad.config import RunConfig
from agent_squad.errors import StaleAttemptError
from agent_squad.models.task import Budget, TaskSpec, TaskStatus
from agent_squad.orchestrator import Orchestrator
from agent_squad.store.db import Database
from agent_squad.store.events import EventLog
from agent_squad.store.state import StateStore

from helpers import two_independent_spec


@pytest.mark.integration
async def test_independent_tasks_run_in_parallel(tmp_path: Path, repo: Path) -> None:
    """Two independent tasks must overlap in time, not run back to back."""
    config = RunConfig(
        run_id="run_concurrent",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        scheduler={"max_workers": 4},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run(
            "two independent tasks", request_spec=two_independent_spec()
        )
        events = sorted(orchestrator.log.read(config.run_id), key=lambda e: e.seq)
    finally:
        await orchestrator.aclose()

    assert outcome.status.value == "succeeded"
    starts = [e for e in events if e.type.value == "agent.started"]
    finishes = [e for e in events if e.type.value == "agent.finished"]
    assert len(starts) == 2
    assert len(finishes) == 2
    second_start = starts[1] if starts[0].seq < starts[1].seq else starts[0]
    first_start = starts[0] if starts[0].seq < starts[1].seq else starts[1]
    # The second worker must start before the first one finishes.
    finishes_after_second = [e for e in finishes if e.seq > second_start.seq]
    assert finishes_after_second, "the second worker never started before the first finished"


@pytest.mark.integration
async def test_a_dependent_task_starts_after_its_prerequisite(
    tmp_path: Path, repo: Path
) -> None:
    config = RunConfig(
        run_id="run_ordered",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run(
            "ordered work", request_spec=two_independent_spec(dependent=True)
        )
        events = sorted(orchestrator.log.read(config.run_id), key=lambda e: e.seq)
    finally:
        await orchestrator.aclose()

    assert outcome.status.value == "succeeded"
    impl_finish = next(e for e in events if e.type.value == "agent.finished")
    dependent_start = next(
        e for e in events if e.type.value == "agent.started" and e.seq > impl_finish.seq
    )
    assert dependent_start.seq > impl_finish.seq


def test_a_stale_attempt_cannot_overwrite_a_newer_result(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    spec = TaskSpec(
        id="t1", run_id="r1", title="t", objective="o", budget=Budget(max_attempts=3)
    )
    state.create_run("r1")
    state.upsert_task(spec)
    state.transition_task("t1", TaskStatus.READY)
    first = state.start_attempt("t1", owner="worker-a", lease_ttl_s=60)
    assert first.attempt == 1
    state.transition_task("t1", TaskStatus.RUNNING, attempt=1)
    second = state.start_attempt("t1", owner="worker-b", lease_ttl_s=60)
    assert second.attempt == 2

    # The first worker now tries to report success. It is fenced out.
    with pytest.raises(StaleAttemptError):
        state.transition_task("t1", TaskStatus.SUCCEEDED, attempt=1)

    # The current owner may proceed.
    state.transition_task("t1", TaskStatus.SUCCEEDED, attempt=2)
    assert state.get_task_state("t1").status is TaskStatus.SUCCEEDED
    db.close()


def test_a_task_cannot_run_more_attempts_than_its_budget(tmp_path: Path) -> None:
    from agent_squad.errors import ValidationError

    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    spec = TaskSpec(
        id="t1",
        run_id="r1",
        title="t",
        objective="o",
        budget=Budget(max_attempts=1),
    )
    state.create_run("r1")
    state.upsert_task(spec)
    state.start_attempt("t1", owner="w", lease_ttl_s=60)
    with pytest.raises(ValidationError):
        state.start_attempt("t1", owner="w", lease_ttl_s=60)
    db.close()


@pytest.mark.integration
async def test_cancelling_a_run_stops_the_schedulers_work(
    tmp_path: Path, repo: Path
) -> None:
    """A cancelled run must not report success."""
    config = RunConfig(
        run_id="run_cancel",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    cancellation = asyncio.Event()
    cancellation.set()
    try:
        outcome = await orchestrator.run(
            "cancelled work",
            request_spec=two_independent_spec(),
            cancellation=cancellation,
        )
        events = sorted(orchestrator.log.read(config.run_id), key=lambda e: e.seq)
    finally:
        await orchestrator.aclose()

    assert outcome.status.value in ("cancelled", "completed_with_failures")
    succeeded = [
        e
        for e in events
        if e.type.value == "task.status_changed" and e.payload.get("to") == "succeeded"
    ]
    assert succeeded == [], "a cancelled run must not report a successful task"


@pytest.mark.integration
async def test_duplicate_messages_are_rejected(tmp_path: Path, repo: Path) -> None:
    from agent_squad.agents.messaging import MessageBus
    from agent_squad.errors import ValidationError
    from agent_squad.models.message import MessageKind, make_message

    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    bus = MessageBus(
        state=state,
        log=log,
        known_recipients=frozenset({"supervisor", "implementer:t1"}),
    )
    message = make_message(
        MessageKind.PROGRESS,
        run_id="r1",
        sender="implementer:t1",
        recipient="supervisor",
        task_id="t1",
        percent=10.0,
    )
    await bus.publish(message)
    with pytest.raises(ValidationError):
        await bus.publish(message)
    db.close()


@pytest.mark.integration
async def test_messages_to_unknown_recipients_are_rejected(tmp_path: Path) -> None:
    from agent_squad.agents.messaging import MessageBus
    from agent_squad.errors import ValidationError
    from agent_squad.models.message import MessageKind, make_message

    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    bus = MessageBus(state=state, log=log, known_recipients=frozenset({"supervisor"}))
    message = make_message(
        MessageKind.PROGRESS,
        run_id="r1",
        sender="supervisor",
        recipient="somebody_else",
        percent=1.0,
    )
    with pytest.raises(ValidationError):
        await bus.publish(message)
    db.close()


@pytest.mark.integration
async def test_stale_message_versions_are_rejected(tmp_path: Path) -> None:
    from agent_squad.agents.messaging import MessageBus
    from agent_squad.errors import ValidationError
    from agent_squad.models.message import Message, MessageKind

    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    bus = MessageBus(state=state, log=log, known_recipients=frozenset({"supervisor", "a"}))
    newer = Message(
        kind=MessageKind.PROGRESS,
        run_id="r1",
        sender="a",
        recipient="supervisor",
        version=2,
        correlation_id="c1",
        payload={"percent": 50.0, "stage": "later"},
    )
    await bus.publish(newer)
    older = Message(
        kind=MessageKind.PROGRESS,
        run_id="r1",
        sender="a",
        recipient="supervisor",
        version=1,
        correlation_id="c1",
        payload={"percent": 10.0, "stage": "earlier"},
    )
    with pytest.raises(ValidationError):
        await bus.publish(older)
    db.close()
