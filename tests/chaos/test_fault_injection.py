"""Chaos and fault-injection tests.

Each test injects one fault and asserts the *recovery contract*: what the
runtime must do, and what it must not do (report success, lose work, or
corrupt state).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from helpers import two_independent_spec

from agent_squad.benchmark.chaos import (
    Fault,
    FaultSpec,
    InjectedFaultProvider,
    assert_safe_failure,
)
from agent_squad.config import RunConfig
from agent_squad.errors import StaleAttemptError
from agent_squad.models.task import Budget, TaskSpec, TaskStatus
from agent_squad.orchestrator import Orchestrator
from agent_squad.providers.mock import MockProvider
from agent_squad.store.db import Database
from agent_squad.store.events import EventLog
from agent_squad.store.state import StateStore


def make_config(tmp_path: Path, repo: Path, run_id: str, **overrides) -> RunConfig:
    payload = {
        "run_id": run_id,
        "repo_path": str(repo),
        "data_dir": str(tmp_path / "data"),
        "provider": {"name": "mock", "model": "mock-deterministic-v1"},
        "retry": {"max_attempts": 2, "initial_backoff_s": 0.01, "max_backoff_s": 0.05},
        "scheduler": {"max_workers": 2, "default_task_timeout_s": 60},
    }
    payload.update(overrides)
    return RunConfig.model_validate(payload)


@pytest.mark.chaos
async def test_a_transient_provider_failure_is_retried(tmp_path: Path, repo: Path) -> None:
    """A flaky provider must not lose the work."""
    config = make_config(tmp_path, repo, "chaos_flaky")
    provider = InjectedFaultProvider(inner=MockProvider())
    provider.inject(FaultSpec(kind=Fault.PROVIDER_FLAKY, times=1))
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data", provider=provider)
    try:
        outcome = await orchestrator.run("flaky work", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    assert outcome.status.value == "succeeded"
    assert provider.calls >= 3  # two tasks, one of which needed a retry
    total_attempts = sum(o.attempts for o in outcome.outcomes.values())
    assert total_attempts > 2, "the retry did not happen"


@pytest.mark.chaos
async def test_a_permanently_unavailable_provider_fails_safely(
    tmp_path: Path, repo: Path
) -> None:
    """An unavailable provider must produce a recorded failure, not a fake success."""
    config = make_config(tmp_path, repo, "chaos_down")
    provider = InjectedFaultProvider(inner=MockProvider())
    provider.inject(FaultSpec(kind=Fault.PROVIDER_UNAVAILABLE, times=1000))
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data", provider=provider)
    try:
        outcome = await orchestrator.run("doomed work", request_spec=two_independent_spec())
        states = orchestrator.state.list_task_states(config.run_id)
    finally:
        await orchestrator.aclose()

    assert outcome.status.value == "completed_with_failures"
    failed = [s for s in states if s.status is TaskStatus.FAILED]
    assert failed, "the failure was not recorded"
    for state in failed:
        assert state.error_class
        assert state.error_message
        assert state.attempt >= 1
    assert_safe_failure(
        status=outcome.status.value,
        failed_tasks=[s.id for s in failed],
    )


@pytest.mark.chaos
async def test_a_worker_crash_does_not_corrupt_the_run(tmp_path: Path, repo: Path) -> None:
    """An unexpected exception in a worker must be contained."""
    from agent_squad.agents.runner import WorkerRunner
    from agent_squad.benchmark.chaos import install_runner_crash

    config = make_config(tmp_path, repo, "chaos_crash")
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        # Wrap the runner the scheduler will use.
        original = WorkerRunner.execute
        calls = {"n": 0}

        async def crashing_execute(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected worker crash")
            return await original(self, *args, **kwargs)

        WorkerRunner.execute = crashing_execute  # type: ignore[method-assign]
        try:
            outcome = await orchestrator.run(
                "crashy work", request_spec=two_independent_spec()
            )
        finally:
            WorkerRunner.execute = original  # type: ignore[method-assign]
        states = orchestrator.state.list_task_states(config.run_id)
    finally:
        await orchestrator.aclose()

    # The crash must not have produced a task in an impossible state.
    for state in states:
        assert state.status in set(TaskStatus), state.status
    assert outcome.status.value in ("succeeded", "completed_with_failures")
    # Whatever happened, the run must still be readable and consistent.
    assert len(states) == 2


@pytest.mark.chaos
async def test_the_crashy_runner_wrapper_raises_then_recovers() -> None:
    """The documented wrapper behaves as advertised."""
    from agent_squad.benchmark.chaos import install_runner_crash

    class Inner:
        async def execute(self, *args, **kwargs):
            return "ok"

    wrapper = install_runner_crash(Inner(), crashes=1)
    with pytest.raises(RuntimeError):
        await wrapper.execute()
    assert await wrapper.execute() == "ok"


@pytest.mark.chaos
async def test_the_provider_fault_installer_applies_the_faults() -> None:
    from agent_squad.benchmark.chaos import install_provider_faults
    from agent_squad.errors import TransientModelError
    from agent_squad.providers.base import ModelRequest
    from agent_squad.providers.mock import MockProvider

    provider = install_provider_faults(
        MockProvider(), [FaultSpec(kind=Fault.PROVIDER_FLAKY, times=1)]
    )
    request = ModelRequest(messages=[])
    with pytest.raises(TransientModelError):
        await provider.complete(request)
    # The second call succeeds.
    await provider.complete(request)


@pytest.mark.chaos
async def test_a_timeout_is_recorded_as_a_timeout(tmp_path: Path, repo: Path) -> None:
    """A worker that overruns its deadline must be timed out, not hang the run."""
    config = make_config(
        tmp_path,
        repo,
        "chaos_timeout",
        scheduler={"max_workers": 1, "default_task_timeout_s": 0.5},
        retry={"max_attempts": 1, "initial_backoff_s": 0.01},
    )
    provider = InjectedFaultProvider(inner=MockProvider())
    provider.inject(FaultSpec(kind=Fault.SLOW_WORKER, times=1000, delay_s=5.0))
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data", provider=provider)
    try:
        outcome = await orchestrator.run("slow work", request_spec=two_independent_spec())
        states = orchestrator.state.list_task_states(config.run_id)
    finally:
        await orchestrator.aclose()

    assert outcome.status.value == "completed_with_failures"
    assert any(s.status is TaskStatus.TIMED_OUT for s in states)


@pytest.mark.chaos
async def test_garbage_model_output_is_rejected_not_acted_on(
    tmp_path: Path, repo: Path
) -> None:
    """A model that returns malformed output must not corrupt the run."""
    from agent_squad.errors import ValidationError
    from agent_squad.planning.planner import LLMPlanner

    class GarbageProvider:
        name = "garbage"
        model = "garbage"

        async def complete(self, request):
            from agent_squad.providers.base import ModelResponse

            return ModelResponse(text="not json at all", provider="garbage", model="garbage")

        async def aclose(self):
            return None

    planner = LLMPlanner(GarbageProvider())
    from agent_squad.planning.planner import PlanRequest

    request = PlanRequest(run_id="r", objective="do something", repo_path=str(repo))
    with pytest.raises(ValidationError):
        await planner.plan(request)


@pytest.mark.chaos
async def test_a_stale_worker_cannot_overwrite_a_newer_result(tmp_path: Path) -> None:
    """The attempt fence must hold even under a delayed writer."""
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    state.create_run("r")
    spec = TaskSpec(
        id="t", run_id="r", title="t", objective="o", budget=Budget(max_attempts=5)
    )
    state.upsert_task(spec)
    state.transition_task("t", TaskStatus.READY)
    state.start_attempt("t", owner="worker-1", lease_ttl_s=60)
    state.transition_task("t", TaskStatus.RUNNING, attempt=1)

    # A second worker takes over.
    state.start_attempt("t", owner="worker-2", lease_ttl_s=60)
    state.transition_task("t", TaskStatus.RUNNING, attempt=2)

    # The first worker finally reports.
    with pytest.raises(StaleAttemptError):
        state.transition_task("t", TaskStatus.SUCCEEDED, attempt=1)

    assert state.get_task_state("t").status is TaskStatus.RUNNING
    assert state.get_task_state("t").owner == "worker-2"
    db.close()


@pytest.mark.chaos
async def test_a_duplicate_message_is_refused(tmp_path: Path) -> None:
    from agent_squad.agents.messaging import MessageBus
    from agent_squad.errors import ValidationError
    from agent_squad.models.message import MessageKind, make_message

    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    bus = MessageBus(state=state, log=log, known_recipients=frozenset({"a", "b"}))
    message = make_message(
        MessageKind.PROGRESS,
        run_id="r",
        sender="a",
        recipient="b",
        task_id="t",
        percent=10.0,
    )
    await bus.publish(message)
    with pytest.raises(ValidationError):
        await bus.publish(message)
    # The store must hold exactly one copy.
    assert len(state.list_messages("r")) == 1
    db.close()


@pytest.mark.chaos
async def test_conflicting_patches_are_detected_at_the_integration_boundary(
    tmp_path: Path, repo: Path
) -> None:
    """Two workers editing the same file must conflict at integration."""
    from agent_squad.benchmark.repo_fixture import conflicting_spec

    config = make_config(tmp_path, repo, "chaos_conflict")
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("conflict", request_spec=conflicting_spec())
    finally:
        await orchestrator.aclose()

    assert outcome.integration is not None
    assert outcome.integration.conflict_count >= 1
    assert outcome.integration.passed is False
    # Both workers succeeded in isolation, and the combination still failed.
    assert all(o.status is TaskStatus.SUCCEEDED for o in outcome.outcomes.values())


@pytest.mark.chaos
async def test_a_cancelled_run_leaves_no_task_in_a_running_state(
    tmp_path: Path, repo: Path
) -> None:
    """Cancellation must leave the run in a terminal, readable state."""
    config = make_config(tmp_path, repo, "chaos_cancel")
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    cancellation = asyncio.Event()
    cancellation.set()
    try:
        await orchestrator.run(
            "cancelled",
            request_spec=two_independent_spec(),
            cancellation=cancellation,
        )
        states = orchestrator.state.list_task_states(config.run_id)
    finally:
        await orchestrator.aclose()

    for state in states:
        assert state.status is not TaskStatus.RUNNING, state.id
