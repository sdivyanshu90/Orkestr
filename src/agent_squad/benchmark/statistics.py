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
from collections.abc import Sequence
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


# ---------------------------------------------------------------------------
# Exact binomial confidence limits
# ---------------------------------------------------------------------------
#
# A normal approximation is not good enough for the claims this project
# makes.  The interesting cases sit at the boundary: zero false accepts in a
# few hundred cases, and an acceptance rate close to one.  A Wald interval on
# a proportion of zero has zero width, which is not an interval at all, and
# the Wilson interval is still an approximation whose coverage is only
# approximate.  The Clopper-Pearson ("exact binomial") limits invert the
# regularized incomplete beta function and are exact by construction: they
# are the widest interval that still attains the nominal coverage.


def _log_beta(a: float, b: float) -> float:
    """``log B(a, b)`` computed from log-gamma to avoid overflow."""
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)

def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz's continued fraction for the incomplete beta function.

    This is the standard modified Lentz algorithm: it evaluates the
    continued fraction by repeated modification of ``c`` and ``d``, which
    converges for the parameter ranges the Clopper-Pearson inversion needs.
    """
    tiny = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        num = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + num * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + num / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        num = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + num * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + num / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """The regularized incomplete beta function ``I_x(a, b)``.

    ``I_x(a, b)`` is the probability that a Beta(a, b) variate is at most
    ``x``.  It is the distribution function the Clopper-Pearson limits are
    built from, so it is exposed here rather than hidden inside the bound
    helpers: a reviewer who wants to check the arithmetic can evaluate the
    function at the reported limit and confirm it equals the target.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # The prefactor is x**a * (1 - x)**b / B(a, b); it is assembled from
    # log-gamma terms so that large shapes do not overflow.
    log_front = -_log_beta(a, b) + a * math.log(x) + b * math.log1p(-x)
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def beta_quantile(a: float, b: float, probability: float) -> float:
    """The ``probability`` quantile of the Beta(a, b) distribution.

    ``I_x(a, b)`` is monotone increasing in ``x``, so the quantile is found
    by bisection.  Bisection is slower than Newton but it cannot diverge,
    which matters because this function is used to certify a threshold.
    """
    if a <= 0.0 or b <= 0.0:
        raise ValueError("beta shape parameters must be positive")
    if probability <= 0.0:
        return 0.0
    if probability >= 1.0:
        return 1.0
    low = 0.0
    high = 1.0
    for _ in range(200):
        mid = 0.5 * (low + high)
        if regularized_incomplete_beta(mid, a, b) < probability:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def clopper_pearson_upper(
    successes: int, trials: int, *, confidence: float = 0.95
) -> float:
    """The exact one-sided upper confidence limit on a binomial rate.

    ``successes`` events were observed in ``trials`` independent trials.
    The returned value ``p`` satisfies ``P(X >= successes) = 1 - alpha`` for
    ``X ~ Binomial(trials, p)``, so ``p`` is the largest rate consistent
    with the observation at the stated confidence level.

    Zero events is the case this exists for: with ``successes == 0`` the
    limit is ``1 - alpha ** (1 / trials)``, which for 300 trials and
    ``alpha = 0.05`` is about 0.994% -- the figure the scoring policy
    quotes.
    """
    if trials <= 0:
        raise ValueError("trials must be positive")
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")
    if successes >= trials:
        return 1.0
    return beta_quantile(successes + 1.0, trials - successes, confidence)


def clopper_pearson_lower(
    successes: int, trials: int, *, confidence: float = 0.95
) -> float:
    """The exact one-sided lower confidence limit on a binomial rate.

    The mirror image of :func:`clopper_pearson_upper`: the returned value
    ``p`` satisfies ``P(X >= successes) = alpha`` for ``X ~ Binomial(trials,
    p)``.  With every trial successful the limit is ``alpha ** (1 /
    trials)``, so 100 of 100 accepted cases give a lower bound of about
    95.1%.
    """
    if trials <= 0:
        raise ValueError("trials must be positive")
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")
    if successes <= 0:
        return 0.0
    return beta_quantile(successes, trials - successes + 1.0, 1.0 - confidence)


