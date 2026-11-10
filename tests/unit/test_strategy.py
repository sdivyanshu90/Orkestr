"""Unit tests for the strategy selector and the coordination cost model."""

from __future__ import annotations

import pytest

from agent_squad.config import StrategyConfig
from agent_squad.dag.graph import TaskGraph
from agent_squad.models.run import StrategyName
from agent_squad.models.task import (
    AcceptanceCriterion,
    CheckKind,
    Complexity,
    RepoScope,
    Role,
    TaskSpec,
)
from agent_squad.strategy.heuristics import CoordinationModel
from agent_squad.strategy.selector import StrategySelector


def task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    duration: float = 60.0,
    files: tuple[str, ...] = (),
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        run_id="run_test",
        title=task_id,
        objective=task_id,
        depends_on=depends_on,
        scope=RepoScope(anticipated_files=files),
        role=Role.IMPLEMENTER,
        complexity=Complexity.MEDIUM,
        estimated_duration_s=duration,
        acceptance_criteria=(
            AcceptanceCriterion(
                id=f"{task_id}-c",
                description="c",
                kind=CheckKind.FILE_EXISTS,
                spec={"path": f"{task_id}.py"},
            ),
        ),
    )


def graph_of(*tasks: TaskSpec) -> TaskGraph:
    graph = TaskGraph()
    graph.add_all(list(tasks))
    graph.link()
    return graph


def test_single_task_selects_the_single_agent_strategy() -> None:
    selector = StrategySelector(StrategyConfig())
    decision = selector.select(graph_of(task("a")))
    assert decision.strategy is StrategyName.SINGLE
    assert decision.worker_count == 1
    assert decision.rationale


def test_independent_tasks_can_select_parallelism() -> None:
    selector = StrategySelector(StrategyConfig())
    graph = graph_of(task("a", files=("a.py",)), task("b", files=("b.py",)), task("c", files=("c.py",)))
    decision = selector.select(graph)
    assert decision.strategy in (StrategyName.STATIC_TWO, StrategyName.STATIC_FOUR, StrategyName.ADAPTIVE)
    assert decision.worker_count >= 2


def test_shared_files_reduce_the_appeal_of_parallelism() -> None:
    selector = StrategySelector(StrategyConfig())
    independent = graph_of(
        task("a", files=("a.py",)), task("b", files=("b.py",)), task("c", files=("c.py",))
    )
    shared = graph_of(
        task("a", files=("shared.py",)),
        task("b", files=("shared.py",)),
        task("c", files=("shared.py",)),
    )
    independent_decision = selector.select(independent)
    shared_decision = selector.select(shared)
    assert shared_decision.estimated_integration_risk > independent_decision.estimated_integration_risk


def test_sequential_dependencies_limit_the_width() -> None:
    selector = StrategySelector(StrategyConfig())
    graph = graph_of(
        task("a"), task("b", depends_on=("a",)), task("c", depends_on=("b",))
    )
    decision = selector.select(graph)
    assert decision.critical_path_tasks == 3
    assert decision.factors["parallel_width"] == 1


def test_a_token_budget_forces_serial_execution() -> None:
    selector = StrategySelector(StrategyConfig())
    graph = graph_of(
        task("a", files=("a.py",)), task("b", files=("b.py",)), task("c", files=("c.py",))
    )
    decision = selector.select(graph, token_budget=1)
    assert decision.strategy is StrategyName.SINGLE
    assert any("budget" in r for r in decision.rationale)


def test_a_cost_budget_forces_serial_execution() -> None:
    selector = StrategySelector(StrategyConfig())
    graph = graph_of(
        task("a", files=("a.py",)), task("b", files=("b.py",)), task("c", files=("c.py",))
    )
    # A price must be supplied for a cost ceiling to mean anything; with a
    # zero price every strategy is free.
    decision = selector.select(graph, cost_budget_usd=0.0001, price_per_1k_tokens=1.0)
    assert decision.strategy is StrategyName.SINGLE
    assert any("budget" in r for r in decision.rationale)


def test_a_zero_price_makes_a_cost_budget_unenforceable() -> None:
    selector = StrategySelector(StrategyConfig())
    graph = graph_of(
        task("a", files=("a.py",)), task("b", files=("b.py",)), task("c", files=("c.py",))
    )
    decision = selector.select(graph, cost_budget_usd=0.0001, price_per_1k_tokens=0.0)
    assert decision.strategy is not StrategyName.SINGLE


def test_an_explicit_override_is_honoured_and_explained() -> None:
    selector = StrategySelector(StrategyConfig())
    graph = graph_of(task("a"))
    decision = selector.select(graph, override="static_four")
    assert decision.strategy is StrategyName.STATIC_FOUR
    assert decision.worker_count == 4
    assert any("overridden" in r for r in decision.rationale)


def test_the_decision_is_deterministic() -> None:
    selector = StrategySelector(StrategyConfig())
    graph = graph_of(
        task("a", files=("a.py",)), task("b", files=("b.py",)), task("c", files=("c.py",))
    )
    first = selector.select(graph)
    second = selector.select(graph)
    assert first.strategy == second.strategy
    assert first.worker_count == second.worker_count
    assert first.estimated_parallel_speedup == second.estimated_parallel_speedup


def test_the_coordination_model_reports_the_conflict_ratio() -> None:
    model = CoordinationModel.from_graph(
        graph_of(
            task("a", files=("shared.py",)),
            task("b", files=("shared.py",)),
            task("c", files=("other.py",)),
        ),
        StrategyConfig(),
    )
    assert model.shared_file_conflict_ratio() == pytest.approx(2 / 3)


def test_coordination_overhead_grows_with_the_worker_count() -> None:
    model = CoordinationModel.from_graph(graph_of(task("a")), StrategyConfig())
    assert model.coordination_overhead_tokens(1) == 0
    assert model.coordination_overhead_tokens(2) > model.coordination_overhead_tokens(1)
    assert model.coordination_overhead_tokens(4) > model.coordination_overhead_tokens(2)
