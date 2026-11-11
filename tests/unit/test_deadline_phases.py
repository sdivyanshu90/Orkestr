"""R1: the absolute deadline is enforced in *every* phase.

The exit criterion promises one absolute deadline covering download/load,
inspection, initial planning, replanning, the provider queue/request, the
worker and its tools, verification, integration and teardown.  A claim is
not evidence: each phase is driven here through its real entry point with a
blocking operation injected at the documented seam
(:class:`~agent_squad.runtime.deadline.PhaseGate`), and the test observes
what the phase actually does:

* an asynchronous phase raises :class:`DeadlineExceeded` (or, where the
  phase deliberately converts the expiry into a result so the run can stop
  gracefully, reports the expiry as that phase and starts no work);
* the work behind the seam is never started;
* a cancellation is acknowledged within five seconds;
* a phase that starts after the expiry is refused before doing anything.

The deadlines are deliberately small and the blocking is injected, so the
suite is fast and deterministic; nothing here sleeps for the length of a
real deadline.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_squad.config import RunConfig
from agent_squad.dag.graph import TaskGraph
from agent_squad.errors import CancelledError as RunCancelledError
from agent_squad.integration.engine import IntegrationEngine
from agent_squad.models.task import AcceptanceCriterion, CheckKind, TaskSpec
from agent_squad.planning.inspector import RepositoryInspector
from agent_squad.planning.planner import BasePlanner, PlanRequest, RuleBasedPlanner
from agent_squad.providers.base import BaseProvider, ModelRequest, ModelResponse
from agent_squad.runtime import deadline as deadline_module
from agent_squad.runtime.deadline import (
    DeadlineExceeded,
    Phase,
    RunDeadline,
    RunScope,
    current_run_scope,
    run_phase,
    run_scope,
    teardown_window,
)
from agent_squad.scheduler.engine import Scheduler
from agent_squad.store.db import Database
from agent_squad.store.events import EventLog
from agent_squad.store.state import StateStore
from agent_squad.verification.gate import VerificationGate
from agent_squad.workspace.worktree import WorktreeManager

# ------------------------------------------------------------------ helpers


class BlockingGate:
    """A :class:`PhaseGate` that blocks one designated phase.

    The gate is the seam the runtime documents for injecting a blocking
    operation into a phase through the ordinary entry point.  It records
    every phase it was asked to enter, so a test can also prove that no
    later phase was started after the expiry.
    """

    def __init__(self, blocked: str) -> None:
        self.blocked = blocked
        self.async_entries: list[str] = []
        self.sync_entries: list[str] = []
        self.entered = threading.Event()
        self._async_release = asyncio.Event()
        self._sync_release = threading.Event()

    async def enter(self, phase: str) -> None:
        self.async_entries.append(phase)
        if phase == self.blocked:
            self.entered.set()
            await self._async_release.wait()

    def enter_sync(self, phase: str) -> None:
        self.sync_entries.append(phase)
        if phase == self.blocked:
            self.entered.set()
            self._sync_release.wait(20)

    def release(self) -> None:
        """Release a blocked gate so no daemon thread is left waiting."""
        self._async_release.set()
        self._sync_release.set()


def started_deadline(*, total_s: float = 0.4, reserve_s: float = 0.1) -> RunDeadline:
    """A deadline that is already running.

    ``started_at`` is set directly rather than through ``start()`` so the
    test does not install an ambient scope of its own: the ``run_scope``
    below is the one every phase must read.
    """
    deadline = RunDeadline(total_s=total_s, reserve_s=reserve_s)
    deadline.started_at = time.monotonic()
    return deadline


def expired_deadline() -> RunDeadline:
    """A deadline whose budget is already gone."""
    deadline = RunDeadline(total_s=0.1, reserve_s=0.0)
    deadline.started_at = time.monotonic() - 10.0
    return deadline


async def wait_until(event: Any, timeout: float = 2.0) -> None:
    """Wait for a threading event without blocking the event loop."""
    limit = time.monotonic() + timeout
    while not event.is_set():
        if time.monotonic() > limit:
            raise AssertionError("the phase was not entered within the timeout")
        await asyncio.sleep(0.005)


class RecordingProvider(BaseProvider):
    """A provider that records whether its request was actually dispatched."""

    name = "recording"

    def __init__(self) -> None:
        super().__init__("recording-model")
        self.dispatch_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.dispatch_calls += 1
        return ModelResponse(text="ok")


class NeverRunner:
    """A worker runner that fails the test if it is ever asked to run."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("the worker was started after the phase was blocked")