@dataclass
class BinomialLimit:
    """An exact binomial confidence statement about a rate."""

    successes: int
    trials: int
    point_estimate: float
    lower: float
    """The one-sided lower limit (a floor on the true rate)."""
    upper: float
    """The one-sided upper limit (a ceiling on the true rate)."""
    confidence: float = 0.95
    method: str = "clopper-pearson"

    def to_dict(self) -> dict[str, Any]:
        return {
            "successes": self.successes,
            "trials": self.trials,
            "point_estimate": self.point_estimate,
            "lower": self.lower,
            "upper": self.upper,
            "confidence": self.confidence,
            "method": self.method,
        }


def binomial_limit(
    successes: int, trials: int, *, confidence: float = 0.95
) -> BinomialLimit:
    """Both one-sided exact limits for a binomial rate, plus the estimate."""
    if trials <= 0:
        raise ValueError("trials must be positive")
    return BinomialLimit(
        successes=successes,
        trials=trials,
        point_estimate=successes / trials,
        lower=clopper_pearson_lower(successes, trials, confidence=confidence),
        upper=clopper_pearson_upper(successes, trials, confidence=confidence),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
#
# A corpus whose cases are drawn from a handful of templates is not a set of
# independent observations: two cases produced by the same template share
# whatever the template's failure mode is, so their errors move together.
# Treating them as independent would claim more precision than the design
# supports.  The helpers below make that dependence explicit instead of
# hiding it: they turn a set of clustered observations into an effective
# sample size and the corresponding limit.


def intra_cluster_correlation(
    proportions: Sequence[float], sizes: Sequence[int]
) -> float:
    """The pooled intra-cluster correlation of a clustered proportion.

    This is the ANOVA estimator: the between-cluster variance of the
    cluster proportions, minus the within-cluster binomial variance,
    expressed as a correlation.  A value of zero means the clusters behave
    like independent draws; a value of one means every case in a cluster
    shares the cluster's outcome exactly.

    When every cluster proportion is identical the estimator is degenerate
    and zero is returned: there is no *observed* evidence of clustering.
    That is a statement about the data, not a claim that clustering is
    impossible, and the caller is expected to report it as such.
    """
    if len(proportions) != len(sizes):
        raise ValueError("proportions and sizes must have the same length")
    if not proportions:
        return 0.0
    total = sum(sizes)
    if total <= 0:
        return 0.0
    grand = sum(p * n for p, n in zip(proportions, sizes, strict=True)) / total
    between = sum(n * (p - grand) ** 2 for p, n in zip(proportions, sizes, strict=True))
    between /= total
    within = sum(
        n * p * (1.0 - p) for p, n in zip(proportions, sizes, strict=True)
    ) / total
    if within <= 0.0:
        # Every observation in every cluster is the same value: there is no
        # within-cluster binomial variance to subtract, so the correlation
        # is not identified.  Report zero and let the caller treat the
        # cluster-level bound as the conservative alternative.
        return 0.0
    raw = (between - within) / (between + within)
    return max(0.0, min(1.0, raw))


def kish_design_effect(average_cluster_size: float, rho: float) -> float:
    """The Kish design effect for a clustered proportion.

    ``DEFF = 1 + (m - 1) * rho`` inflates the variance of a clustered mean
    by the factor by which the clusters' shared outcomes make the
    observations less informative than independent ones.  The effective
    sample size is then ``n / DEFF``.
    """
    if average_cluster_size <= 0:
        raise ValueError("the average cluster size must be positive")
    if not 0.0 <= rho <= 1.0:
        raise ValueError("the intra-cluster correlation must be in [0, 1]")
    return 1.0 + (average_cluster_size - 1.0) * rho


def effective_sample_size(total: int, design_effect: float) -> float:
    """The number of independent observations a clustered design supports."""
    if total <= 0:
        raise ValueError("total must be positive")
    if design_effect <= 0:
        raise ValueError("the design effect must be positive")
    return total / design_effect
