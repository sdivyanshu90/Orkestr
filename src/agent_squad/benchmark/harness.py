"""The orchestration efficiency laboratory.

The harness runs the *same* work under several execution strategies and
records what happened.  Its job is to produce evidence for the research
question, not to demonstrate that the multi-agent system is better.

Methodology, stated plainly:

* Every scenario is executed once per strategy per trial, on a freshly
  created repository, so no strategy benefits from work another strategy did.
* The provider is the deterministic mock unless the scenario installs a
  fault.  That makes the comparison about *orchestration*, not about model
  luck, and it makes the results exactly reproducible.
* Metrics come from the run's own event log, not from the harness's
  assumptions about what happened.
* Wall-clock numbers on a local machine are noisy.  The harness reports
  mean and standard deviation across trials and refuses to draw conclusions
  from a single trial.

A negative result -- a multi-agent strategy that is slower or more expensive
than the single-agent baseline -- is a legitimate and useful outcome of this
harness, and the report format is designed to make it visible.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import RunConfig
from ..errors import AgentSquadError, ValidationError
from ..ids import run_id as new_run_id
from ..models.run import RunStatus
from ..observe.metrics import Comparison, RunMetrics, StrategyAggregate
from ..orchestrator import Orchestrator
from ..store.events import EventLog, EventType
from .tasks import Scenario, build_scenarios

STRATEGIES: dict[str, int] = {
    "single": 1,
    "static_two": 2,
    "static_four": 4,
    "adaptive": 0,  # decided by the selector
}


@dataclass
class TrialResult:
    """One execution of one scenario under one strategy."""

    scenario_id: str
    strategy: str
    workers: int
    trial: int
    metrics: RunMetrics
    run_id: str
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "strategy": self.strategy,
            "workers": self.workers,
            "trial": self.trial,
            "run_id": self.run_id,
            "error": self.error,
            "warnings": list(self.warnings),
            "metrics": self.metrics.to_dict(),
        }


@dataclass
class SuiteResult:
    """The complete outcome of a benchmark suite."""

    suite: str
    trials: list[TrialResult] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    strategies: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def aggregates(self) -> dict[str, StrategyAggregate]:
        out: dict[str, StrategyAggregate] = {}
        for trial in self.trials:
            aggregate = out.setdefault(
                trial.strategy, StrategyAggregate(strategy=trial.strategy, workers=trial.workers)
            )
            aggregate.trials.append(trial.metrics)
        return out

    def comparisons(self) -> list[Comparison]:
        """Compare every strategy against the single-agent baseline."""
        aggregates = self.aggregates()
        baseline = aggregates.get("single")
        if baseline is None:
            return []
        comparisons: list[Comparison] = []
        for name, aggregate in aggregates.items():
            if name == "single":
                continue
            base_mean = baseline.mean
            cand_mean = aggregate.mean
            for metric, better_is_lower in (
                ("verified_completion_rate", False),
                ("wall_clock_s", True),
                ("tokens_used", True),
                ("cost_usd", True),
                ("tool_calls", True),
                ("integration_conflicts", True),
            ):
                if metric not in base_mean or metric not in cand_mean:
                    continue
                base_value = float(base_mean[metric])
                cand_value = float(cand_mean[metric])
                if abs(cand_value - base_value) < 1e-12:
                    improved = False
                    note = "no measurable difference"
                elif better_is_lower:
                    improved = cand_value < base_value
                else:
                    improved = cand_value > base_value
                if metric == "verified_completion_rate" and not improved and not note:
                    note = (
                        "multi-agent execution did not improve the verified "
                        "completion rate"
                    )
                if metric == "wall_clock_s" and not improved and not note:
                    note = "multi-agent execution was not faster"
                comparisons.append(
                    Comparison(
                        baseline="single",
                        candidate=name,
                        metric=metric,
                        baseline_value=base_value,
                        candidate_value=cand_value,
                        improved=improved,
                        note=note,
                    )
                )
        return comparisons

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "scenarios": list(self.scenarios),
            "strategies": list(self.strategies),
            "trial_count": len(self.trials),
            "duration_s": round((self.finished_at or time.time()) - self.started_at, 3),
            "trials": [t.to_dict() for t in self.trials],
            "aggregates": {
                name: aggregate.to_dict() for name, aggregate in self.aggregates().items()
            },
            "comparisons": [c.to_dict() for c in self.comparisons()],
            "notes": list(self.notes),
        }


def _metrics_from_events(
    *,
    run_id: str,
    log: EventLog,
    strategy: str,
    workers: int,
    trial: int,
    wall_clock_s: float,
    integration_conflicts: int,
    patches_applied: int,
    integration_passed: bool,
    final_status: str,
) -> RunMetrics:
    """Derive metrics from the run's own event log."""
    events = log.read(run_id)
    metrics = RunMetrics(
        run_id=run_id,
        strategy=strategy,
        workers=workers,
        trials=trial,
        wall_clock_s=wall_clock_s,
        integration_conflicts=integration_conflicts,
        patches_applied=patches_applied,
        integration_passed=integration_passed,
    )
    reported_tokens = 0
    estimated_tokens = 0
    for event in events:
        if event.type is EventType.MODEL_CALL:
            metrics.model_calls += 1
            tokens = int(event.payload.get("input_tokens") or 0) + int(
                event.payload.get("output_tokens") or 0
            )
            metrics.tokens_used += tokens
            if event.payload.get("estimated"):
                estimated_tokens += tokens
            else:
                reported_tokens += tokens
        elif event.type is EventType.TOOL_INVOKED:
            metrics.tool_calls += 1
        elif event.type is EventType.MESSAGE_SENT:
            metrics.messages += 1
        elif event.type is EventType.VERIFICATION_FAILED:
            metrics.verification_failures += 1
        elif event.type is EventType.TASK_ATTEMPT_STARTED:
            if int(event.attempt or 0) > 1:
                metrics.retries += 1
        elif event.type is EventType.TOOL_DENIED or event.type is EventType.POLICY_DENIED:
            metrics.policy_denials += 1
        elif event.type is EventType.HUMAN_APPROVAL:
            if event.payload.get("status") in ("requested", "approved", "denied"):
                metrics.human_interventions += 1
    total_tokens = reported_tokens + estimated_tokens
    metrics.reported_usage_ratio = (reported_tokens / total_tokens) if total_tokens else 1.0
    # Coordination overhead is approximated by the tokens carried by
    # inter-agent messages: every message is context the agents had to share.
    metrics.coordination_tokens = int(metrics.messages * 120)

    states = log.read(run_id)
    succeeded = sum(
        1
        for e in states
        if e.type is EventType.TASK_STATUS_CHANGED and e.payload.get("to") == "succeeded"
    )
    metrics.succeeded = succeeded
    metrics.total_tasks = len(
        {e.task_id for e in states if e.type is EventType.TASK_CREATED and e.task_id}
    )
    metrics.verified_tasks = succeeded
    if final_status == RunStatus.COMPLETED_WITH_FAILURES.value:
        metrics.failed = max(0, metrics.total_tasks - succeeded)
    return metrics