def make_task(*, task_id: str = "t1", run_id: str = "run_deadline") -> TaskSpec:
    """A task with one deterministic criterion, so verification is real."""
    return TaskSpec(
        id=task_id,
        run_id=run_id,
        title=task_id,
        objective="a bounded piece of work",
        acceptance_criteria=(
            AcceptanceCriterion(
                id="exists",
                description="the file exists",
                kind=CheckKind.FILE_EXISTS,
                spec={"path": "nothing.txt"},
            ),
        ),
    )


def make_scheduler(
    tmp_path: Path,
    *,
    gate: BlockingGate,
    deadline: RunDeadline,
    cancellation: asyncio.Event | None = None,
) -> tuple[Scheduler, NeverRunner, Database]:
    """A real scheduler over one real task, with a runner that must not run."""
    config = RunConfig(
        run_id="run_deadline",
        repo_path=str(tmp_path),
        data_dir=str(tmp_path / "data"),
        retry={"max_attempts": 1},
    )
    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    state.create_run(config.run_id)
    task = make_task(run_id=config.run_id)
    state.upsert_task(task)
    graph = TaskGraph()
    graph.add(task)
    runner = NeverRunner()
    scheduler = Scheduler(
        config=config,
        graph=graph,
        state=state,
        log=log,
        runner=runner,
        gate=VerificationGate(config),
        briefs={},
        deadline=deadline,
        cancellation=cancellation,
    )
    return scheduler, runner, db


# ----------------------------------------------------------------- download


async def test_download_phase_raises_deadline_exceeded() -> None:
    """A slow model load is bounded and reports the download phase."""
    gate = BlockingGate(Phase.DOWNLOAD.value)
    deadline = started_deadline()
    provider = RecordingProvider()
    request = ModelRequest(messages=[])
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate), pytest.raises(DeadlineExceeded) as raised:
            await provider.complete(request)
    finally:
        gate.release()

    assert raised.value.phase == Phase.DOWNLOAD.value
    assert provider.dispatch_calls == 0, "the request was dispatched after the block"
    assert gate.async_entries == [Phase.DOWNLOAD.value]
    assert deadline.observed_phase == Phase.DOWNLOAD.value
    assert time.monotonic() - started < 1.0


async def test_download_phase_acknowledges_cancellation() -> None:
    """A blocked download is cancelled promptly, not merely timed out."""
    gate = BlockingGate(Phase.DOWNLOAD.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    provider = RecordingProvider()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            call = asyncio.ensure_future(provider.complete(ModelRequest(messages=[])))
            await wait_until(gate.entered)
            cancellation.set()
            with pytest.raises(RunCancelledError):
                await asyncio.wait_for(call, timeout=5.0)
    finally:
        gate.release()

    assert provider.dispatch_calls == 0
    assert time.monotonic() - started < 5.0


# ----------------------------------------------------------------- provider


async def test_provider_phase_raises_deadline_exceeded() -> None:
    """A slow request is bounded and reports the provider phase."""
    gate = BlockingGate(Phase.PROVIDER.value)
    deadline = started_deadline()
    provider = RecordingProvider()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate), pytest.raises(DeadlineExceeded) as raised:
            await provider.complete(ModelRequest(messages=[]))
    finally:
        gate.release()

    assert raised.value.phase == Phase.PROVIDER.value
    assert provider.dispatch_calls == 0
    assert gate.async_entries == [Phase.DOWNLOAD.value, Phase.PROVIDER.value]
    assert deadline.observed_phase == Phase.PROVIDER.value
    assert time.monotonic() - started < 1.0


