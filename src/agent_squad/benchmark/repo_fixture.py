"""Deterministic repository and request fixtures.

These ship with the package because the benchmark harness must be able to
create the same repository on any machine, in any environment, without
network access.  The fixtures are small on purpose: the interesting
behaviour of the system (isolation, ordering, integration, verification) is
independent of repository size.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

CALC_SOURCE = '''"""A tiny arithmetic module used as a benchmark target."""


def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b
'''

ROOT_CONFTEST = '''import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
'''

BASIC_TEST = '''from calc import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 2) == 3
'''

ROUNDING_SOURCE = '''"""Rounding helpers."""


def round_half_up(value: float, digits: int = 0) -> float:
    """Round half away from zero (not banker's rounding)."""
    factor = 10**digits
    shifted = value * factor
    if shifted >= 0:
        rounded = int(shifted + 0.5)
    else:
        rounded = -int(-shifted + 0.5)
    return rounded / factor
'''

ROUNDING_TEST = '''import pytest

from calc import round_half_up


def test_round_half_up_basic():
    assert round_half_up(2.5) == 3.0
    assert round_half_up(3.14159, 2) == 3.14


def test_round_half_up_negative():
    assert round_half_up(-2.5) == -3.0


@pytest.mark.parametrize(
    "value,expected", [(0.0, 0.0), (0.125, 0.13), (2.5, 3.0)]
)
def test_round_half_up_values(value, expected):
    assert round_half_up(value, 2 if value != 2.5 else 0) == expected
'''


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return proc.stdout.strip()


def init_repo(path: Path, *, files: dict[str, str] | None = None) -> Path:
    """Create a small git repository with a working test suite."""
    path.mkdir(parents=True, exist_ok=True)
    contents = {
        "calc.py": CALC_SOURCE,
        "conftest.py": ROOT_CONFTEST,
        "tests/test_calc.py": BASIC_TEST,
        ".gitignore": "__pycache__/\n*.pyc\n",
    }
    if files:
        contents.update(files)
    for name, content in contents.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "agent-squad")
    git(path, "config", "user.email", "agent-squad@localhost")
    git(path, "add", "-A")
    git(path, "commit", "-m", "initial commit")
    return path


def sample_spec() -> dict:
    """A two-task request: implement a helper, then test it.

    The two tasks have a real dependency (the test task needs the
    implementation), which exercises ordered execution, stacked worktrees and
    ordered patch integration.
    """
    return {
        "objective": "Add a round_half_up helper and test it",
        "description": "Introduce round_half_up in calc.py and cover it with tests.",
        "tasks": [
            {
                "id": "impl",
                "title": "Implement round_half_up",
                "objective": "Add a round_half_up(value, digits=0) helper to calc.py",
                "description": "Rounding must be half-away-from-zero, not banker's.",
                "role": "implementer",
                "depends_on": [],
                "priority": 80,
                "complexity": "low",
                "estimated_duration_s": 30,
                "include_paths": ["calc.py"],
                "anticipated_files": ["calc.py"],
                "edits": [
                    {"path": "calc.py", "content": CALC_SOURCE + "\n\n" + ROUNDING_SOURCE}
                ],
                "acceptance_criteria": [
                    {
                        "id": "impl-file",
                        "description": "calc.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "calc.py"},
                    },
                    {
                        "id": "impl-symbol",
                        "description": "calc.py defines round_half_up",
                        "kind": "file_contains",
                        "spec": {"path": "calc.py", "pattern": "def round_half_up"},
                    },
                    {
                        "id": "impl-changed",
                        "description": "the implementation changed the file",
                        "kind": "diff_nonempty",
                        "spec": {},
                    },
                ],
            },
            {
                "id": "tests",
                "title": "Test round_half_up",
                "objective": "Add unit tests covering round_half_up",
                "role": "test_engineer",
                "depends_on": ["impl"],
                "priority": 60,
                "complexity": "low",
                "estimated_duration_s": 30,
                "include_paths": ["tests/"],
                "anticipated_files": ["tests/test_rounding.py"],
                "edits": [
                    {"path": "tests/test_rounding.py", "content": ROUNDING_TEST}
                ],
                "acceptance_criteria": [
                    {
                        "id": "tests-file",
                        "description": "the rounding test module exists",
                        "kind": "file_exists",
                        "spec": {"path": "tests/test_rounding.py"},
                    },
                    {
                        "id": "tests-pass",
                        "description": "the rounding tests pass",
                        "kind": "pytest_selector",
                        "spec": {"selector": "tests/test_rounding.py"},
                    },
                ],
            },
        ],
    }


def conflicting_spec() -> dict:
    """Two independent tasks that both modify the same file.

    Used to exercise the integration conflict detector.
    """
    return {
        "objective": "Two agents edit the same file",
        "tasks": [
            {
                "id": "writer-a",
                "title": "Writer A",
                "objective": "add a marker for writer A",
                "role": "implementer",
                "depends_on": [],
                "anticipated_files": ["calc.py"],
                "edits": [
                    {"path": "calc.py", "content": CALC_SOURCE + "\n\nWRITER_A = 'a'\n"}
                ],
                "acceptance_criteria": [
                    {
                        "id": "a-file",
                        "description": "calc.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "calc.py"},
                    }
                ],
            },
            {
                "id": "writer-b",
                "title": "Writer B",
                "objective": "add a different marker for writer B",
                "role": "implementer",
                "depends_on": [],
                "anticipated_files": ["calc.py"],
                "edits": [
                    {"path": "calc.py", "content": CALC_SOURCE + "\n\nWRITER_B = 'b'\n"}
                ],
                "acceptance_criteria": [
                    {
                        "id": "b-file",
                        "description": "calc.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "calc.py"},
                    }
                ],
            },
        ],
    }