async def run_scenario_strategy(
    scenario: Scenario,
    strategy: str,
    trial: int,
    *,
    workdir: Path,
    max_workers: int | None = None,
) -> TrialResult:
    """Run one scenario under one strategy and collect its metrics."""
    from ..providers.mock import MockProvider

    root = workdir / f"{scenario.id}-{strategy}-{trial}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    repo = scenario.build_repo(root / "repo")

    provider = MockProvider(model="mock-deterministic-v1")
    if scenario.provider_factory is not None:
        provider = scenario.provider_factory(provider)

    run_id = new_run_id()
    worker_limit = max_workers or STRATEGIES.get(strategy) or 4
    config = RunConfig(
        run_id=run_id,
        repo_path=str(repo),
        data_dir=str(root / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        scheduler={"max_workers": worker_limit},
        strategy_override=strategy if strategy != "adaptive" else None,
        max_workers_override=max_workers if strategy == "adaptive" else None,
        verification={"integration_test_command": ("python3", "-m", "pytest", "-q")},
        retry={"max_attempts": 3, "initial_backoff_s": 0.05, "max_backoff_s": 0.5},
    )

    orchestrator = Orchestrator(config, data_dir=root / "data", provider=provider)
    started = time.monotonic()
    error: str | None = None
    conflicts = 0
    applied = 0
    integration_passed = False
    final_status = "error"
    workers = STRATEGIES.get(strategy, 1)
    try:
        outcome = await orchestrator.run(
            scenario.spec.get("objective", scenario.name),
            description=scenario.description,
            request_spec=scenario.spec,
            integrate=True,
        )
        wall_clock = time.monotonic() - started
        workers = outcome.decision.worker_count if outcome.decision else 1
        if outcome.integration is not None:
            conflicts = outcome.integration.conflict_count
            applied = len(outcome.integration.applied)
            integration_passed = outcome.integration.passed
        final_status = outcome.status.value
    except AgentSquadError as exc:
        wall_clock = time.monotonic() - started
        error = exc.message
        final_status = "error"

    metrics = _metrics_from_events(
        run_id=run_id,
        log=EventLog(orchestrator.db),
        strategy=strategy,
        workers=workers,
        trial=trial,
        wall_clock_s=wall_clock,
        integration_conflicts=conflicts,
        patches_applied=applied,
        integration_passed=integration_passed,
        final_status=final_status,
    )
    await orchestrator.aclose()
    return TrialResult(
        scenario_id=scenario.id,
        strategy=strategy,
        workers=workers,
        trial=trial,
        metrics=metrics,
        run_id=run_id,
        error=error,
    )


async def run_suite(
    *,
    suite: str = "demo",
    trials: int = 1,
    data_dir: str | Path | None = None,
    strategies: list[str] | None = None,
    scenario_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run the benchmark suite and return a serialisable report."""
    scenarios = build_scenarios()
    if scenario_ids:
        wanted = set(scenario_ids)
        scenarios = [s for s in scenarios if s.id in wanted]
    selected = strategies or list(STRATEGIES)
    unknown = [s for s in selected if s not in STRATEGIES]
    if unknown:
        raise ValidationError(f"unknown strategies: {unknown}", strategies=unknown)
    result = SuiteResult(
        suite=suite,
        scenarios=[s.id for s in scenarios],
        strategies=selected,
        trials=[],
    )
    result.notes.append(
        "The provider is deterministic, so differences between strategies "
        "reflect orchestration decisions rather than model variance."
    )
    if trials < 2:
        result.notes.append(
            "A single trial per configuration cannot support a variance claim; "
            "run with --trials 3 or more for that."
        )

    with tempfile.TemporaryDirectory(prefix="agent-squad-bench-") as tmp:
        workdir = Path(tmp)
        for scenario in scenarios:
            for strategy in selected:
                for trial in range(trials):
                    outcome = await run_scenario_strategy(
                        scenario, strategy, trial, workdir=workdir
                    )
                    result.trials.append(outcome)

    result.finished_at = time.time()
    return result.to_dict()