async def test_provider_phase_acknowledges_cancellation() -> None:
    """A request blocked in the provider queue is cancelled promptly."""
    gate = BlockingGate(Phase.PROVIDER.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    provider = RecordingProvider()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            call = asyncio.ensure_future(provider.complete(ModelRequest(messages=[])))
            await wait_until(gate.entered)
            cancellation.set()
            with pytest.raises(RunCancelledError):
                await asyncio.wait_for(call, timeout=5.0)
    finally:
        gate.release()

    assert provider.dispatch_calls == 0
    assert time.monotonic() - started < 5.0


# --------------------------------------------------------------- inspection


def test_inspection_phase_is_bounded_and_observes_the_expiry(tmp_path: Path) -> None:
    """A scan that cannot finish is bounded and reports the inspection phase.

    The inspector deliberately converts the expiry into an empty context so
    the run stops gracefully instead of raising out of the composition root;
    the durable fact is ``observed_phase`` and the absence of a fabricated
    survey.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    gate = BlockingGate(Phase.INSPECTION.value)
    deadline = started_deadline()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate):
            context = RepositoryInspector(tmp_path).inspect()
    finally:
        gate.release()

    assert context.files == []
    assert gate.sync_entries == [Phase.INSPECTION.value]
    assert deadline.observed_phase == Phase.INSPECTION.value
    assert time.monotonic() - started < 1.0


def test_inspection_phase_acknowledges_cancellation(tmp_path: Path) -> None:
    """A blocked scan notices cancellation through the sync seam."""
    gate = BlockingGate(Phase.INSPECTION.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            threading.Timer(0.1, cancellation.set).start()
            context = RepositoryInspector(tmp_path).inspect()
    finally:
        gate.release()

    assert context.files == []
    assert gate.sync_entries == [Phase.INSPECTION.value]
    assert time.monotonic() - started < 5.0


# ----------------------------------------------------------------- planning


async def test_planning_phase_is_bounded_and_reports_the_expiry() -> None:
    """A plan that cannot be produced stops at the deadline with no tasks."""
    gate = BlockingGate(Phase.PLANNING.value)
    deadline = started_deadline()
    planner: BasePlanner = RuleBasedPlanner()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate):
            result = await planner.plan(PlanRequest(run_id="r", objective="do work"))
    finally:
        gate.release()

    assert result.tasks == []
    assert any("could not be produced" in warning for warning in result.warnings)
    assert gate.async_entries == [Phase.PLANNING.value]
    assert deadline.observed_phase == Phase.PLANNING.value
    assert time.monotonic() - started < 1.0


async def test_planning_phase_acknowledges_cancellation() -> None:
    """A blocked plan is cancelled promptly and returns no tasks."""
    gate = BlockingGate(Phase.PLANNING.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    planner: BasePlanner = RuleBasedPlanner()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            plan = asyncio.ensure_future(
                planner.plan(PlanRequest(run_id="r", objective="do work"))
            )
            await wait_until(gate.entered)
            cancellation.set()
            result = await asyncio.wait_for(plan, timeout=5.0)
    finally:
        gate.release()

    assert result.tasks == []
    assert time.monotonic() - started < 5.0


# --------------------------------------------------------------- replanning


async def test_replanning_phase_raises_deadline_exceeded() -> None:
    """A revision is its own phase and is bounded like the initial plan.

    Unlike the initial plan, a revision that cannot be produced is refused
    (raised) rather than replaced with an empty plan: the supervisor keeps
    the plan that already exists.
    """
    gate = BlockingGate(Phase.REPLANNING.value)
    deadline = started_deadline()
    planner: BasePlanner = RuleBasedPlanner(phase=Phase.REPLANNING.value)
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate), pytest.raises(DeadlineExceeded) as raised:
            await planner.plan(PlanRequest(run_id="r", objective="revise"))
    finally:
        gate.release()

    assert raised.value.phase == Phase.REPLANNING.value
    assert gate.async_entries == [Phase.REPLANNING.value]
    assert deadline.observed_phase == Phase.REPLANNING.value
    assert time.monotonic() - started < 1.0


async def test_replanning_phase_acknowledges_cancellation() -> None:
    """A blocked revision is cancelled promptly and abandons no tasks."""
    gate = BlockingGate(Phase.REPLANNING.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    planner: BasePlanner = RuleBasedPlanner(phase=Phase.REPLANNING.value)
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            revision = asyncio.ensure_future(
                planner.plan(PlanRequest(run_id="r", objective="revise"))
            )
            await wait_until(gate.entered)
            cancellation.set()
            with pytest.raises(RunCancelledError):
                await asyncio.wait_for(revision, timeout=5.0)
    finally:
        gate.release()

    assert time.monotonic() - started < 5.0


# ------------------------------------------------------------------- worker


async def test_worker_phase_is_bounded_before_the_runner_starts(tmp_path: Path) -> None:
    """A blocked attempt is abandoned as a timeout, not retried."""
    gate = BlockingGate(Phase.WORKER.value)
    deadline = started_deadline()
    scheduler, runner, db = make_scheduler(tmp_path, gate=gate, deadline=deadline)
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate):
            summary = await scheduler.run()
    finally:
        gate.release()
        db.close()

    outcome = scheduler.outcomes["t1"]
    assert runner.calls == 0, "the runner was started after the block"
    assert outcome.status.value == "cancelled"
    assert outcome.error_class == "timeout"
    assert gate.async_entries == [Phase.WORKER.value]
    assert deadline.observed_phase == Phase.WORKER.value
    assert summary.status.value == "cancelled"
    assert time.monotonic() - started < 1.0


async def test_worker_phase_acknowledges_cancellation(tmp_path: Path) -> None:
    """A blocked attempt is cancelled and records the cancellation."""
    gate = BlockingGate(Phase.WORKER.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    scheduler, runner, db = make_scheduler(
        tmp_path, gate=gate, deadline=deadline, cancellation=cancellation
    )
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            run = asyncio.ensure_future(scheduler.run())
            await wait_until(gate.entered)
            cancellation.set()
            await asyncio.wait_for(run, timeout=5.0)
    finally:
        gate.release()
        db.close()

    outcome = scheduler.outcomes["t1"]
    assert runner.calls == 0
    assert outcome.status.value == "cancelled"
    assert outcome.error_class == "cancelled"
    assert time.monotonic() - started < 5.0


# ------------------------------------------------------------- verification


async def test_verification_phase_raises_deadline_exceeded(tmp_path: Path) -> None:
    """A blocked verification raises before any check is executed."""
    gate = BlockingGate(Phase.VERIFICATION.value)
    deadline = started_deadline()
    config = RunConfig(run_id="run_deadline", repo_path=str(tmp_path))
    gate_check = VerificationGate(config)
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate), pytest.raises(DeadlineExceeded) as raised:
            await gate_check.verify_task(
                make_task(), workspace=tmp_path, has_changes=True
            )
    finally:
        gate.release()

    assert raised.value.phase == Phase.VERIFICATION.value
    assert gate.async_entries == [Phase.VERIFICATION.value]
    assert deadline.observed_phase == Phase.VERIFICATION.value
    assert time.monotonic() - started < 1.0


async def test_verification_phase_acknowledges_cancellation(tmp_path: Path) -> None:
    """A blocked verification check is cancelled promptly."""
    gate = BlockingGate(Phase.VERIFICATION.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    config = RunConfig(run_id="run_deadline", repo_path=str(tmp_path))
    gate_check = VerificationGate(config)
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            check = asyncio.ensure_future(
                gate_check.verify_task(make_task(), workspace=tmp_path, has_changes=True)
            )
            await wait_until(gate.entered)
            cancellation.set()
            with pytest.raises(RunCancelledError):
                await asyncio.wait_for(check, timeout=5.0)
    finally:
        gate.release()

    assert time.monotonic() - started < 5.0


# --------------------------------------------------------------- integration


async def test_integration_phase_raises_deadline_exceeded(
    tmp_path: Path, repo: Path
) -> None:
    """A blocked integration raises instead of judging an unverified tree."""
    gate = BlockingGate(Phase.INTEGRATION.value)
    deadline = started_deadline()
    config = RunConfig(run_id="run_deadline", repo_path=str(repo))
    worktrees = WorktreeManager(config.repo_path, tmp_path / "wt")
    engine = IntegrationEngine(config=config, worktrees=worktrees)
    base = worktrees.head_revision()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate), pytest.raises(DeadlineExceeded) as raised:
            await engine.integrate([], base_revision=base, run_id="run_deadline")
    finally:
        gate.release()

    assert raised.value.phase == Phase.INTEGRATION.value
    assert gate.async_entries == [Phase.INTEGRATION.value]
    assert deadline.observed_phase == Phase.INTEGRATION.value
    assert time.monotonic() - started < 1.0


async def test_integration_phase_acknowledges_cancellation(
    tmp_path: Path, repo: Path
) -> None:
    """A blocked integration is cancelled promptly."""
    gate = BlockingGate(Phase.INTEGRATION.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    config = RunConfig(run_id="run_deadline", repo_path=str(repo))
    worktrees = WorktreeManager(config.repo_path, tmp_path / "wt")
    engine = IntegrationEngine(config=config, worktrees=worktrees)
    base = worktrees.head_revision()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            integration = asyncio.ensure_future(
                engine.integrate([], base_revision=base, run_id="run_deadline")
            )
            await wait_until(gate.entered)
            cancellation.set()
            await asyncio.wait_for(integration, timeout=5.0)
    finally:
        gate.release()

    assert time.monotonic() - started < 5.0


# ----------------------------------------------------------------- teardown


def test_teardown_gate_is_bounded_by_the_remaining_budget() -> None:
    """The synchronous teardown seam cannot outlive the remaining budget."""
    gate = BlockingGate(Phase.TEARDOWN.value)
    deadline = started_deadline()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate):
            current_run_scope().block_sync(Phase.TEARDOWN.value, deadline=deadline)
    finally:
        gate.release()

    assert gate.entered.is_set()
    assert gate.sync_entries == [Phase.TEARDOWN.value]
    assert time.monotonic() - started < 1.0


def test_teardown_gate_acknowledges_cancellation() -> None:
    """A blocked teardown returns promptly when the run is cancelled."""
    gate = BlockingGate(Phase.TEARDOWN.value)
    deadline = started_deadline(total_s=5.0, reserve_s=0.0)
    cancellation = asyncio.Event()
    cancellation.set()
    started = time.monotonic()
    try:
        with run_scope(deadline=deadline, gate=gate, cancellation=cancellation):
            current_run_scope().block_sync(Phase.TEARDOWN.value, deadline=deadline)
    finally:
        gate.release()

    assert time.monotonic() - started < 5.0


def test_teardown_window_refuses_to_start_when_the_budget_is_gone() -> None:
    """Teardown is bounded too: it may spend the reserve, but no more."""
    deadline = expired_deadline()
    gate = BlockingGate(Phase.TEARDOWN.value)
    try:
        with (
            run_scope(deadline=deadline, gate=gate),
            pytest.raises(DeadlineExceeded) as raised,
            teardown_window(deadline),
        ):
            pytest.fail("teardown ran after the whole budget was gone")
    finally:
        gate.release()

    assert raised.value.phase == Phase.TEARDOWN.value
    assert gate.sync_entries == []


# ------------------------------------------------------- admission and ack


@pytest.mark.parametrize(
    "phase",
    [phase.value for phase in Phase if phase is not Phase.TEARDOWN],
)
async def test_no_async_phase_starts_after_expiry(phase: str) -> None:
    """Every asynchronous phase refuses to start once the budget is gone."""
    gate = BlockingGate(phase)
    deadline = expired_deadline()
    called = False

    async def work() -> None:
        nonlocal called
        called = True

    with run_scope(deadline=deadline, gate=gate), pytest.raises(DeadlineExceeded) as raised:
        await run_phase(work, phase=phase, deadline=deadline)

    assert raised.value.phase == phase
    assert called is False
    assert gate.async_entries == []


async def test_uncooperative_work_cannot_extend_the_acknowledgement_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase that swallows CancelledError cannot make the run wait forever."""
    monkeypatch.setattr(deadline_module, "CANCELLATION_ACK_S", 0.2)
    deadline = started_deadline()

    async def stubborn() -> str:
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            await asyncio.sleep(0.4)
            return "late"
        return "never"

    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        await run_phase(stubborn, phase=Phase.WORKER.value, deadline=deadline)
    elapsed = time.monotonic() - started
    # The abandoned task's eventual result is consumed by the done callback
    # inside ``run_phase``; sleeping here would make the test's own task
    # cancellable during teardown, which is a race in the test, not in the
    # runtime.
    assert elapsed < 1.5, "the acknowledgement window did not bound the wait"


def test_run_scope_is_the_documented_seam() -> None:
    """A scope without a gate is a no-op seam, not a missing deadline."""
    deadline = started_deadline()
    with run_scope(deadline=deadline) as scope:
        assert isinstance(scope, RunScope)
        assert current_run_scope().deadline is deadline
        assert scope.gate is None
        scope.enter_sync(Phase.TEARDOWN.value)
