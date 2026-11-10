"""Deterministic strategy selector.

Given a task DAG the selector answers: *how many agents should work on this
in parallel, and is that worth doing at all?*  The answer is a
:class:`~agent_squad.models.run.StrategyDecision` containing the chosen
strategy plus the reasoning that produced it, so that a later analyst can
check whether the decision was sensible in hindsight.

The selector is deterministic: same graph, same configuration, same
decision.  That is a hard requirement for the replay and benchmark features
-- a decision engine that changes its mind between runs cannot be audited.
"""

from __future__ import annotations

from typing import Any

from ..config import StrategyConfig
from ..dag.graph import TaskGraph
from ..models.run import StrategyDecision, StrategyName
from .heuristics import CoordinationModel


class StrategySelector:
    """Chooses between single-agent and multi-agent execution."""

    def __init__(self, config: StrategyConfig) -> None:
        self._config = config

    def select(
        self,
        graph: TaskGraph,
        *,
        override: str | None = None,
        max_workers_override: int | None = None,
        token_budget: int | None = None,
        cost_budget_usd: float | None = None,
        price_per_1k_tokens: float = 0.0,
    ) -> StrategyDecision:
        model = CoordinationModel.from_graph(graph, self._config)
        width = graph.parallel_width()
        chain, critical_duration = graph.critical_path()

        rationale: list[str] = []
        factors: dict[str, Any] = {
            "levels": [len(level) for level in graph.levels()],
            "critical_path": chain,
            "critical_path_duration_s": critical_duration,
            "parallel_width": width,
            **model.to_dict(),
        }

        # --- explicit override -------------------------------------------
        if override:
            strategy = StrategyName(override)
            workers = (
                max_workers_override
                or strategy.worker_count
                or self._config.max_workers
            )
            rationale.append(f"strategy overridden by configuration to {strategy.value}")
            return StrategyDecision(
                strategy=strategy,
                worker_count=workers,
                independent_tasks=model.task_count,
                critical_path_tasks=len(chain),
                estimated_parallel_speedup=model.expected_speedup(workers, width),
                estimated_coordination_overhead_tokens=model.coordination_overhead_tokens(workers),
                estimated_context_duplication_tokens=model.context_duplication_tokens(workers),
                estimated_integration_risk=model.integration_risk(),
                rationale=tuple(rationale),
                factors=factors,
            )

        # --- trivial case --------------------------------------------------
        if model.task_count < self._config.min_tasks_for_parallel:
            rationale.append(
                f"only {model.task_count} task(s) in the graph; parallelism cannot "
                "amortise the coordination cost"
            )
            return self._decision(
                StrategyName.SINGLE,
                1,
                model,
                width,
                chain,
                rationale,
                factors,
            )

        # --- candidate comparison ------------------------------------------
        candidates = self._candidates(width)
        serial_time = model.total_duration_s
        serial_cost = self._cost(model.task_count, price_per_1k_tokens)

        scored: list[tuple[float, int, float, int]] = []
        for workers in candidates:
            wall = model.estimated_wall_time(workers, width)
            tokens = self._tokens(model.task_count, workers, price_per_1k_tokens)
            cost = tokens / 1000.0 * price_per_1k_tokens
            scored.append((wall, workers, tokens, cost))

        best_wall, best_workers, best_tokens, best_cost = min(scored, key=lambda s: s[0])

        rationale.append(
            f"serial execution estimated at {serial_time:.1f}s; "
            f"best parallel option uses {best_workers} worker(s) "
            f"at ~{best_wall:.1f}s"
        )

        # --- budget constraints --------------------------------------------
        if token_budget is not None and best_tokens > token_budget:
            rationale.append(
                f"the parallel plan needs ~{best_tokens} tokens which exceeds the "
                f"run budget of {token_budget}; falling back to serial execution"
            )
            return self._decision(
                StrategyName.SINGLE, 1, model, width, chain, rationale, factors
            )
        if cost_budget_usd is not None and best_cost > cost_budget_usd:
            rationale.append(
                f"the parallel plan costs ~${best_cost:.4f} which exceeds the run "
                f"budget of ${cost_budget_usd:.4f}; falling back to serial execution"
            )
            return self._decision(
                StrategyName.SINGLE, 1, model, width, chain, rationale, factors
            )

        # --- is parallelism actually worth it? ------------------------------
        improvement = (serial_time - best_wall) / serial_time if serial_time else 0.0
        if improvement <= 0.02:
            rationale.append(
                f"parallelism improves estimated wall-clock time by only "
                f"{improvement * 100:.1f}%, which does not justify the added "
                "coordination and integration risk"
            )
            return self._decision(
                StrategyName.SINGLE, 1, model, width, chain, rationale, factors
            )

        rationale.append(
            f"parallelism improves estimated wall-clock time by {improvement * 100:.1f}%"
        )
        if model.shared_file_conflict_ratio() > 0.5:
            rationale.append(
                f"{model.shared_file_conflict_ratio() * 100:.0f}% of tasks share "
                "anticipated files, which raises integration risk"
            )

        strategy = {
            1: StrategyName.SINGLE,
            2: StrategyName.STATIC_TWO,
            4: StrategyName.STATIC_FOUR,
        }.get(best_workers, StrategyName.ADAPTIVE)
        if strategy in (StrategyName.STATIC_TWO, StrategyName.STATIC_FOUR):
            rationale.append(
                f"selected the static {best_workers}-agent strategy because the "
                "graph width and budget both support a fixed worker count"
            )
        else:
            strategy = StrategyName.ADAPTIVE
            rationale.append(
                "selected the adaptive strategy; the best worker count is not one "
                "of the fixed benchmarks"
            )
        return self._decision(strategy, best_workers, model, width, chain, rationale, factors)

    # ------------------------------------------------------------- helpers

    def _candidates(self, width: int) -> list[int]:
        counts = {1, min(2, self._config.max_workers), min(4, self._config.max_workers)}
        counts = {c for c in counts if c >= 1}
        feasible = sorted(c for c in counts if c <= max(1, width))
        return feasible or [1]

    @staticmethod
    def _tokens(task_count: int, workers: int, price_per_1k_tokens: float) -> int:
        base = 1200 * task_count
        overhead = 600 * max(0, workers - 1)
        return int(base + overhead)

    @staticmethod
    def _cost(task_count: int, price_per_1k_tokens: float) -> float:
        return round(task_count * 1200 / 1000.0 * price_per_1k_tokens, 6)

    def _decision(
        self,
        strategy: StrategyName,
        workers: int,
        model: CoordinationModel,
        width: int,
        chain: list[str],
        rationale: list[str],
        factors: dict[str, Any],
    ) -> StrategyDecision:
        return StrategyDecision(
            strategy=strategy,
            worker_count=workers,
            independent_tasks=model.task_count,
            critical_path_tasks=len(chain),
            estimated_parallel_speedup=model.expected_speedup(workers, width),
            estimated_coordination_overhead_tokens=model.coordination_overhead_tokens(workers),
            estimated_context_duplication_tokens=model.context_duplication_tokens(workers),
            estimated_integration_risk=model.integration_risk(),
            rationale=tuple(rationale),
            factors=factors,
        )
