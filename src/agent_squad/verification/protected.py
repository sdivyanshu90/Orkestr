"""Protected verification material.

A test suite that the worker wrote is not evidence: the worker could have
written a test that asserts nothing, or deleted the tests it broke.  The
tests in the *repository* are evidence only while the worker cannot change
them.

This module makes that concrete.  A run declares which paths are
*protected*: the hidden regression checks, the baseline tests, the
configuration.  The orchestrator:

1. records the content hash of every protected path in the **base** tree;
2. **stages** the material outside the task's worktree, so that it is
   outside the worker's write scope and, when it is hidden material,
   outside its read scope as well;
3. refuses, during execution, any attempt to write, delete or read the
   material;
4. re-checks every protected path in the **candidate** tree before the
   verdict is issued.

The distinction that matters is between step 3 and step 4.  Step 4 alone
is a hash comparison *after* the fact: a worker that rewrites the
material, runs the check and puts the original back leaves no trace for
the hash to find.  Step 3 is what actually stops it, and it has to happen
while the worker is running, because that is the only moment at which the
change can still be prevented.

The scope restriction is what makes this work in practice.  The protected
paths are excluded from the task's writable scope, the policy engine
refuses a write outside it, and the command path refuses a command that
mentions the material at all.  The hash comparison is the *independent*
check that the scope was not bypassed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import posixpath
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models.contract import OutcomeState, OutcomeVerdict
from ..models.task import AcceptanceCriterion, CheckKind

#: The kinds of check whose oracle is a *file* the worker could write.
#:
#: A command that runs the project's own tests is only independent while
#: those tests are protected; a command that runs a file the worker wrote
#: is never independent, because the worker wrote the thing that judges
#: the work.
_FILE_BEARING_CHECKS: frozenset[CheckKind] = frozenset(
    {
        CheckKind.COMMAND_EXITS_ZERO,
        CheckKind.COMMAND_OUTPUT_MATCHES,
        CheckKind.PYTEST_SELECTOR,
    }
)


@dataclass
class ProtectedMaterial:
    """The material a worker must not be able to change -- or to read."""

    paths: tuple[str, ...] = ()
    base_hashes: dict[str, str] = field(default_factory=dict)
    staged_root: Path | None = None
    """Where the material was staged, outside the worker's worktree."""
    staged_hashes: dict[str, str] = field(default_factory=dict)

    def capture(self, base_tree: Path) -> ProtectedMaterial:
        """Record the content hash of every protected path in the base tree."""
        self.base_hashes = {}
        for relative in self.paths:
            target = base_tree / relative
            self.base_hashes[relative] = _hash_path(target)
        return self

    def stage(
        self,
        base_tree: Path,
        destination: Path,
        *,
        remove_from_source: bool = False,
    ) -> ProtectedMaterial:
        """Copy the protected material outside the worker's scope.

        The staged copy is the authority: the verifier reads it from
        outside the worktree, and the worker cannot reach it with the file
        tools, because every path those tools resolve is inside the
        workdir.  That is a *structural* exclusion rather than a promise.

        ``remove_from_source`` takes the material out of the worker's tree
        as well.  That is what "the hidden evaluator material is not
        exposed in the worker's readable task environment" means: the
        worker does not merely lose the ability to change the checks, it
        loses the ability to read them, and with it the ability to write a
        check that satisfies them.
        """
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        self.staged_root = destination
        self.staged_hashes = {}
        for relative in self.paths:
            source = base_tree / relative
            target = destination / relative
            if source.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                continue
            self.staged_hashes[relative] = _hash_path(target)
            if remove_from_source:
                if source.is_dir():
                    shutil.rmtree(source, ignore_errors=True)
                elif source.exists():
                    source.unlink()
        return self

    def check(self, candidate_tree: Path) -> tuple[OutcomeVerdict, ...]:
        """Compare the candidate tree against the recorded base hashes.

        The staged copy is checked as well: it is the copy the verifier
        reads, so a worker that reached it has changed the oracle.
        """
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
        for relative, expected in sorted(self.staged_hashes.items()):
            actual = _hash_path(self.staged_path(relative))
            if actual != expected:
                verdicts.append(
                    OutcomeVerdict(
                        criterion_id=f"protected-staged:{relative}",
                        state=OutcomeState.NOT_SATISFIED,
                        detail=(
                            "the staged verification material was modified by "
                            "the run, so the oracle the verdict relied on is "
                            "not the one that was staged"
                        ),
                    )
                )
        return tuple(verdicts)

    def assert_intact(self) -> tuple[bool, str]:
        """Whether the staged material is still exactly what was staged.

        This is the check that can be made *during* execution -- after
        every tool call, for instance.  A worker that modified the
        material is caught while it is still running, which is the only
        moment at which the change can still be prevented.
        """
        if self.staged_root is None:
            return True, "nothing was staged"
        for relative, expected in sorted(self.staged_hashes.items()):
            actual = _hash_path(self.staged_path(relative))
            if actual != expected:
                return False, (
                    f"the staged verification material {relative!r} was "
                    "modified while the run was in progress"
                )
        return True, "the staged verification material is unchanged"

    def staged_path(self, relative: str) -> Path:
        """Where a protected path lives once it has been staged."""
        if self.staged_root is None:
            return Path(relative)
        return self.staged_root / relative

    def is_protected(self, path: str) -> bool:
        """Whether a path names protected material."""
        candidate = posixpath.normpath(str(path))
        for pattern in self.paths:
            if fnmatch.fnmatch(candidate, pattern) or candidate.startswith(
                pattern.rstrip("/") + "/"
            ):
                return True
        return False

    def scope_patterns(self) -> tuple[str, ...]:
        """The patterns that keep the material out of the worker's scope."""
        return protected_scope_patterns(self.paths)

    def missing_oracle_paths(
        self, criteria: tuple[AcceptanceCriterion, ...]
    ) -> tuple[str, ...]:
        """The files a check depends on that are *not* protected.

        A required outcome whose check runs a file the worker can write is
        not an independent oracle: the worker would be supplying the only
        judge of its own change.  The files such a check depends on have
        to be protected for the check to mean anything, and this is what
        says which ones are not.
        """
        wanted: list[str] = []
        for criterion in criteria:
            if criterion.kind not in _FILE_BEARING_CHECKS:
                continue
            for path in _command_paths(criterion):
                if not self.is_protected(path):
                    wanted.append(path)
        return tuple(dict.fromkeys(wanted))

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "base_hashes": dict(self.base_hashes),
            "staged_root": str(self.staged_root) if self.staged_root else "",
            "staged_hashes": dict(self.staged_hashes),
        }


def _command_paths(criterion: AcceptanceCriterion) -> tuple[str, ...]:
    """The files a command check depends on.

    A ``pytest_selector`` names a file directly, so it is considered before
    the command list: a selector-only criterion has a command of its own
    that the runtime synthesises later, and returning early on the missing
    ``command`` key would silently declare the oracle independent.
    """
    paths: list[str] = []
    selector = criterion.spec.get("selector")
    if isinstance(selector, str) and selector and ("/" in selector or selector.endswith(".py")):
        paths.append(selector)
    command = criterion.spec.get("command")
    if not isinstance(command, (list, tuple)):
        return tuple(paths)
    for part in command:
        token = str(part)
        if token.startswith("-"):
            continue
        if "/" in token or token.endswith(".py"):
            paths.append(token)
    return tuple(dict.fromkeys(paths))


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
