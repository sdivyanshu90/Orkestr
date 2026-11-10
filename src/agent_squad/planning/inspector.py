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
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

    def __init__(self, root: str | Path, *, max_files: int = 4000) -> None:
        self.root = Path(root).resolve()
        self.max_files = max_files

    def inspect(self, *, max_depth: int = 4) -> RepoContext:
        context = RepoContext(root=str(self.root))
        context.head_revision = self._head_revision()
        count = 0
        for path in sorted(self.root.rglob("*")):
            if count >= self.max_files:
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
            findings = self._scan(path)
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
    def _scan(path: Path) -> list[Any]:
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
