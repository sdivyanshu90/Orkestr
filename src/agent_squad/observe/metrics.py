"""Metric collection for the orchestration efficiency laboratory.

The metrics here are the ones the research question actually needs:

* **verified completion rate** -- how often did the run produce work that
  passed independent verification, as opposed to work an agent claimed to
  have done;
* **wall-clock time** -- end to end, and on the critical path;
* **token usage and cost** -- measured, with the reported/estimated
  distinction preserved;
* **tool calls** -- how much work the agents actually did;
* **coordination overhead** -- tokens spent on messages, handoffs and
  duplicated context;
* **integration conflicts** -- the price of parallelism;
* **retries, verification failures, human interventions, policy violations**.

Everything is a plain dataclass so that results can be serialised into a
report without a metrics backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunMetrics:
    """Metrics for a single execution of a task set."""

    run_id: str
    strategy: str = "unknown"
    workers: int = 1
    trials: int = 1

    total_tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    cancelled: int = 0

    verified_tasks: int = 0
    advisory_only_tasks: int = 0

    wall_clock_s: float = 0.0
    critical_path_s: float = 0.0

    tokens_used: int = 0
    cost_usd: float = 0.0
    reported_usage_ratio: float = 1.0

    tool_calls: int = 0
    model_calls: int = 0
    messages: int = 0
    coordination_tokens: int = 0

    integration_conflicts: int = 0
    patches_applied: int = 0
    integration_passed: bool = False

    retries: int = 0
    verification_failures: int = 0
    policy_denials: int = 0
    human_interventions: int = 0
    stale_updates_rejected: int = 0

    @property
    def verified_completion_rate(self) -> float:
        if self.total_tasks == 0:
            return 0.0
        return self.verified_tasks / self.total_tasks

    @property
    def tokens_per_verified_task(self) -> float:
        if self.verified_tasks == 0:
            return float("inf") if self.tokens_used else 0.0
        return self.tokens_used / self.verified_tasks

    @property
    def cost_per_verified_task(self) -> float:
        if self.verified_tasks == 0:
            return float("inf") if self.cost_usd else 0.0
        return self.cost_usd / self.verified_tasks

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "workers": self.workers,
            "trials": self.trials,
            "total_tasks": self.total_tasks,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "blocked": self.blocked,
            "cancelled": self.cancelled,
            "verified_tasks": self.verified_tasks,
            "advisory_only_tasks": self.advisory_only_tasks,
            "verified_completion_rate": round(self.verified_completion_rate, 6),
            "wall_clock_s": round(self.wall_clock_s, 6),
            "critical_path_s": round(self.critical_path_s, 6),
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd, 8),
            "reported_usage_ratio": round(self.reported_usage_ratio, 6),
            "tool_calls": self.tool_calls,
            "model_calls": self.model_calls,
            "messages": self.messages,
            "coordination_tokens": self.coordination_tokens,
            "integration_conflicts": self.integration_conflicts,
            "patches_applied": self.patches_applied,
            "integration_passed": self.integration_passed,
            "retries": self.retries,
            "verification_failures": self.verification_failures,
            "policy_denials": self.policy_denials,
            "human_interventions": self.human_interventions,
            "stale_updates_rejected": self.stale_updates_rejected,
            "tokens_per_verified_task": (
                None if self.tokens_per_verified_task == float("inf")
                else round(self.tokens_per_verified_task, 4)
            ),
            "cost_per_verified_task": (
                None if self.cost_per_verified_task == float("inf")
                else round(self.cost_per_verified_task, 8)
            ),
        }


@dataclass
class StrategyAggregate:
    """Aggregated results for one strategy across trials."""

    strategy: str
    workers: int
    trials: list[RunMetrics] = field(default_factory=list)

    @property
    def mean(self) -> dict[str, float]:
        if not self.trials:
            return {}
        keys = [
            "verified_completion_rate",
            "wall_clock_s",
            "tokens_used",
            "cost_usd",
            "tool_calls",
            "integration_conflicts",
            "retries",
            "verification_failures",
            "policy_denials",
        ]
        out: dict[str, float] = {}
        for key in keys:
            values = [float(getattr(t, key)) for t in self.trials]
            out[key] = sum(values) / len(values)
        return out

    @property
    def stdev(self) -> dict[str, float]:
        if len(self.trials) < 2:
            return {}
        import statistics

        keys = [
            "verified_completion_rate",
            "wall_clock_s",
            "tokens_used",
            "cost_usd",
            "tool_calls",
            "integration_conflicts",
        ]
        out: dict[str, float] = {}
        for key in keys:
            values = [float(getattr(t, key)) for t in self.trials]
            out[key] = statistics.stdev(values)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "workers": self.workers,
            "trials": len(self.trials),
            "mean": {k: round(v, 6) for k, v in self.mean.items()},
            "stdev": {k: round(v, 6) for k, v in self.stdev.items()},
            "runs": [t.run_id for t in self.trials],
        }


@dataclass
class Comparison:
    """A comparison between two strategies."""

    baseline: str
    candidate: str
    metric: str
    baseline_value: float
    candidate_value: float
    improved: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "candidate": self.candidate,
            "metric": self.metric,
            "baseline_value": round(self.baseline_value, 6),
            "candidate_value": round(self.candidate_value, 6),
            "improved": self.improved,
            "note": self.note,
        }
