"""Repository inspection.

The inspector reads the repository *read-only* and produces a compact
context object.  It is deliberately shallow: it reports structure, not
content.  Fetching whole files into the orchestrator's memory is how context
explosion starts, and the orchestrator's job is to keep context small and
deliberate.

Safety notes:

* The inspector never executes anything from the repository.
* Paths discovered in the repository are treated as untrusted data.
* Binary and oversized files are skipped rather than truncated silently.

The inspection is a *phase* of the run, and it happens before the scheduler
exists, so it is the phase that most easily escapes a run's absolute
deadline.  The scan therefore runs in a daemon thread which is joined with
the run's remaining budget: a repository on a filesystem that has stopped
responding cannot keep the run past its deadline, and a cancellation is
observed while the scan is still running.
"""

from __future__ import annotations

import fnmatch
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import CancelledError as RunCancelledError
from ..runtime.deadline import (
    DeadlineExceeded,
    Phase,
    RunDeadline,
    RunScope,
    current_run_scope,
)
from ..security.injection import InjectionCategory, scan_for_injection

DEFAULT_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
        ".idea",
        ".vscode",
        "target",
        ".eggs",
    }
)

LANGUAGE_SUFFIXES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".sh": "shell",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
}

TEST_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.js",
    "*.spec.ts",
    "conftest.py",
)


@dataclass
class RepoFile:
    path: str
    size: int
    language: str | None = None


@dataclass
class RepoContext:
    """A compact, structured view of the repository."""

    root: str
    head_revision: str = ""
    files: list[RepoFile] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    test_files: list[str] = field(default_factory=list)
    manifest_files: list[str] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    injection_findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "head_revision": self.head_revision,
            "file_count": len(self.files),
            "languages": self.languages,
            "test_files": self.test_files,
            "manifest_files": self.manifest_files,
            "directories": self.directories,
            "injection_findings": self.injection_findings,
        }

    def summary_text(self, limit: int = 120) -> str:
        """A short, human-readable summary suitable for a prompt."""
        lines = [
            f"repository root: {self.root}",
            f"head revision: {self.head_revision[:12]}",
            f"files: {len(self.files)}",
        ]
        if self.languages:
            top = sorted(self.languages.items(), key=lambda kv: -kv[1])[:6]
            lines.append("languages: " + ", ".join(f"{lang}({n})" for lang, n in top))
        if self.test_files:
            lines.append("tests: " + ", ".join(self.test_files[:10]))
        if self.manifest_files:
            lines.append("manifests: " + ", ".join(self.manifest_files[:6]))
        if self.directories:
            lines.append("top-level dirs: " + ", ".join(self.directories[:12]))
        return "\n".join(lines)


