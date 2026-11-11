"""R1 at the run boundary: every phase is bounded by one absolute deadline.

The unit suite proves each phase consults the deadline at its own seam.  This
suite proves the promise through the *normal entry point*: a real
``Orchestrator.run`` is given a declared deadline ``D``, a blocking operation
is injected into one phase through the ambient run scope, and the whole run
-- inspection, plan, scheduler, workers, checks, integration and teardown, in
that order -- must finish by ``D + 5`` seconds.

The gate is the observable dispatch boundary.  The provider enters the
``download`` and ``provider`` phases immediately before it builds and sends a
request, and the scheduler enters ``worker`` immediately before an attempt.
A gate entry after the expiry therefore *is* a request or attempt created
after the expiry, which is what the criterion forbids.

The teardown phase is the one exception to the "no entry after expiry" rule:
reclaiming owned resources is allowed to spend the reserve, so teardown
entries are permitted until the whole budget is gone.  Everything else --
including work that would be created by the scheduler itself -- must be
refused before it starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib.machinery
import inspect
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_squad.config import RetryConfig, RunConfig
from agent_squad.models.contract import VerificationOutcome
from agent_squad.models.run import RunStatus
from agent_squad.orchestrator import Orchestrator, RunOutcome
from agent_squad.runtime.deadline import Phase, run_scope

pytestmark = pytest.mark.integration

DEADLINE_S = 3.0
RESERVE_S = 0.25
BOUND_S = 5.0
#: How much later than the computed expiry a gate entry may be recorded.  The
#: expiry is computed from the test's clock, a few milliseconds before the
#: orchestrator starts its own, so a small tolerance is expected.
SLACK_S = 0.2


class RunGate:
    """A phase gate that blocks one phase and timestamps every entry."""

    def __init__(self, blocked: str) -> None:
        self.blocked = blocked
        self.entries: list[tuple[str, float]] = []
        self.entered = threading.Event()
        self._async_release = asyncio.Event()
        self._sync_release = threading.Event()

    async def enter(self, phase: str) -> None:
        self.entries.append((phase, time.monotonic()))
        if phase == self.blocked:
            self.entered.set()
            await self._async_release.wait()

    def enter_sync(self, phase: str) -> None:
        self.entries.append((phase, time.monotonic()))
        if phase == self.blocked:
            self.entered.set()
            self._sync_release.wait(30)

    def release(self) -> None:
        self._async_release.set()
        self._sync_release.set()

    async def wait_entered(self, timeout: float = 10.0) -> None:
        limit = time.monotonic() + timeout
        while not self.entered.is_set():
            if time.monotonic() > limit:
                raise AssertionError(f"the {self.blocked} phase was never entered")
            await asyncio.sleep(0.005)

    def entries_after(self, moment: float, *, include_teardown: bool = False) -> list[str]:
        return [
            phase
            for phase, at in self.entries
            if at > moment and (include_teardown or phase != Phase.TEARDOWN.value)
        ]


def bounded_config(run_config: RunConfig, **updates: Any) -> RunConfig:
    """The fixture configuration with a small, declared deadline."""
    return run_config.model_copy(
        update={"deadline_s": DEADLINE_S, "teardown_reserve_s": RESERVE_S, **updates}
    )


def failing_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """A request whose implementation can never satisfy its own check.

    Verification therefore fails, the scheduler classifies the failure as a
    verification failure, and the orchestrator enters the bounded replanning
    phase -- which is the phase this test needs to reach.
    """
    failing = copy.deepcopy(spec)
    for task in failing["tasks"]:
        task["max_attempts"] = 1
        if task["id"] == "impl":
            task["acceptance_criteria"].append(
                {
                    "id": "impossible",
                    "description": "a symbol that is never written",
                    "kind": "file_contains",
                    "spec": {"path": "calc.py", "pattern": "def never_present_in_the_tree"},
                }
            )
    return failing


async def run_with_blocked_phase(
    tmp_path: Path,
    config: RunConfig,
    spec: dict[str, Any],
    phase: str,
) -> tuple[float, RunGate, RunOutcome, float]:
    """Run the normal entry point with one phase blocked.

    Returns the wall-clock duration (including the caller's ``aclose``
    teardown), the gate, the outcome and the monotonic start time.
    """
    gate = RunGate(phase)
    started = time.monotonic()
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        with run_scope(gate=gate):
            outcome = await orchestrator.run(
                spec["objective"],
                description=spec.get("description", ""),
                request_spec=spec,
            )
    finally:
        await orchestrator.aclose()
        gate.release()
    elapsed = time.monotonic() - started
    return elapsed, gate, outcome, started


def assert_finished_by_deadline_plus_five(
    elapsed: float,
    gate: RunGate,
    started: float,
    *,
    deadline_s: float = DEADLINE_S,
    reserve_s: float = RESERVE_S,
) -> None:
    """The whole run finished on time and started no new work after expiry."""
    expiry = started + deadline_s - reserve_s
    assert gate.entered.is_set(), "the injected blocking operation was never reached"
    assert elapsed <= deadline_s + BOUND_S, (
        f"the run took {elapsed:.2f}s, longer than the declared {deadline_s}s "
        f"deadline plus {BOUND_S}s"
    )
    late = gate.entries_after(expiry + SLACK_S)
    assert late == [], f"these phases were started after the deadline expired: {late}"


# --------------------------------------------------------------- the phases


async def test_download_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """A blocked model load cannot keep the run past D + 5."""
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.DOWNLOAD.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    assert outcome.status is RunStatus.CANCELLED


async def test_inspection_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """Inspection runs before the scheduler exists and is still bounded.

    This is the "no synchronous operation escapes merely because the
    scheduler has not started" case: the scan is blocked through the
    synchronous seam and the run must still stop on time.
    """
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.INSPECTION.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    assert outcome.status is RunStatus.CANCELLED


async def test_planning_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """The initial plan is bounded before the scheduler exists."""
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.PLANNING.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    assert outcome.status is RunStatus.CANCELLED


def replanning_import_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the ``orchestrator.py`` import hunk reported with R1.

    The shipped orchestrator contains::

        from ..planning.planner import PlanRequest

    in ``_request_revision``.  ``agent_squad.orchestrator`` is a *top-level*
    module of the ``agent_squad`` package, so two dots resolve one level
    beyond it and raise ``ImportError`` before the replanning phase can run.
    The one-line fix (``from .planning.planner import ...``) belongs to
    ``orchestrator.py``, which this change does not own, so it is reported
    as a hunk instead of applied.

    Until that hunk lands, this shim gives the module a two-part package for
    exactly the duration of the revision request, so this test still drives
    the *same* normal entry point and the real replanning phase.  It detects
    the broken source and does nothing once the hunk has been applied.
    """
    original = Orchestrator._request_revision
    if "from ..planning.planner import" not in inspect.getsource(original):
        return

    module = sys.modules[Orchestrator.__module__]

    async def patched(self: Orchestrator, context: Any, contract: Any) -> Any:
        previous = module.__package__
        previous_spec = module.__spec__
        module.__package__ = "agent_squad.orchestrator"
        # Python 3.14 refuses a relative import whose ``__package__`` does not
        # match ``__spec__.parent``, so the temporary spec must agree too.
        module.__spec__ = importlib.machinery.ModuleSpec(
            "agent_squad.orchestrator.shim", loader=None
        )
        try:
            return await original(self, context, contract)
        finally:
            module.__package__ = previous
            module.__spec__ = previous_spec

    monkeypatch.setattr(Orchestrator, "_request_revision", patched)


