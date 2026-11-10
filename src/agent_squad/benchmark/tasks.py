"""Benchmark scenarios.

Each scenario is a *complete, reproducible* piece of work: a repository
fixture plus a structured request.  The scenarios deliberately span the
spectrum the research question cares about -- work that parallelises well,
work that does not, work that conflicts, and work where a single agent is
plainly the right answer.

A scenario may also install a fault: that is how the harness measures what
happens when a provider fails, without which the failure-handling code would
never be exercised by the benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..models.task import Role

ProviderFactory = Callable[[Any], Any]


@dataclass
class Scenario:
    """A reproducible unit of benchmark work."""

    id: str
    name: str
    description: str
    spec: dict[str, Any]
    repo_files: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    provider_factory: ProviderFactory | None = None
    expected_conflicts: bool = False
    expected_failures: bool = False

    def build_repo(self, root: Path) -> Path:
        from .repo_fixture import init_repo

        return init_repo(root, files=dict(self.repo_files))


def _criterion(cid: str, description: str, kind: str, spec: dict) -> dict[str, Any]:
    return {"id": cid, "description": description, "kind": kind, "spec": spec}


def _task(
    task_id: str,
    title: str,
    objective: str,
    role: str,
    *,
    depends_on: list[str] | None = None,
    edits: list[dict[str, Any]] | None = None,
    criteria: list[dict[str, Any]] | None = None,
    anticipated_files: list[str] | None = None,
    duration_s: float = 30.0,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "objective": objective,
        "role": role,
        "depends_on": depends_on or [],
        "edits": edits or [],
        "acceptance_criteria": criteria or [],
        "anticipated_files": anticipated_files or [],
        "estimated_duration_s": duration_s,
    }


# --------------------------------------------------------------------------


def independent_parallel() -> Scenario:
    """Two genuinely independent features: ideal for fan-out."""
    return Scenario(
        id="independent_parallel",
        name="Independent parallel features",
        description=(
            "Two unrelated features that touch different files and have no "
            "dependency between them."
        ),
        spec={
            "objective": "Add two independent features",
            "tasks": [
                _task(
                    "feature-a",
                    "Add the alpha helper",
                    "Create alpha.py exposing an alpha() function",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "alpha.py",
                            "content": '"""Alpha feature."""\n\n\ndef alpha(x: int) -> int:\n    return x * 2\n',
                        }
                    ],
                    criteria=[
                        _criterion("a-exists", "alpha.py exists", "file_exists", {"path": "alpha.py"}),
                        _criterion(
                            "a-symbol",
                            "alpha.py defines alpha",
                            "file_contains",
                            {"path": "alpha.py", "pattern": "def alpha"},
                        ),
                    ],
                    anticipated_files=["alpha.py"],
                ),
                _task(
                    "feature-b",
                    "Add the beta helper",
                    "Create beta.py exposing a beta() function",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "beta.py",
                            "content": '"""Beta feature."""\n\n\ndef beta(x: int) -> int:\n    return x * 3\n',
                        }
                    ],
                    criteria=[
                        _criterion("b-exists", "beta.py exists", "file_exists", {"path": "beta.py"}),
                        _criterion(
                            "b-symbol",
                            "beta.py defines beta",
                            "file_contains",
                            {"path": "beta.py", "pattern": "def beta"},
                        ),
                    ],
                    anticipated_files=["beta.py"],
                ),
            ],
        },
        tags=("parallel", "independent"),
    )


def tightly_coupled() -> Scenario:
    """Tasks that must run in sequence because of a real dependency."""
    return Scenario(
        id="tightly_coupled",
        name="Tightly coupled sequence",
        description=(
            "An implementation task and the test task that depends on it: the "
            "second task cannot start until the first has produced code."
        ),
        spec={
            "objective": "Implement a helper and then test it",
            "tasks": [
                _task(
                    "impl",
                    "Implement gamma",
                    "Create gamma.py with a gamma() function",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "gamma.py",
                            "content": '"""Gamma feature."""\n\n\ndef gamma(x: int) -> int:\n    return x + 1\n',
                        }
                    ],
                    criteria=[
                        _criterion("g-exists", "gamma.py exists", "file_exists", {"path": "gamma.py"}),
                    ],
                    anticipated_files=["gamma.py"],
                ),
                _task(
                    "tests",
                    "Test gamma",
                    "Create tests/test_gamma.py covering gamma()",
                    Role.TEST_ENGINEER.value,
                    depends_on=["impl"],
                    edits=[
                        {
                            "path": "tests/test_gamma.py",
                            "content": (
                                "from gamma import gamma\n\n\n"
                                "def test_gamma():\n    assert gamma(1) == 2\n"
                            ),
                        }
                    ],
                    criteria=[
                        _criterion(
                            "gt-exists",
                            "the gamma test exists",
                            "file_exists",
                            {"path": "tests/test_gamma.py"},
                        ),
                        _criterion(
                            "gt-pass",
                            "the gamma test passes",
                            "pytest_selector",
                            {"selector": "tests/test_gamma.py"},
                        ),
                    ],
                    anticipated_files=["tests/test_gamma.py"],
                ),
            ],
        },
        tags=("sequential", "dependent"),
    )


def multi_file_refactor() -> Scenario:
    """A refactor that spans several files."""
    return Scenario(
        id="multi_file_refactor",
        name="Multi-file refactor",
        description="A refactor touching four files in one task.",
        spec={
            "objective": "Refactor four modules to a shared helper",
            "tasks": [
                _task(
                    "refactor",
                    "Introduce a shared helper",
                    "Rewrite four modules to use a shared helper module",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {"path": "shared.py", "content": '"""Shared helper."""\n\n\ndef double(x: int) -> int:\n    return x * 2\n'},
                        {"path": "alpha.py", "content": 'from shared import double\n\n\ndef alpha(x: int) -> int:\n    return double(x)\n'},
                        {"path": "beta.py", "content": 'from shared import double\n\n\ndef beta(x: int) -> int:\n    return double(x) + 1\n'},
                        {"path": "gamma.py", "content": 'from shared import double\n\n\ndef gamma(x: int) -> int:\n    return double(x) + 2\n'},
                    ],
                    criteria=[
                        _criterion("shared", "shared.py exists", "file_exists", {"path": "shared.py"}),
                        _criterion(
                            "refactor-uses-helper",
                            "alpha.py uses the shared helper",
                            "file_contains",
                            {"path": "alpha.py", "pattern": "from shared import double"},
                        ),
                    ],
                    anticipated_files=["shared.py", "alpha.py", "beta.py", "gamma.py"],
                )
            ],
        },
        tags=("refactor", "single-task"),
    )


def bug_fix_with_regression_test() -> Scenario:
    """A fix plus the regression test that proves it."""
    return Scenario(
        id="bug_fix_regression",
        name="Bug fix with regression test",
        description="A behaviour change plus a test that fails without the fix.",
        spec={
            "objective": "Fix the subtraction semantics and add a regression test",
            "tasks": [
                _task(
                    "fix",
                    "Fix subtract",
                    "Make subtract return a - b (it currently returns b - a)",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "calc.py",
                            "content": (
                                '"""A tiny arithmetic module used as a benchmark target."""\n\n\n'
                                "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
                                "def subtract(a: int, b: int) -> int:\n    return a - b\n"
                            ),
                        }
                    ],
                    criteria=[
                        _criterion(
                            "subtract-fixed",
                            "calc.py subtracts in the right order",
                            "file_contains",
                            {"path": "calc.py", "pattern": "return a - b"},
                        ),
                    ],
                    anticipated_files=["calc.py"],
                ),
                _task(
                    "regression",
                    "Add the regression test",
                    "Add a regression test for subtract()",
                    Role.TEST_ENGINEER.value,
                    depends_on=["fix"],
                    edits=[
                        {
                            "path": "tests/test_subtract.py",
                            "content": (
                                "from calc import subtract\n\n\n"
                                "def test_subtract_order():\n    assert subtract(5, 2) == 3\n"
                            ),
                        }
                    ],
                    criteria=[
                        _criterion(
                            "regression-pass",
                            "the regression test passes",
                            "pytest_selector",
                            {"selector": "tests/test_subtract.py"},
                        ),
                    ],
                    anticipated_files=["tests/test_subtract.py"],
                ),
            ],
        },
        tags=("sequential", "bugfix"),
    )


def integration_conflict() -> Scenario:
    """Two workers that modify the same file: the price of parallelism."""
    return Scenario(
        id="integration_conflict",
        name="Conflicting parallel edits",
        description=(
            "Two independent tasks rewrite the same file. Each passes its own "
            "checks; the combination cannot be applied cleanly."
        ),
        spec={
            "objective": "Two agents rewrite the same file",
            "tasks": [
                _task(
                    "writer-a",
                    "Writer A",
                    "Add a marker for writer A to calc.py",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "calc.py",
                            "content": (
                                '"""A tiny arithmetic module used as a benchmark target."""\n\n\n'
                                "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
                                "def subtract(a: int, b: int) -> int:\n    return a - b\n\n\n"
                                "WRITER_A = 'a'\n"
                            ),
                        }
                    ],
                    criteria=[
                        _criterion("wa", "calc.py exists", "file_exists", {"path": "calc.py"}),
                    ],
                    anticipated_files=["calc.py"],
                ),
                _task(
                    "writer-b",
                    "Writer B",
                    "Add a different marker for writer B to calc.py",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "calc.py",
                            "content": (
                                '"""A tiny arithmetic module used as a benchmark target."""\n\n\n'
                                "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
                                "def subtract(a: int, b: int) -> int:\n    return a - b\n\n\n"
                                "WRITER_B = 'b'\n"
                            ),
                        }
                    ],
                    criteria=[
                        _criterion("wb", "calc.py exists", "file_exists", {"path": "calc.py"}),
                    ],
                    anticipated_files=["calc.py"],
                ),
            ],
        },
        tags=("conflict", "parallel"),
        expected_conflicts=True,
    )


def _failing_provider_factory(provider: Any) -> Any:
    """Make the first provider call fail, to exercise the retry path.

    The failure is *transient*, so the runtime should retry and complete the
    work.  A run that ends with the work undone would indicate that the
    retry policy is not doing its job.
    """
    from ..errors import TransientModelError

    original = provider.complete
    state = {"calls": 0}

    async def complete(request: Any) -> Any:
        state["calls"] += 1
        if state["calls"] == 1:
            raise TransientModelError(
                "injected transient provider failure", provider="mock"
            )
        return await original(request)

    provider.complete = complete  # type: ignore[method-assign]
    return provider


def failing_worker() -> Scenario:
    """A worker whose provider fails, to exercise retry and recovery."""
    return Scenario(
        id="failing_worker",
        name="Failing worker",
        description=(
            "The provider fails for the first attempt of the first task; the "
            "runtime must retry and still complete the work."
        ),
        spec={
            "objective": "Add a feature while the provider is briefly unavailable",
            "tasks": [
                _task(
                    "feature",
                    "Add delta",
                    "Create delta.py exposing a delta() function",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "delta.py",
                            "content": '"""Delta feature."""\n\n\ndef delta(x: int) -> int:\n    return x - 1\n',
                        }
                    ],
                    criteria=[
                        _criterion("d-exists", "delta.py exists", "file_exists", {"path": "delta.py"}),
                    ],
                    anticipated_files=["delta.py"],
                )
            ],
        },
        tags=("failure", "retry"),
        provider_factory=_failing_provider_factory,
        expected_failures=True,
    )


def single_agent_preferred() -> Scenario:
    """A single small task, where parallelism cannot help."""
    return Scenario(
        id="single_agent_preferred",
        name="Single agent is enough",
        description="One small task; coordination overhead cannot pay for itself.",
        spec={
            "objective": "Add a one-line helper",
            "tasks": [
                _task(
                    "tiny",
                    "Add epsilon",
                    "Create epsilon.py exposing an epsilon() function",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": "epsilon.py",
                            "content": '"""Epsilon feature."""\n\n\ndef epsilon(x: int) -> int:\n    return x\n',
                        }
                    ],
                    criteria=[
                        _criterion("e-exists", "epsilon.py exists", "file_exists", {"path": "epsilon.py"}),
                    ],
                    anticipated_files=["epsilon.py"],
                )
            ],
        },
        tags=("single",),
    )


def meaningful_parallel() -> Scenario:
    """Four independent tasks: the case fan-out is designed for."""
    return Scenario(
        id="meaningful_parallel",
        name="Four independent tasks",
        description="Four independent modules that can be built concurrently.",
        spec={
            "objective": "Add four independent modules",
            "tasks": [
                _task(
                    f"mod-{name}",
                    f"Add {name}",
                    f"Create {name}.py exposing {name}()",
                    Role.IMPLEMENTER.value,
                    edits=[
                        {
                            "path": f"{name}.py",
                            "content": f'"""{name} feature."""\n\n\ndef {name}(x: int) -> int:\n    return x\n',
                        }
                    ],
                    criteria=[
                        _criterion(
                            f"{name}-exists",
                            f"{name}.py exists",
                            "file_exists",
                            {"path": f"{name}.py"},
                        ),
                    ],
                    anticipated_files=[f"{name}.py"],
                )
                for name in ("mod_a", "mod_b", "mod_c", "mod_d")
            ],
        },
        tags=("parallel", "wide"),
    )


def build_scenarios() -> list[Scenario]:
    return [
        independent_parallel(),
        tightly_coupled(),
        multi_file_refactor(),
        bug_fix_with_regression_test(),
        integration_conflict(),
        failing_worker(),
        single_agent_preferred(),
        meaningful_parallel(),
    ]


def get_scenario(scenario_id: str) -> Scenario:
    for scenario in build_scenarios():
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(f"unknown scenario: {scenario_id}")
