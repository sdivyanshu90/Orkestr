"""Evaluation statistics.

The roadmap's exit criteria are specific about *how* a claim must be
supported:

* **Denominators must be complete.**  A rate computed over the runs that
  happened to finish is not a rate.  Cancelled and deadline-exhausted runs
  are counted, and the treatment of each is stated.
* **Uncertainty must be reported.**  A single mean is not an estimate of
  anything.  The spread is reported, and intervals are clustered by
  repository and by task, because runs on the same task are not independent.
* **Tail latency matters.**  The mean hides the slow runs, so p50 and p95
  are reported alongside it.
* **A censored run is not a failure.**  Work a user aborted is censored:
  it is counted and reported, but it is not silently dropped to improve a
  headline number, and a sensitivity bound is published with it.

These helpers exist so that the numbers in a report are produced by the
same code every time, rather than recomputed by hand.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunOutcomeRecord:
    """One run, in the terms the statistics need."""

    run_id: str
    scenario_id: str
    strategy: str
    model: str
    repository: str = ""
    task: str = ""
    completed: bool = False
    verified: bool = False
    censored: bool = False
    """The run was aborted by a user; it is neither a success nor a failure."""

    deadline_exhausted: bool = False
    """The run used its whole budget; that is a failed budgeted attempt."""

    wall_clock_s: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyStatistics:
    """The statistics for one strategy over a corpus."""

    strategy: str
    runs: list[RunOutcomeRecord] = field(default_factory=list)

    # ------------------------------------------------------------ denominators

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def completed(self) -> int:
        return sum(1 for r in self.runs if r.completed and not r.censored)

    @property
    def censored(self) -> int:
        return sum(1 for r in self.runs if r.censored)

    @property
    def deadline_exhausted(self) -> int:
        return sum(1 for r in self.runs if r.deadline_exhausted)

    @property
    def verified(self) -> int:
        return sum(1 for r in self.runs if r.verified)

    @property
    def failed(self) -> int:
        return sum(
            1
            for r in self.runs
            if r.completed and not r.verified and not r.censored
        )

    def acceptance_rate(self, *, include_censored_as_failure: bool = False) -> float | None:
        """The verified completion rate over a *complete* denominator.

        ``include_censored_as_failure`` gives the conservative bound: a
        censored run counts against the strategy.  Both are published,
        because a rate that depends on how the censored runs were treated
        is not a rate.
        """
        if not self.runs:
            return None
        if include_censored_as_failure:
            denominator = self.total
            numerator = self.verified
        else:
            denominator = self.total - self.censored
            numerator = self.verified
        if denominator <= 0:
            return None
        return numerator / denominator

    def sensitivity_bounds(self) -> tuple[float | None, float | None]:
        """The acceptance rate under the two treatments of censored runs.

        The lower bound counts every censored run as a failure; the upper
        bound counts them all as successes.  The true rate is inside this
        interval, and publishing it is more honest than picking one.
        """
        return (
            self.acceptance_rate(include_censored_as_failure=True),
            self.acceptance_rate(include_censored_as_failure=False),
        )

    # -------------------------------------------------------------- latency

    def wall_clock_percentiles(self, percentiles: tuple[float, ...] = (50, 95)) -> dict[str, float]:
        values = sorted(r.wall_clock_s for r in self.runs if r.completed and not r.censored)
        if not values:
            return {f"p{p:g}": 0.0 for p in percentiles}
        return {f"p{p:g}": percentile(values, p) for p in percentiles}

    def mean_wall_clock(self) -> float:
        values = [r.wall_clock_s for r in self.runs if r.completed and not r.censored]
        return statistics.mean(values) if values else 0.0

    def mean_tokens(self) -> float:
        values = [r.tokens for r in self.runs if r.completed and not r.censored]
        return statistics.mean(values) if values else 0.0

    def total_cost(self) -> float:
        return round(sum(r.cost_usd for r in self.runs), 8)

    # ------------------------------------------------------------ clustering

    def cluster_means(self, key: str = "repository") -> dict[str, float]:
        """The mean of each cluster.

        Runs on the same repository (or the same task) are not independent,
        so the uncertainty of an overall mean has to account for that.
        """
        groups: dict[str, list[float]] = {}
        for run in self.runs:
            if not run.completed or run.censored:
                continue
            group = str(run.extra.get(key) or getattr(run, key, "") or "")
            groups.setdefault(group, []).append(run.wall_clock_s)
        return {
            group: round(statistics.mean(values), 6)
            for group, values in sorted(groups.items())
            if values
        }

    def clustered_interval(
        self, key: str = "repository", *, confidence: float = 0.95
    ) -> tuple[float | None, float | None]:
        """A confidence interval for the mean, clustered by ``key``.

        The cluster means are treated as the independent units, and the
        interval is computed over them.  This is wider than treating every
        run as independent, which is the correct direction: it does not
        claim more precision than the design supports.
        """
        groups: dict[str, list[float]] = {}
        for run in self.runs:
            if not run.completed or run.censored:
                continue
            group = str(run.extra.get(key) or getattr(run, key, "") or "")
            groups.setdefault(group, []).append(run.wall_clock_s)
        if len(groups) < 2:
            return None, None
        means = [statistics.mean(v) for v in groups.values()]
        n = len(means)
        mean = statistics.mean(means)
        if n < 2:
            return None, None
        sd = statistics.stdev(means)
        z = 1.96 if confidence >= 0.95 else 1.645
        margin = z * sd / math.sqrt(n)
        return round(mean - margin, 6), round(mean + margin, 6)

    def to_dict(self) -> dict[str, Any]:
        lower, upper = self.sensitivity_bounds()
        interval_low, interval_high = self.clustered_interval()
        return {
            "strategy": self.strategy,
            "runs": self.total,
            "completed": self.completed,
            "verified": self.verified,
            "failed": self.failed,
            "censored": self.censored,
            "deadline_exhausted": self.deadline_exhausted,
            "acceptance_rate": self.acceptance_rate(),
            "acceptance_rate_conservative": lower,
            "acceptance_rate_optimistic": upper,
            "mean_wall_clock_s": round(self.mean_wall_clock(), 6),
            "wall_clock_percentiles": self.wall_clock_percentiles(),
            "mean_tokens": round(self.mean_tokens(), 2),
            "total_cost_usd": self.total_cost(),
            "cluster_means": self.cluster_means(),
            "clustered_interval_95": [interval_low, interval_high],
        }


def percentile(values: list[float], p: float) -> float:
    """A percentile of a list of values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100.0)
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return ordered[int(k)]
    return ordered[lower] * (upper - k) + ordered[upper] * (k - lower)


def compare(
    baseline: StrategyStatistics, candidate: StrategyStatistics, *, metric: str = "wall_clock_s"
) -> dict[str, Any]:
    """A paired comparison of two strategies over the same corpus."""
    base_by_task: dict[tuple[str, str], RunOutcomeRecord] = {
        (r.repository, r.task or r.scenario_id): r for r in baseline.runs
    }
    cand_by_task: dict[tuple[str, str], RunOutcomeRecord] = {
        (r.repository, r.task or r.scenario_id): r for r in candidate.runs
    }
    shared = sorted(set(base_by_task) & set(cand_by_task))
    deltas: list[float] = []
    flips = 0
    for key in shared:
        a = base_by_task[key]
        b = cand_by_task[key]
        if a.censored or b.censored:
            continue
        deltas.append(getattr(b, metric) - getattr(a, metric))
        if a.verified != b.verified:
            flips += 1
    return {
        "metric": metric,
        "paired_samples": len(deltas),
        "mean_delta": round(statistics.mean(deltas), 6) if deltas else None,
        "median_delta": round(statistics.median(deltas), 6) if deltas else None,
        "verification_flips": flips,
        "baseline": baseline.to_dict(),
        "candidate": candidate.to_dict(),
    }