class RepositoryInspector:
    """Read-only repository survey."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_files: int = 4000,
        deadline: RunDeadline | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_files = max_files
        self._deadline = deadline

    @property
    def deadline(self) -> RunDeadline | None:
        """The run's absolute deadline, explicit or ambient."""
        return self._deadline or current_run_scope().deadline

    def inspect(self, *, max_depth: int = 4) -> RepoContext:
        """Survey the repository.

        The scan is the blocking part of the phase, so it is executed in a
        daemon thread that is joined with the run's remaining budget.  A
        scan that cannot finish therefore delays the phase by at most the
        remaining budget: a repository on a filesystem that has stopped
        responding cannot keep the run past its deadline.
        """
        deadline = self.deadline
        scope = current_run_scope()
        # Fail fast: a survey that starts after the expiry would only
        # produce a context nobody has time to act on.
        if deadline is not None and deadline.expired():
            deadline.observe(Phase.INSPECTION.value)
            return RepoContext(root=str(self.root))
        try:
            return self._scan_bounded(max_depth, deadline=deadline, scope=scope)
        except (DeadlineExceeded, RunCancelledError):
            # The phase could not finish.  The run cannot proceed anyway --
            # the budget is gone -- so the phase reports an empty context
            # and lets the run stop, rather than letting the failure escape
            # the run as an exception the caller never asked for.
            return RepoContext(root=str(self.root))

    def _scan_bounded(
        self, max_depth: int, *, deadline: RunDeadline | None, scope: RunScope
    ) -> RepoContext:
        """Run the scan off the event loop and wait for it within budget."""
        holder: dict[str, RepoContext] = {}
        failure: list[BaseException] = []

        def work() -> None:
            try:
                holder["context"] = self._scan(max_depth, deadline=deadline, scope=scope)
            except BaseException as exc:
                # The failure is reported to the caller rather than
                # swallowed: a survey that could not be completed must not
                # look like an empty repository.
                failure.append(exc)

        thread = threading.Thread(target=work, name="squad-inspect", daemon=True)
        thread.start()
        cancellation = scope.cancellation
        while thread.is_alive():
            budget = deadline.remaining() if deadline is not None else None
            if budget is not None and budget <= 0:
                break
            thread.join(timeout=0.05)
            if cancellation is not None and cancellation.is_set():
                raise RunCancelledError(
                    "the inspection phase was cancelled", phase=Phase.INSPECTION.value
                )
        if thread.is_alive():
            assert deadline is not None
            deadline.observe(Phase.INSPECTION.value)
            raise DeadlineExceeded(
                "the repository inspection did not finish within the run's "
                f"deadline: {deadline.summary()}",
                phase=Phase.INSPECTION.value,
            )
        if failure:
            raise failure[0]
        return holder["context"]

    def _scan(
        self, max_depth: int, *, deadline: RunDeadline | None, scope: RunScope
    ) -> RepoContext:
        context = RepoContext(root=str(self.root))
        # The seam is inside the scan thread on purpose: a test that blocks
        # the inspection must not be able to freeze the event loop as well.
        scope.enter_sync(Phase.INSPECTION.value)
        context.head_revision = self._head_revision()
        count = 0
        for path in sorted(self.root.rglob("*")):
            if count >= self.max_files:
                break
            if deadline is not None and count % 64 == 0 and deadline.expired():
                # The scan stops rather than starting more work it cannot
                # finish; what it has already seen is still reported.
                deadline.observe(Phase.INSPECTION.value)
                break
            if not self._include(path):
                continue
            rel = path.relative_to(self.root).as_posix()
            if path.is_dir():
                if rel.count("/") <= max_depth:
                    context.directories.append(rel)
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            suffix = path.suffix.lower()
            language = LANGUAGE_SUFFIXES.get(suffix)
            context.files.append(RepoFile(path=rel, size=size, language=language))
            count += 1
            if language:
                context.languages[language] = context.languages.get(language, 0) + 1
            if any(fnmatch.fnmatch(rel, pat) for pat in TEST_PATTERNS):
                context.test_files.append(rel)
            if path.name in (
                "pyproject.toml",
                "package.json",
                "go.mod",
                "Cargo.toml",
                "requirements.txt",
                "setup.py",
                "Makefile",
                "pom.xml",
                "build.gradle",
            ):
                context.manifest_files.append(rel)
            findings = self._scan_file(path)
            for finding in findings:
                context.injection_findings.append(
                    {"path": rel, **finding.to_dict(), "source": "repository_file"}
                )
        context.directories = sorted(set(context.directories))
        context.test_files = sorted(set(context.test_files))
        context.manifest_files = sorted(set(context.manifest_files))
        return context

    def _include(self, path: Path) -> bool:
        if any(part in DEFAULT_IGNORE_DIRS for part in path.parts):
            return False
        # Symlinks can point anywhere; the inspector does not follow them.
        return not path.is_symlink()

    def _head_revision(self) -> str:
        from ..workspace.worktree import git

        proc = git(["rev-parse", "HEAD"], cwd=self.root, check=False)
        return proc.stdout.strip()

    @staticmethod
    def _scan_file(path: Path) -> list[Any]:
        try:
            if path.stat().st_size > 200_000:
                return []
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        findings = scan_for_injection(text, max_findings=5)
        return [
            f
            for f in findings
            if f.category
            in (
                InjectionCategory.INSTRUCTION_OVERRIDE,
                InjectionCategory.ROLE_ESCALATION,
                InjectionCategory.DATA_EXFILTRATION,
            )
        ]


_SEMVER = re.compile(r"\d+\.\d+")
