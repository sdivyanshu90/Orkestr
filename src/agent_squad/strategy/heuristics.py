"""Coordination cost model.

The question this module answers is narrow and quantitative: *given a task
DAG, is running the tasks concurrently likely to finish sooner than running
them one after another, and is the extra cost worth it?*

The model is deliberately a set of transparent, hand-tuned heuristics rather
than a learned predictor.  A learned model would need training data from a
system that does not exist yet; a transparent model can be inspected,
argued with and -- crucially -- audited against observed outcomes by the
benchmark harness.

Every number produced here is an *estimate*.  The benchmark harness records
estimates and observations side by side so that the model's accuracy can be
measured rather than assumed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..config import StrategyConfig
from ..dag.graph import TaskGraph


@dataclass
class TaskEstimate:
    """Per-task inputs to the coordination model."""

    task_id: str
    duration_s: float
    dependents: int = 0
    dependencies: int = 0
    shared_file_partners: int = 0
    acceptance_criteria: int = 0


@dataclass
class CoordinationModel:
    """Estimates the cost and benefit of parallel execution."""

    config: StrategyConfig
    estimates: list[TaskEstimate] = field(default_factory=list)

    # ----------------------------------------------------------- structure

    @classmethod
    def from_graph(cls, graph: TaskGraph, config: StrategyConfig) -> CoordinationModel:
        overlaps = graph.scope_overlaps()
        partners: dict[str, int] = {tid: 0 for tid in graph.tasks}
        for overlap in overlaps:
            partners[overlap.a] += 1
            partners[overlap.b] += 1

        estimates = [
            TaskEstimate(
                task_id=task.id,
                duration_s=task.estimated_duration_s,
                dependents=len(graph.children(task.id)),
                dependencies=len(task.depends_on),
                shared_file_partners=partners.get(task.id, 0),
                acceptance_criteria=len(task.acceptance_criteria),
            )
            for task in graph.tasks.values()
        ]
        return cls(config=config, estimates=estimates)

    # ------------------------------------------------------------- numbers

    @property
    def task_count(self) -> int:
        return len(self.estimates)

    @property
    def total_duration_s(self) -> float:
        return sum(e.duration_s for e in self.estimates)

    @property
    def mean_duration_s(self) -> float:
        if not self.estimates:
            return 0.0
        return self.total_duration_s / len(self.estimates)

    def shared_file_conflict_ratio(self) -> float:
        """Fraction of tasks that share at least one anticipated file."""
        if not self.estimates:
            return 0.0
        conflicted = sum(1 for e in self.estimates if e.shared_file_partners > 0)
        return conflicted / len(self.estimates)

    def integration_risk(self) -> float:
        """Rough probability-like score that integration will need rework.

        Driven by how many tasks touch the same files and by how deep the
        dependency structure is (deep structures amplify a single conflict).
        """
        conflict_ratio = self.shared_file_conflict_ratio()
        depth_factor = 1.0
        if self.estimates:
            max_deps = max((e.dependencies for e in self.estimates), default=0)
            depth_factor = 1.0 + min(0.5, max_deps * 0.1)
        risk = min(1.0, conflict_ratio * depth_factor)
        return round(risk * self.config.integration_risk_penalty, 6)

    def coordination_overhead_tokens(self, workers: int) -> int:
        """Extra tokens spent on coordination for ``workers`` workers."""
        if workers <= 1:
            return 0
        per_worker = self.config.coordination_overhead_tokens
        # Each additional worker needs its own copy of the shared context.
        duplication = self.config.context_duplication_tokens * (workers - 1)
        fanout = per_worker * (workers - 1)
        return int(fanout + duplication)

    def context_duplication_tokens(self, workers: int) -> int:
        if workers <= 1:
            return 0
        return int(self.config.context_duplication_tokens * (workers - 1))

    def expected_speedup(self, workers: int, width: int) -> float:
        """Expected speed-up factor relative to serial execution.

        Amdahl-like: the parallelisable fraction is reduced by the fact that
        the critical path is inherently serial, then reduced again by the
        coordination overhead expressed in wall-clock terms.
        """
        if workers <= 1 or self.task_count <= 1:
            return 1.0
        serial = self.total_duration_s
        if serial <= 0:
            return 1.0
        levels = self._levels_hint(width)
        critical = self._critical_path_estimate()
        serial_fraction = min(1.0, critical / serial) if serial else 1.0
        parallel_fraction = 1.0 - serial_fraction
        effective_workers = max(1, min(workers, width))
        # Sub-linear scaling: real coordination costs grow faster than the
        # ideal Amdahl curve.
        raw = 1.0 / (serial_fraction + parallel_fraction / effective_workers)
        raw = 1.0 + (raw - 1.0) * (
            effective_workers ** (self.config.parallel_speedup_exponent - 1.0)
        )
        overhead_penalty = 1.0 - min(0.5, self.integration_risk() * 0.5)
        return round(max(1.0, raw * overhead_penalty), 4)

    def _levels_hint(self, width: int) -> int:
        return max(1, width)

    def _critical_path_estimate(self) -> float:
        if not self.estimates:
            return 0.0
        by_id = {e.task_id: e for e in self.estimates}
        best = 0.0
        for estimate in self.estimates:
            # Approximate: a task's serial contribution is its own duration
            # plus the longest dependency chain above it.
            chain = estimate.duration_s
            best = max(best, chain)
        return min(best, self.total_duration_s)

    def estimated_wall_time(self, workers: int, width: int) -> float:
        """Estimated wall-clock seconds for ``workers`` workers."""
        if workers <= 1:
            return self.total_duration_s
        speedup = self.expected_speedup(workers, width)
        if speedup <= 0:
            return math.inf
        return self.total_duration_s / speedup

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_count": self.task_count,
            "total_duration_s": self.total_duration_s,
            "mean_duration_s": self.mean_duration_s,
            "shared_file_conflict_ratio": self.shared_file_conflict_ratio(),
            "integration_risk": self.integration_risk(),
        }
