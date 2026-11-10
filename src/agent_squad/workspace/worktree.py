"""Git worktree isolation.

Each coding task gets its own detached git worktree.  Two consequences
matter:

1. Workers never share a live working directory, so concurrent edits cannot
   interleave and corrupt each other's state.
2. The diff of a worktree against the base revision is a self-contained,
   auditable patch artifact.

A git worktree isolates *code*, not *authority*: a process inside the
worktree can still read anything the host user can read.  Isolation of
authority is the job of the capability layer; isolation of resources is the
job of the sandbox.  See ``docs/security-threat-model.md``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import InfrastructureError
from ..ids import worktree_id


class GitError(InfrastructureError):
    """A git command failed."""


def git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the completed process."""
    import os

    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed with exit {proc.returncode}: "
            f"{proc.stderr.strip()[:500]}",
            args=args,
            exit_code=proc.returncode,
        )
    return proc


@dataclass
class Worktree:
    """A single isolated checkout."""

    id: str
    path: Path
    base_revision: str
    repo_path: Path
    task_id: str | None = None

    @property
    def exists(self) -> bool:
        return self.path.exists()


class WorktreeManager:
    """Creates and disposes of isolated worktrees for a repository."""

    def __init__(self, repo_path: str | Path, root: str | Path) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._created: set[str] = set()

    # ------------------------------------------------------------ inspection

    def head_revision(self) -> str:
        proc = git(["rev-parse", "HEAD"], cwd=self.repo_path)
        return proc.stdout.strip()

    def current_branch(self) -> str:
        proc = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_path, check=False)
        return proc.stdout.strip()

    def has_commits(self) -> bool:
        proc = git(["rev-parse", "--verify", "HEAD"], cwd=self.repo_path, check=False)
        return proc.returncode == 0

    def status_porcelain(self) -> str:
        proc = git(["status", "--porcelain"], cwd=self.repo_path, check=False)
        return proc.stdout

    def is_dirty(self) -> bool:
        return bool(self.status_porcelain().strip())

    # -------------------------------------------------------------- creation

    def create(self, task_id: str | None = None, *, base_revision: str | None = None) -> Worktree:
        """Create a detached worktree at the given base revision."""
        if not self.has_commits():
            raise GitError(
                "repository has no commits; a worktree needs at least one commit",
                repo_path=str(self.repo_path),
            )
        base = base_revision or self.head_revision()
        wt_id = worktree_id()
        path = self.root / wt_id
        git(
            ["worktree", "add", "--detach", "--force", str(path), base],
            cwd=self.repo_path,
        )
        self._created.add(wt_id)
        return Worktree(
            id=wt_id,
            path=path,
            base_revision=base,
            repo_path=self.repo_path,
            task_id=task_id,
        )

    def remove(self, worktree: Worktree, *, force: bool = True) -> None:
        """Remove a worktree and prune its administrative files."""
        git(
            ["worktree", "remove", "--force" if force else "--", str(worktree.path)],
            cwd=self.repo_path,
            check=False,
        )
        if worktree.path.exists():
            shutil.rmtree(worktree.path, ignore_errors=True)
        git(["worktree", "prune"], cwd=self.repo_path, check=False)
        self._created.discard(worktree.id)

    def remove_all(self) -> None:
        git(["worktree", "remove", "--force", "--", str(self.root)], cwd=self.repo_path, check=False)
        git(["worktree", "prune"], cwd=self.repo_path, check=False)
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    # ----------------------------------------------------------------- diffs

    def diff(self, worktree: Worktree, *, staged: bool = False) -> str:
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        args.append("--binary")
        proc = git(args, cwd=worktree.path, check=False)
        return proc.stdout

    def changed_files(self, worktree: Worktree) -> list[tuple[str, str]]:
        """Return ``(path, status)`` pairs for files changed in the worktree."""
        proc = git(["status", "--porcelain"], cwd=worktree.path, check=False)
        entries: list[tuple[str, str]] = []
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            status = line[:2].strip() or "M"
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            entries.append((path, status))
        return entries

    def diff_stat(self, worktree: Worktree) -> str:
        proc = git(["diff", "--stat", "--no-color"], cwd=worktree.path, check=False)
        return proc.stdout

    def stage_all(self, worktree: Worktree) -> None:
        """Stage every change in the worktree (including new files)."""
        git(["add", "-A"], cwd=worktree.path)

    def commit_in_worktree(self, worktree: Worktree, message: str) -> str:
        """Commit everything in the worktree and return the new head sha."""
        git(["add", "-A"], cwd=worktree.path)
        git(
            [
                "-c",
                "user.name=agent-squad",
                "-c",
                "user.email=agent-squad@localhost",
                "commit",
                "-m",
                message,
            ],
            cwd=worktree.path,
            check=False,
        )
        proc = git(["rev-parse", "HEAD"], cwd=worktree.path)
        return proc.stdout.strip()

    def list_worktrees(self) -> list[dict[str, str]]:
        proc = git(["worktree", "list", "--porcelain"], cwd=self.repo_path, check=False)
        out: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            if not line.strip():
                if current:
                    out.append(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            out.append(current)
        return out
