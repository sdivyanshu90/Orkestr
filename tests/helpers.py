"""Shared helpers for the test-suite."""

from __future__ import annotations

from typing import Any


def two_independent_spec(*, dependent: bool = False) -> dict[str, Any]:
    """Two tasks that touch different files.

    With ``dependent=True`` the second task depends on the first, so the
    scheduler must serialise them.
    """
    spec: dict[str, Any] = {
        "objective": "Two independent changes",
        "tasks": [
            {
                "id": "one",
                "title": "Add module one",
                "objective": "Create one.py",
                "role": "implementer",
                "depends_on": [],
                "edits": [
                    {"path": "one.py", "content": '"""One."""\n\n\ndef one() -> int:\n    return 1\n'}
                ],
                "acceptance_criteria": [
                    {
                        "id": "one-exists",
                        "description": "one.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "one.py"},
                    }
                ],
            },
            {
                "id": "two",
                "title": "Add module two",
                "objective": "Create two.py",
                "role": "implementer",
                "depends_on": ["one"] if dependent else [],
                "edits": [
                    {"path": "two.py", "content": '"""Two."""\n\n\ndef two() -> int:\n    return 2\n'}
                ],
                "acceptance_criteria": [
                    {
                        "id": "two-exists",
                        "description": "two.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "two.py"},
                    }
                ],
            },
        ],
    }
    return spec
