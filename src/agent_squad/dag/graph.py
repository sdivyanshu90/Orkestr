"""Task dependency graph.

The DAG is the orchestrator's central data structure: it encodes *what may
run when*, and therefore everything that has to be validated before any
worker is dispatched.

Validation is strict and total.  A graph that contains a cycle, a dangling
dependency, a self-dependency or a delegation-depth violation is rejected
before execution starts, because none of those conditions can be repaired
mid-flight without corrupting a run.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from ..errors import DependencyError, ValidationError
from ..models.task import Role, TaskSpec, TaskStatus


@dataclass(frozen=True)
class ScopeOverlap:
    """Two tasks that may touch the same files."""

    a: str
    b: str
    shared_paths: tuple[str, ...]

    @property
    def severity(self) -> float:
        return min(1.0, len(self.shared_paths) / 4.0)


@dataclass
class TaskGraph:
    """A validated directed acyclic graph of tasks."""

    tasks: dict[str, TaskSpec] = field(default_factory=dict)
    _children: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    # ---------------------------------------------------------------- build

    def add(self, task: TaskSpec) -> None:
        if task.id in self.tasks:
            raise ValidationError(f"duplicate task id: {task.id}", task_id=task.id)
        self.tasks[task.id] = task
        self._children[task.id] = set()

    def add_all(self, tasks: list[TaskSpec]) -> None:
        for task in tasks:
            self.add(task)

    def link(self) -> None:
        """Build the child index from the declared dependencies."""
        self._children = defaultdict(set)
        for task in self.tasks.values():
            for dep in task.depends_on:
                if dep in self.tasks:
                    self._children[dep].add(task.id)

    # --------------------------------------------------------------- access

    def __len__(self) -> int:
        return len(self.tasks)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self.tasks

    def get(self, task_id: str) -> TaskSpec:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise DependencyError(f"unknown task: {task_id}", task_id=task_id) from exc

    @property
    def roots(self) -> list[TaskSpec]:
        return [t for t in self.tasks.values() if not t.depends_on]

    def children(self, task_id: str) -> set[str]:
        return set(self._children.get(task_id, set()))

    def ancestors(self, task_id: str) -> set[str]:
        """All transitive dependencies of ``task_id`` (excluding itself)."""
        seen: set[str] = set()
        stack = list(self.tasks[task_id].depends_on) if task_id in self.tasks else []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            task = self.tasks.get(current)
            if task:
                stack.extend(task.depends_on)
        return seen

    def descendants(self, task_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [task_id]
        while stack:
            current = stack.pop()
            for child in self._children.get(current, set()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    # ------------------------------------------------------------ validation

    def validate(self, *, max_delegation_depth: int = 2) -> list[str]:
        """Validate the graph, returning a list of human-readable problems.

        Raises :class:`DependencyError` for structural problems that make
        execution impossible.  Softer policy problems (for example a task
        with no deterministic acceptance criteria) are returned as warnings so
        the planner can surface them without aborting the run.
        """
        problems: list[str] = []

        for task in self.tasks.values():
            for dep in task.depends_on:
                if dep not in self.tasks:
                    raise DependencyError(
                        f"task {task.id!r} depends on unknown task {dep!r}",
                        task_id=task.id,
                        dependency=dep,
                    )
                if dep == task.id:
                    raise DependencyError(
                        f"task {task.id!r} depends on itself", task_id=task.id
                    )

        cycle = self.find_cycle()
        if cycle:
            raise DependencyError(
                "dependency cycle detected: " + " -> ".join(cycle), cycle=list(cycle)
            )

        for task in self.tasks.values():
            if task.delegation_depth > max_delegation_depth:
                raise DependencyError(
                    f"task {task.id!r} exceeds max delegation depth "
                    f"({task.delegation_depth} > {max_delegation_depth})",
                    task_id=task.id,
                )
            if task.parent_id is not None and task.parent_id not in self.tasks:
                raise DependencyError(
                    f"task {task.id!r} has unknown parent {task.parent_id!r}",
                    task_id=task.id,
                )
            if task.role == Role.INTEGRATION:
                problems.append(
                    f"task {task.id!r} uses the integration role, which is reserved "
                    "for the deterministic integration controller"
                )
            if task.is_advisory_only:
                problems.append(
                    f"task {task.id!r} has no deterministic acceptance criteria; "
                    "it cannot be machine-verified"
                )
            if not task.acceptance_criteria:
                problems.append(f"task {task.id!r} declares no acceptance criteria")

        return problems

    def find_cycle(self) -> list[str] | None:
        """Return a cycle as a list of task ids, or ``None`` if acyclic."""
        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(self.tasks, WHITE)
        parent: dict[str, str | None] = {tid: None for tid in self.tasks}

        for root in self.tasks:
            if colour[root] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(root, 0)]
            while stack:
                node, idx = stack[-1]
                if idx == 0:
                    colour[node] = GREY
                deps = self.tasks[node].depends_on
                if idx < len(deps):
                    stack[-1] = (node, idx + 1)
                    nxt = deps[idx]
                    if colour[nxt] == GREY:
                        cycle = [nxt]
                        cur = node
                        while cur is not None and cur != nxt:
                            cycle.append(cur)
                            cur = parent[cur]
                        cycle.append(nxt)
                        cycle.reverse()
                        return cycle
                    if colour[nxt] == WHITE:
                        parent[nxt] = node
                        stack.append((nxt, 0))
                else:
                    colour[node] = BLACK
                    stack.pop()
        return None

    # -------------------------------------------------------------- ordering

    def topological_order(self) -> list[str]:
        """Kahn's algorithm with a deterministic tie-break on task id."""
        indegree = {tid: len(t.depends_on) for tid, t in self.tasks.items()}
        ready = deque(sorted([tid for tid, d in indegree.items() if d == 0]))
        order: list[str] = []
        while ready:
            current = ready.popleft()
            order.append(current)
            newly: list[str] = []
            for child in sorted(self._children.get(current, set())):
                indegree[child] -= 1
                if indegree[child] == 0:
                    newly.append(child)
            for child in sorted(newly):
                ready.append(child)
            if len(order) > len(self.tasks):  # pragma: no cover - defensive
                raise DependencyError("topological sort did not converge")
        if len(order) != len(self.tasks):
            raise DependencyError("graph contains a cycle")
        return order

    def levels(self) -> list[list[str]]:
        """Group tasks into dependency levels (all tasks in a level are independent)."""
        levels: list[list[str]] = []
        done: set[str] = set()
        pending = set(self.tasks)
        while pending:
            level = sorted(tid for tid in pending if all(d in done for d in self.tasks[tid].depends_on))
            if not level:  # pragma: no cover - guarded by validate()
                raise DependencyError("cycle while computing levels")
            levels.append(level)
            done.update(level)
            pending.difference_update(level)
        return levels

    # ------------------------------------------------------------ scheduling

    def ready_tasks(self, completed: set[str], blocked: set[str]) -> list[TaskSpec]:
        """Tasks whose dependencies are all satisfied and which are not blocked."""
        ready: list[TaskSpec] = []
        for task in self.tasks.values():
            if task.id in completed or task.id in blocked:
                continue
            if all(dep in completed for dep in task.depends_on):
                ready.append(task)
        ready.sort(key=lambda t: (-t.priority, t.id))
        return ready

    def blocked_by_failure(self, failed: set[str]) -> set[str]:
        """Every task that can no longer run because an ancestor failed."""
        blocked: set[str] = set()
        frontier = set(failed)
        while frontier:
            current = frontier.pop()
            for child in self._children.get(current, set()):
                if child not in blocked:
                    blocked.add(child)
                    frontier.add(child)
        return blocked

    def critical_path(self) -> tuple[list[str], float]:
        """Longest dependency chain by estimated duration.

        Returns the chain of task ids and its total estimated duration.
        Ties are broken deterministically by task id.
        """
        if not self.tasks:
            return [], 0.0
        memo: dict[str, tuple[list[str], float]] = {}

        def visit(tid: str) -> tuple[list[str], float]:
            if tid in memo:
                return memo[tid]
            task = self.tasks[tid]
            best_chain: list[str] = []
            best_time = 0.0
            for dep in sorted(task.depends_on):
                chain, total = visit(dep)
                if total > best_time:
                    best_chain, best_time = chain, total
            result = ([*best_chain, tid], best_time + task.estimated_duration_s)
            memo[tid] = result
            return result

        best: tuple[list[str], float] = ([], 0.0)
        for tid in sorted(self.tasks):
            chain, total = visit(tid)
            if total > best[1]:
                best = (chain, total)
        return best

    def parallel_width(self) -> int:
        """Maximum number of tasks that could run at the same time."""
        levels = self.levels()
        return max((len(lvl) for lvl in levels), default=0)

    # ----------------------------------------------------------------- scope

    def scope_overlaps(self) -> list[ScopeOverlap]:
        """Pairs of tasks whose anticipated file sets intersect."""
        by_task: dict[str, set[str]] = {}
        for task in self.tasks.values():
            paths = set(task.scope.anticipated_files)
            for criterion in task.acceptance_criteria:
                path = criterion.spec.get("path")
                if isinstance(path, str):
                    paths.add(path)
            by_task[task.id] = paths
        overlaps: list[ScopeOverlap] = []
        ids = sorted(by_task)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                shared = tuple(sorted(by_task[a] & by_task[b]))
                if shared:
                    overlaps.append(ScopeOverlap(a=a, b=b, shared_paths=shared))
        return overlaps

    # ------------------------------------------------------------------ misc

    def states(self) -> dict[str, TaskStatus]:
        return {tid: TaskStatus.PENDING for tid in self.tasks}
