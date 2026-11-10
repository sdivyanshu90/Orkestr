"""Unit tests for the task graph."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from agent_squad.dag.graph import TaskGraph
from agent_squad.errors import DependencyError, ValidationError
from agent_squad.models.task import (
    AcceptanceCriterion,
    CheckKind,
    Complexity,
    RepoScope,
    Role,
    TaskSpec,
)


def make_task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    duration: float = 10.0,
    files: tuple[str, ...] = (),
    role: Role = Role.IMPLEMENTER,
    depth: int = 0,
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        run_id="run_test",
        title=f"task {task_id}",
        objective=f"do {task_id}",
        depends_on=depends_on,
        scope=RepoScope(anticipated_files=files),
        role=role,
        complexity=Complexity.LOW,
        estimated_duration_s=duration,
        delegation_depth=depth,
        acceptance_criteria=(
            AcceptanceCriterion(
                id=f"{task_id}-c",
                description="a file exists",
                kind=CheckKind.FILE_EXISTS,
                spec={"path": f"{task_id}.py"},
            ),
        ),
    )


def test_dag_rejects_unknown_dependency() -> None:
    graph = TaskGraph()
    graph.add(make_task("a", depends_on=("missing",)))
    with pytest.raises(DependencyError) as exc:
        graph.validate()
    assert "missing" in str(exc.value)


def test_dag_rejects_self_dependency() -> None:
    # The task specification refuses a self-dependency at construction time,
    # before the graph ever sees it.
    with pytest.raises(PydanticValidationError) as exc:
        make_task("a", depends_on=("a",))
    assert "depend on itself" in str(exc.value)


def test_dag_detects_a_cycle() -> None:
    graph = TaskGraph()
    graph.add(make_task("a", depends_on=("b",)))
    graph.add(make_task("b", depends_on=("a",)))
    with pytest.raises(DependencyError) as exc:
        graph.validate()
    assert "cycle" in str(exc.value)


def test_dag_detects_a_longer_cycle() -> None:
    graph = TaskGraph()
    graph.add(make_task("a", depends_on=("c",)))
    graph.add(make_task("b", depends_on=("a",)))
    graph.add(make_task("c", depends_on=("b",)))
    cycle = graph.find_cycle()
    assert cycle is not None
    assert set(cycle) == {"a", "b", "c"}


def test_dag_rejects_excessive_delegation_depth() -> None:
    graph = TaskGraph()
    graph.add(make_task("a", depth=5))
    with pytest.raises(DependencyError) as exc:
        graph.validate(max_delegation_depth=2)
    assert "delegation depth" in str(exc.value)


def test_dag_warns_about_advisory_only_tasks() -> None:
    graph = TaskGraph()
    task = make_task("a")
    advisory = task.model_copy(update={"acceptance_criteria": ()})
    graph.add(advisory)
    problems = graph.validate()
    assert any("deterministic acceptance criteria" in p for p in problems)


def test_dag_rejects_duplicate_task_ids() -> None:
    graph = TaskGraph()
    graph.add(make_task("a"))
    with pytest.raises(ValidationError):
        graph.add(make_task("a"))


def test_topological_order_respects_dependencies() -> None:
    graph = TaskGraph()
    graph.add_all(
        [
            make_task("a"),
            make_task("b", depends_on=("a",)),
            make_task("c", depends_on=("a",)),
            make_task("d", depends_on=("b", "c")),
        ]
    )
    graph.link()
    order = graph.topological_order()
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_levels_group_independent_tasks() -> None:
    graph = TaskGraph()
    graph.add_all(
        [
            make_task("a"),
            make_task("b"),
            make_task("c", depends_on=("a", "b")),
        ]
    )
    graph.link()
    levels = graph.levels()
    assert levels[0] == ["a", "b"]
    assert levels[1] == ["c"]
    assert graph.parallel_width() == 2


def test_ready_tasks_requires_dependencies() -> None:
    graph = TaskGraph()
    graph.add_all([make_task("a"), make_task("b", depends_on=("a",))])
    graph.link()
    ready = graph.ready_tasks(completed=set(), blocked=set())
    assert [t.id for t in ready] == ["a"]
    ready = graph.ready_tasks(completed={"a"}, blocked=set())
    assert [t.id for t in ready] == ["b"]


def test_blocked_by_failure_propagates_to_descendants() -> None:
    graph = TaskGraph()
    graph.add_all(
        [
            make_task("a"),
            make_task("b", depends_on=("a",)),
            make_task("c", depends_on=("b",)),
            make_task("d"),
        ]
    )
    graph.link()
    blocked = graph.blocked_by_failure({"a"})
    assert blocked == {"b", "c"}
    assert "d" not in blocked


def test_critical_path_follows_the_longest_chain() -> None:
    graph = TaskGraph()
    graph.add_all(
        [
            make_task("a", duration=5),
            make_task("b", depends_on=("a",), duration=5),
            make_task("c", depends_on=("a",), duration=1),
            make_task("d", duration=3),
        ]
    )
    graph.link()
    chain, total = graph.critical_path()
    assert chain == ["a", "b"]
    assert total == 10


def test_scope_overlaps_detect_shared_files() -> None:
    graph = TaskGraph()
    graph.add_all(
        [
            make_task("a", files=("shared.py", "a.py")),
            make_task("b", files=("shared.py",)),
            make_task("c", files=("other.py",)),
        ]
    )
    graph.link()
    overlaps = graph.scope_overlaps()
    assert len(overlaps) == 1
    assert overlaps[0].a == "a"
    assert overlaps[0].b == "b"
    assert overlaps[0].shared_paths == ("shared.py",)


def test_repo_scope_matching() -> None:
    scope = RepoScope(include_paths=("src/",), exclude_paths=("src/generated/",))
    assert scope.touched("src/app.py")
    assert not scope.touched("docs/readme.md")
    assert not scope.touched("src/generated/x.py")


def test_repo_scope_without_includes_allows_everything() -> None:
    scope = RepoScope()
    assert scope.touched("anything/at/all.py")


def test_task_spec_rejects_duplicate_criteria() -> None:
    criterion = AcceptanceCriterion(
        id="c", description="d", kind=CheckKind.FILE_EXISTS, spec={"path": "x"}
    )
    with pytest.raises(PydanticValidationError):
        TaskSpec(
            id="t",
            run_id="r",
            title="t",
            objective="o",
            acceptance_criteria=(criterion, criterion),
        )


def test_criterion_requires_the_spec_keys_for_its_kind() -> None:
    with pytest.raises(PydanticValidationError):
        AcceptanceCriterion(id="c", description="d", kind=CheckKind.FILE_EXISTS, spec={})


def test_criterion_rejects_an_empty_command() -> None:
    with pytest.raises(PydanticValidationError):
        AcceptanceCriterion(
            id="c",
            description="d",
            kind=CheckKind.COMMAND_EXITS_ZERO,
            spec={"command": []},
        )
