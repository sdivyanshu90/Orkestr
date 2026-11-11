"""Evaluation statistics and protected-material tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.benchmark.statistics import (
    RunOutcomeRecord,
    StrategyStatistics,
    compare,
    percentile,
)
from agent_squad.observe.fingerprint import capture
from agent_squad.verification.protected import (
    ProtectedMaterial,
    protected_scope_patterns,
)


def make_runs(
    count: int = 10,
    *,
    strategy: str = "single",
    verified: bool = True,
    repository: str = "repo-a",
) -> list[RunOutcomeRecord]:
    return [
        RunOutcomeRecord(
            run_id=f"{strategy}-{i}",
            scenario_id=f"task-{i}",
            task=f"task-{i}",
            strategy=strategy,
            model="m",
            repository=repository,
            completed=True,
            verified=verified,
            wall_clock_s=float(i + 1),
            tokens=1000 + i,
            extra={"repository": repository},
        )
        for i in range(count)
    ]


def test_the_denominator_is_complete() -> None:
    stats = StrategyStatistics(strategy="single")
    stats.runs = make_runs(8)
    stats.runs.append(
        RunOutcomeRecord(
            run_id="c",
            scenario_id="t",
            strategy="single",
            model="m",
            completed=False,
            censored=True,
        )
    )
    stats.runs.append(
        RunOutcomeRecord(
            run_id="d",
            scenario_id="t",
            strategy="single",
            model="m",
            completed=False,
            deadline_exhausted=True,
        )
    )
    assert stats.total == 10
    assert stats.censored == 1
    assert stats.deadline_exhausted == 1
    assert stats.completed == 8
    assert stats.verified == 8


def test_a_censored_run_is_not_silently_dropped() -> None:
    """Both treatments are published, so the reader can see the sensitivity."""
    stats = StrategyStatistics(strategy="single")
    stats.runs = make_runs(4, verified=True)
    stats.runs.append(
        RunOutcomeRecord(
            run_id="c",
            scenario_id="t",
            strategy="single",
            model="m",
            completed=False,
            censored=True,
        )
    )
    lower, upper = stats.sensitivity_bounds()
    assert lower is not None and upper is not None
    assert lower < upper, "the two treatments must differ when a run was censored"
    assert stats.acceptance_rate() == upper


def test_a_deadline_exhausted_run_counts_as_a_failed_attempt() -> None:
    stats = StrategyStatistics(strategy="single")
    stats.runs = make_runs(4, verified=True)
    stats.runs.append(
        RunOutcomeRecord(
            run_id="d",
            scenario_id="t",
            strategy="single",
            model="m",
            completed=False,
            deadline_exhausted=True,
        )
    )
    assert stats.deadline_exhausted == 1
    # It is not a success, and it is not counted as completed either.
    assert stats.verified == 4
    assert stats.acceptance_rate() == pytest.approx(4 / 5)


def test_percentiles_are_reported() -> None:
    stats = StrategyStatistics(strategy="single", runs=make_runs(20))
    percentiles = stats.wall_clock_percentiles()
    assert "p50" in percentiles and "p95" in percentiles
    assert percentiles["p50"] < percentiles["p95"]
    assert stats.mean_wall_clock() > 0


def test_the_percentile_helper_handles_edges() -> None:
    assert percentile([], 50) == 0.0
    assert percentile([5.0], 95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)


def test_the_clustered_interval_is_wider_than_the_naive_one() -> None:
    """Clustering must not claim more precision than the design supports."""
    stats = StrategyStatistics(strategy="single")
    stats.runs = []
    for repo in ("a", "b", "c"):
        for i in range(4):
            stats.runs.append(
                RunOutcomeRecord(
                    run_id=f"{repo}-{i}",
                    scenario_id=f"t{i}",
                    task=f"t{i}",
                    strategy="single",
                    model="m",
                    repository=repo,
                    completed=True,
                    verified=True,
                    wall_clock_s=1.0 + (0.1 if repo == "a" else 0.5),
                    extra={"repository": repo},
                )
            )
    low, high = stats.clustered_interval()
    assert low is not None and high is not None
    assert high - low > 0


def test_a_paired_comparison_reports_flips() -> None:
    baseline = StrategyStatistics(strategy="single", runs=make_runs(6))
    candidate_runs = make_runs(6, strategy="static_two")
    candidate_runs[0].verified = False  # one regression
    candidate = StrategyStatistics(strategy="static_two", runs=candidate_runs)
    result = compare(baseline, candidate)
    assert result["paired_samples"] == 6
    assert result["verification_flips"] == 1
    assert result["baseline"]["runs"] == 6


def test_the_environment_fingerprint_is_stable_and_sensitive() -> None:
    a = capture(provider="mock", model="m", config={"x": 1})
    b = capture(provider="mock", model="m", config={"x": 1})
    c = capture(provider="mock", model="m", config={"x": 2})
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint
    assert a.python_version
    assert a.dependency_hash


def test_the_fingerprint_records_the_provider_and_model() -> None:
    fingerprint = capture(provider="openai_compat", model="gpt-4o-mini")
    assert fingerprint.provider == "openai_compat"
    assert fingerprint.model == "gpt-4o-mini"
    assert "fingerprint" in fingerprint.to_dict()


# ------------------------------------------------------- protected material


def test_an_unchanged_protected_file_passes(tmp_path: Path) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    (base / "tests").mkdir()
    (candidate / "tests").mkdir()
    (base / "tests" / "hidden.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (candidate / "tests" / "hidden.py").write_text("def test_x(): pass\n", encoding="utf-8")

    material = ProtectedMaterial(paths=("tests/hidden.py",)).capture(base)
    verdicts = material.check(candidate)
    assert len(verdicts) == 1
    assert verdicts[0].state.value == "established"


def test_a_modified_protected_file_is_caught(tmp_path: Path) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    (base / "hidden.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    # The worker weakened the hidden test.
    (candidate / "hidden.py").write_text("def test_x(): pass\n", encoding="utf-8")

    material = ProtectedMaterial(paths=("hidden.py",)).capture(base)
    verdicts = material.check(candidate)
    assert verdicts[0].state.value == "not_satisfied"
    assert "modified by the run" in verdicts[0].detail


def test_a_deleted_protected_file_is_caught(tmp_path: Path) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    (base / "hidden.py").write_text("x = 1\n", encoding="utf-8")
    # The worker deleted it.
    material = ProtectedMaterial(paths=("hidden.py",)).capture(base)
    verdicts = material.check(candidate)
    assert verdicts[0].state.value == "not_satisfied"


def test_protected_paths_are_excluded_from_the_write_scope() -> None:
    patterns = protected_scope_patterns(("tests/hidden.py", "conftest.py"))
    assert "tests/hidden.py" in patterns
    assert "conftest.py" in patterns