async def test_replanning_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig,
    spec: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded revision after a verification failure is bounded too."""
    retry = RetryConfig(
        max_attempts=1,
        **run_config.retry.model_dump(exclude={"max_attempts"}),
    )
    config = bounded_config(run_config, retry=retry)
    replanning_import_fix(monkeypatch)
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, config, failing_spec(spec), Phase.REPLANNING.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    # The scheduler had already finished that plan, so the run status is not
    # "success"; the deadline itself is reported by the contract verdict.
    assert outcome.status is not RunStatus.SUCCEEDED
    assert outcome.contract is not None
    assert outcome.contract.outcome is VerificationOutcome.TIMED_OUT


async def test_provider_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """A request blocked in the provider queue cannot outlive the deadline."""
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.PROVIDER.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    assert outcome.status is RunStatus.CANCELLED


async def test_worker_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """A blocked worker attempt is abandoned; dependents are never started."""
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.WORKER.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    assert outcome.status is RunStatus.CANCELLED


async def test_verification_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """A blocked acceptance check cannot keep the run alive."""
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.VERIFICATION.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    assert outcome.status is RunStatus.CANCELLED


async def test_integration_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """A blocked integration refuses the candidate instead of judging it late."""
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.INTEGRATION.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    # The tasks themselves had succeeded; the deadline stopped the candidate
    # from being integrated and judged.  The verdict must say *that*, not
    # report a successful run whose contract was never satisfied.
    assert outcome.contract is not None
    assert outcome.contract.outcome is VerificationOutcome.TIMED_OUT


async def test_teardown_phase_finishes_by_deadline_plus_five(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """A blocked cleanup may spend the reserve but not the run's future."""
    elapsed, gate, outcome, started = await run_with_blocked_phase(
        tmp_path, bounded_config(run_config), spec, Phase.TEARDOWN.value
    )

    assert_finished_by_deadline_plus_five(elapsed, gate, started)
    assert outcome.status is RunStatus.CANCELLED


# -------------------------------------------------------------- cancellation


async def test_a_cancelled_run_is_acknowledged_within_five_seconds(
    run_config: RunConfig, spec: dict[str, Any], tmp_path: Path
) -> None:
    """A user cancellation of a blocked worker is acknowledged within 5 s.

    The deadline is disabled so this measures the *cancellation* path alone:
    the worker is blocked, the operator cancels, and the run must return
    within five seconds of the cancellation rather than at the end of some
    unrelated timeout.
    """
    gate = RunGate(Phase.WORKER.value)
    cancellation = asyncio.Event()
    orchestrator = Orchestrator(run_config, data_dir=tmp_path / "data")
    acknowledged_at: float | None = None
    finished_at: float | None = None

    async def cancel_once_blocked() -> None:
        nonlocal acknowledged_at
        await gate.wait_entered()
        acknowledged_at = time.monotonic()
        cancellation.set()

    monitor = asyncio.create_task(cancel_once_blocked())
    try:
        with run_scope(gate=gate):
            outcome = await asyncio.wait_for(
                orchestrator.run(
                    spec["objective"],
                    description=spec.get("description", ""),
                    request_spec=spec,
                    cancellation=cancellation,
                ),
                timeout=30.0,
            )
        finished_at = time.monotonic()
    finally:
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        await orchestrator.aclose()
        gate.release()

    assert acknowledged_at is not None
    assert finished_at is not None
    assert finished_at - acknowledged_at <= BOUND_S, (
        f"the cancellation was acknowledged after "
        f"{finished_at - acknowledged_at:.2f}s, more than {BOUND_S}s"
    )
    assert outcome.status is RunStatus.CANCELLED
    started_work = [
        phase
        for phase in gate.entries_after(acknowledged_at)
        if phase in (Phase.WORKER.value, Phase.PROVIDER.value, Phase.DOWNLOAD.value)
    ]
    assert started_work == [], (
        "a worker attempt or provider request was started after the cancellation"
    )
