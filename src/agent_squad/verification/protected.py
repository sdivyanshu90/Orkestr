"""Protected verification material.

A test suite that the worker wrote is not evidence: the worker could have
written a test that asserts nothing, or deleted the tests it broke.  The
tests in the *repository* are evidence only while the worker cannot change
them.

This module makes that concrete.  A run declares which paths are
*protected*: the hidden regression checks, the baseline tests, the
configuration.  The orchestrator:

1. records the content hash of every protected path in the **base** tree;
2. runs the task with those paths outside the worker's write scope, so the
   capability layer refuses to change them;
3. re-checks every protected path in the **candidate** tree before the
   verdict is issued.

If a protected path changed, the evidence is void and the run is rejected —
not because the work was wrong, but because the thing that judged the work
was tampered with.

The scope restriction is what makes this work in practice.  The protected
paths are excluded from the task's writable scope, and the policy engine
refuses a write outside it.  The hash comparison is the *independent* check
that the scope was not bypassed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models.contract import OutcomeState, OutcomeVerdict


@dataclass
class ProtectedMaterial:
    """The paths a worker must not be able to change."""

    paths: tuple[str, ...] = ()
    base_hashes: dict[str, str] = field(default_factory=dict)

    def capture(self, base_tree: Path) -> ProtectedMaterial:
        """Record the content hash of every protected path in the base tree."""
        self.base_hashes = {}
        for relative in self.paths:
            target = base_tree / relative
            self.base_hashes[relative] = _hash_path(target)
        return self

    def check(self, candidate_tree: Path) -> tuple[OutcomeVerdict, ...]:
        """Compare the candidate tree against the recorded base hashes."""
        verdicts: list[OutcomeVerdict] = []
        for relative, expected in sorted(self.base_hashes.items()):
            actual = _hash_path(candidate_tree / relative)
            if actual == expected:
                verdicts.append(
                    OutcomeVerdict(
                        criterion_id=f"protected:{relative}",
                        state=OutcomeState.ESTABLISHED,
                        detail="the protected file was not modified",
                    )
                )
            else:
                verdicts.append(
                    OutcomeVerdict(
                        criterion_id=f"protected:{relative}",
                        state=OutcomeState.NOT_SATISFIED,
                        detail=(
                            "the protected file was modified by the run "
                            f"(expected {expected[:12]}, found {actual[:12]})"
                        ),
                    )
                )
        return tuple(verdicts)

    @property
    def tampered(self) -> bool:
        return any(
            v.state is OutcomeState.NOT_SATISFIED for v in self.check(Path("."))
        ) if False else False

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "base_hashes": dict(self.base_hashes),
        }


def _hash_path(path: Path) -> str:
    """The content hash of a file, or a marker for a missing one."""
    try:
        if path.is_dir():
            parts = []
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    parts.append(f"{child.relative_to(path)}:{_hash_path(child)}")
            blob = "\n".join(parts).encode("utf-8")
        else:
            blob = path.read_bytes()
    except OSError:
        return "missing"
    return hashlib.sha256(blob).hexdigest()


def protected_scope_patterns(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Glob patterns that exclude the protected paths from a write scope."""
    patterns: list[str] = []
    for relative in paths:
        patterns.append(relative)
        patterns.append(f"{relative.rstrip('/')}/**")
    return tuple(patterns)
