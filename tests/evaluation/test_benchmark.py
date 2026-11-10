"""Evaluation tests.

These check that the benchmark harness produces *reproducible* results, that
its metrics are derived from the run's own record rather than from the
harness's assumptions, and that a negative result is representable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.benchmark.harness import STRATEGIES, run_suite
from agent_squad.benchmark.tasks import build_scenarios, get_scenario
from agent_squad.observe.metrics import Comparison, RunMetrics, StrategyAggregate


def test_every_scenario_is_well_formed() -> None:
    for scenario in build_scenarios():
        assert scenario.id
        assert scenario.name
        assert scenario.spec.get("tasks"), scenario.id
        for task in scenario.spec["tasks"]:
            assert task["id"]
            assert task["role"] in ("implementer", "test_engineer", "reviewer", "supervisor")
            assert task["acceptance_criteria"], f"{scenario.id}/{task['id']} has no criteria"
            for criterion in task["acceptance_criteria"]:
                assert criterion["kind"]
                assert criterion["spec"] is not None


def test_scenario_dependencies_reference_known_tasks() -> None:
    for scenario in build_scenarios():
        ids = {t["id"] for t in scenario.spec["tasks"]}
        for task in scenario.spec["tasks"]:
            for dependency in task.get("depends_on", []):
                assert dependency in ids, f"{scenario.id}: {dependency} is unknown"


def test_scenario_lookup() -> None:
    assert get_scenario("independent_parallel").id == "independent_parallel"
    with pytest.raises(KeyError):
        get_scenario("does_not_exist")


def test_the_strategy_table_is_complete() -> None:
    assert set(STRATEGIES) == {"single", "static_two", "static_four", "adaptive"}


def test_metrics_derive_the_completion_rate_from_verified_tasks() -> None:
    metrics = RunMetrics(run_id="r", total_tasks=4, verified_tasks=3)
    assert metrics.verified_completion_rate == pytest.approx(0.75)


def test_metrics_report_cost_per_verified_task() -> None:
    metrics = RunMetrics(run_id="r", total_tasks=2, verified_tasks=2, tokens_used=1000, cost_usd=0.5)
    assert metrics.tokens_per_verified_task == pytest.approx(500)
    assert metrics.cost_per_verified_task == pytest.approx(0.25)


def test_metrics_with_no_verified_tasks_report_none_rather_than_infinity() -> None:
    metrics = RunMetrics(run_id="r", total_tasks=2, verified_tasks=0, tokens_used=10)
    assert metrics.to_dict()["tokens_per_verified_task"] is None


def test_the_aggregate_reports_variance_across_trials() -> None:
    aggregate = StrategyAggregate(strategy="single", workers=1)
    aggregate.trials.append(
        RunMetrics(run_id="a", total_tasks=2, verified_tasks=2, wall_clock_s=1.0)
    )
    aggregate.trials.append(
        RunMetrics(run_id="b", total_tasks=2, verified_tasks=1, wall_clock_s=3.0)
    )
    mean = aggregate.mean
    assert mean["verified_completion_rate"] == pytest.approx(0.75)
    assert mean["wall_clock_s"] == pytest.approx(2.0)
    stdev = aggregate.stdev
    assert stdev["wall_clock_s"] == pytest.approx(1.4142135, rel=1e-5)


def test_a_single_trial_reports_no_standard_deviation() -> None:
    aggregate = StrategyAggregate(strategy="single", workers=1)
    aggregate.trials.append(RunMetrics(run_id="a", wall_clock_s=1.0))
    assert aggregate.stdev == {}


def test_a_comparison_can_record_a_negative_result() -> None:
    comparison = Comparison(
        baseline="single",
        candidate="static_four",
        metric="wall_clock_s",
        baseline_value=1.0,
        candidate_value=3.0,
        improved=False,
        note="multi-agent execution was not faster",
    )
    assert comparison.improved is False
    assert "not faster" in comparison.note


@pytest.mark.slow
async def test_the_suite_runs_and_reports_for_every_strategy(tmp_path: Path) -> None:
    report = await run_suite(
        suite="unit",
        trials=1,
        strategies=["single", "static_two"],
        scenario_ids=["independent_parallel", "single_agent_preferred"],
    )
    assert report["trial_count"] == 4
    assert set(report["aggregates"]) == {"single", "static_two"}
    for aggregate in report["aggregates"].values():
        assert aggregate["trials"] == 2
        assert "verified_completion_rate" in aggregate["mean"]
    assert report["comparisons"]
    for trial in report["trials"]:
        assert trial["error"] is None
    assert all(t["metrics"]["total_tasks"] > 0 for t in report["trials"])


@pytest.mark.slow
async def test_the_suite_is_reproducible(tmp_path: Path) -> None:
    """Two runs of the same scenario under the same strategy must agree."""
    first = await run_suite(
        suite="repro",
        trials=1,
        strategies=["single"],
        scenario_ids=["independent_parallel"],
    )
    second = await run_suite(
        suite="repro",
        trials=1,
        strategies=["single"],
        scenario_ids=["independent_parallel"],
    )
    a = first["aggregates"]["single"]["mean"]
    b = second["aggregates"]["single"]["mean"]
    # The deterministic provider means the token and tool-call counts must
    # be identical between runs.
    assert a["tokens_used"] == b["tokens_used"]
    assert a["tool_calls"] == b["tool_calls"]
    assert a["verified_completion_rate"] == b["verified_completion_rate"]


@pytest.mark.slow
async def test_a_failing_worker_is_retried_and_the_work_still_completes() -> None:
    report = await run_suite(
        suite="failure",
        trials=1,
        strategies=["single"],
        scenario_ids=["failing_worker"],
    )
    trial = report["trials"][0]
    assert trial["error"] is None
    assert trial["metrics"]["retries"] >= 1
    assert trial["metrics"]["verified_completion_rate"] == pytest.approx(1.0)


@pytest.mark.slow
async def test_conflicting_edits_are_detected_at_the_integration_boundary() -> None:
    report = await run_suite(
        suite="conflict",
        trials=1,
        strategies=["static_two"],
        scenario_ids=["integration_conflict"],
    )
    trial = report["trials"][0]
    # Both workers succeed in isolation, but the combination conflicts.
    assert trial["metrics"]["integration_conflicts"] >= 1
    assert trial["metrics"]["integration_passed"] is False
