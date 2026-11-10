"""Git-native integration of independently produced changes.

Workers never share a working directory, so their changes arrive here as
patches.  Combining patches is a *verification boundary*, not a formality:
patches that each pass their own tests can still fail together, and the
engine's job is to find that out.

The engine:

* refuses patches computed against a different base revision (they are
  stale and applying them would silently mix trees);
* detects overlapping file modifications between patches *before* applying
  them, so the operator gets an explanation rather than a merge conflict
  dump;
* applies patches with ``git apply --3way`` so that non-overlapping changes
  to the same file can still be merged;
* treats any unresolved conflict as a hard failure requiring escalation --
  the engine never "resolves" a conflict by guessing;
* runs the integration test command against the combined tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import RunConfig
from ..models.evidence import CheckRecord
from ..store.events import EventLog, EventType
from ..workspace.worktree import Worktree, WorktreeManager, git
from .patch import ConflictRecord, IntegrationResult, PatchArtifact


@dataclass
class IntegrationEngine:
    """Combines patches and verifies the combined result."""

    config: RunConfig
    worktrees: WorktreeManager
    log: EventLog | None = None

    # ------------------------------------------------------------ analysis

    def detect_overlaps(
        self, patches: list[PatchArtifact]
    ) -> list[tuple[PatchArtifact, PatchArtifact, tuple[str, ...]]]:
        """Pairs of patches that touch the same files."""
        overlaps: list[tuple[PatchArtifact, PatchArtifact, tuple[str, ...]]] = []
        for i, a in enumerate(patches):
            for b in patches[i + 1 :]:
                shared = tuple(sorted(set(a.files) & set(b.files)))
                if shared:
                    overlaps.append((a, b, shared))
        return overlaps

    def detect_stale(
        self,
        patches: list[PatchArtifact],
        base_revision: str,
        dependencies: dict[str, tuple[str, ...]] | None = None,
    ) -> list[PatchArtifact]:
        """Patches whose base content is not the tree they were computed against.

        A patch is *stale* when the tree it was computed against is not the
        tree the orchestrator expects it to apply to.  The expectation
        follows the dependency structure:

        * an independent task's patch was computed against the run's base
          tree;
        * a dependent task's patch was computed against the head its
          dependency produced.

        Trees are compared rather than commit ids because the integration
        engine creates its own commits (with different messages) for the
        same content, so the commit ids differ while the content is
        identical.

        Note that a patch is *not* stale merely because an earlier,
        unrelated patch has since been applied: that is the normal case for
        parallel work, and the patch either applies cleanly or conflicts,
        which are different outcomes.
        """
        dependencies = dependencies or {}
        base_tree = self.worktrees.tree_of(base_revision)
        head_trees: dict[str, str] = {}
        stale: list[PatchArtifact] = []

        for patch in patches:
            expected = base_tree
            deps = dependencies.get(patch.task_id, ())
            for dependency in deps:
                candidate = head_trees.get(dependency)
                if candidate:
                    expected = candidate
                    break
            actual = patch.base_tree or self.worktrees.tree_of(patch.base_revision)
            if actual and expected and actual != expected:
                stale.append(patch)
                continue
            head_trees[patch.task_id] = patch.head_tree or expected
        return stale


    # ---------------------------------------------------------- integration

    async def integrate(
        self,
        patches: list[PatchArtifact],
        *,
        base_revision: str,
        run_id: str = "",
        task_id: str | None = None,
        run_checks: bool = True,
        dependencies: dict[str, tuple[str, ...]] | None = None,
    ) -> IntegrationResult:
        """Apply the patches to a fresh integration worktree and verify them."""
        result = IntegrationResult(passed=False)
        workspace: Worktree | None = None
        try:
            workspace = self.worktrees.create(task_id=task_id, base_revision=base_revision)
        except Exception as exc:
            result.error = f"failed to create integration worktree: {exc}"
            return result

        result.workspace = str(workspace.path)
        if self.log:
            self.log.append(
                EventType.INTEGRATION_ATTEMPTED,
                run_id=run_id,
                task_id=task_id,
                payload={
                    "patches": [p.artifact_id for p in patches],
                    "base_revision": base_revision,
                    "workspace": str(workspace.path),
                },
            )

        try:
            overlaps = self.detect_overlaps(patches)
            for a, b, shared in overlaps:
                result.conflicts.append(
                    ConflictRecord(
                        task_id=b.task_id,
                        artifact_id=b.artifact_id,
                        kind="overlapping_files",
                        files=shared,
                        detail=(
                            f"tasks {a.task_id} and {b.task_id} both modify "
                            + ", ".join(shared)
                        ),
                    )
                )

            current_head = base_revision
            head_trees: dict[str, str] = {}
            base_tree = self.worktrees.tree_of(base_revision)
            for patch in patches:
                expected_tree = base_tree
                for dependency in (dependencies or {}).get(patch.task_id, ()):
                    candidate = head_trees.get(dependency)
                    if candidate:
                        expected_tree = candidate
                        break
                actual_tree = patch.base_tree or self.worktrees.tree_of(patch.base_revision)
                if actual_tree and expected_tree and actual_tree != expected_tree:
                    result.conflicts.append(
                        ConflictRecord(
                            task_id=patch.task_id,
                            artifact_id=patch.artifact_id,
                            kind="stale_patch",
                            files=patch.files,
                            detail=(
                                f"patch was produced against tree {actual_tree[:12]} "
                                f"but the orchestrator expected {expected_tree[:12]}"
                            ),
                        )
                    )
                    result.stale.append(patch.artifact_id)
                    result.rejected.append(
                        {"task_id": patch.task_id, "artifact_id": patch.artifact_id}
                    )
                    continue
                _applied, error, unmerged = self._apply(workspace, patch)
                if error:
                    result.conflicts.append(
                        ConflictRecord(
                            task_id=patch.task_id,
                            artifact_id=patch.artifact_id,
                            kind="apply_failed",
                            files=unmerged or patch.files,
                            detail=error,
                        )
                    )
                    result.rejected.append(
                        {"task_id": patch.task_id, "artifact_id": patch.artifact_id}
                    )
                    continue
                result.applied.append(patch.artifact_id)
                current_head = self.worktrees.commit_in_worktree(
                    workspace, f"agent-squad: integrate {patch.task_id}"
                )
                head_trees[patch.task_id] = self.worktrees.tree_of(current_head)

            if result.conflicts:
                result.error = (
                    f"{len(result.conflicts)} conflict(s) detected during integration"
                )
                self._log_conflicts(run_id, task_id, result)
                return result

            if run_checks:
                record = await self._run_integration_checks(workspace.path)
                result.check_output = record.stdout_excerpt
                if not record.passed:
                    result.error = "integration checks failed"
                    if self.log:
                        self.log.append(
                            EventType.INTEGRATION_ATTEMPTED,
                            run_id=run_id,
                            task_id=task_id,
                            payload={"checks_failed": True, "output": record.stdout_excerpt[-2000:]},
                        )
                    return result

            result.passed = True
            result.commit = self.worktrees.commit_in_worktree(
                workspace, "agent-squad: integrated changes"
            )
            if self.log:
                self.log.append(
                    EventType.INTEGRATION_VERIFIED,
                    run_id=run_id,
                    task_id=task_id,
                    payload={
                        "commit": result.commit,
                        "applied": result.applied,
                        "workspace": str(workspace.path),
                    },
                )
            return result

        finally:
            keep = self.config.workspace.keep_worktrees_on_success
            if not result.passed:
                keep = True
            if not keep:
                self.worktrees.remove(workspace)

    # -------------------------------------------------------------- helpers

    def _apply(
        self, workspace: Worktree, patch: PatchArtifact
    ) -> tuple[bool, str | None, tuple[str, ...]]:
        if not patch.content.strip():
            return False, "patch is empty", ()
        patch_file = workspace.path / f".squad-patch-{patch.artifact_id}.diff"
        patch_file.write_text(patch.content, encoding="utf-8")
        try:
            proc = git(
                ["apply", "--3way", "--whitespace=nowarn", str(patch_file)],
                cwd=workspace.path,
                check=False,
            )
            if proc.returncode == 0:
                return True, None, ()
            unmerged = self._unmerged_files(workspace)
            return False, (proc.stderr or proc.stdout).strip()[:1000], unmerged
        finally:
            patch_file.unlink(missing_ok=True)

    @staticmethod
    def _unmerged_files(workspace: Worktree) -> tuple[str, ...]:
        proc = git(
            ["diff", "--name-only", "--diff-filter=U"], cwd=workspace.path, check=False
        )
        return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())

    async def _run_integration_checks(self, workspace: Path) -> CheckRecord:
        from ..sandbox.limits import Sandbox

        command = list(self.config.verification.integration_test_command)
        sandbox = Sandbox(self.config.sandbox)
        started = __import__("datetime").datetime.now(__import__("datetime").UTC)
        try:
            result = await sandbox.run(
                command,
                cwd=workspace,
                timeout_s=self.config.verification.command_timeout_s,
            )
        except Exception as exc:
            return CheckRecord(
                criterion_id="integration",
                kind="command_exits_zero",
                command=tuple(command),
                exit_code=None,
                error=str(exc),
                deterministic=True,
            )
        return CheckRecord(
            criterion_id="integration",
            kind="command_exits_zero",
            command=tuple(command),
            exit_code=result.exit_code,
            stdout_excerpt=result.stdout[-4000:],
            stderr_excerpt=result.stderr[-4000:],
            duration_s=result.duration_s,
            started_at=started,
            error=None if result.ok else f"exit code {result.exit_code}",
            deterministic=True,
        )

    def _log_conflicts(
        self, run_id: str, task_id: str | None, result: IntegrationResult
    ) -> None:
        if not self.log:
            return
        for conflict in result.conflicts:
            self.log.append(
                EventType.INTEGRATION_CONFLICT,
                run_id=run_id,
                task_id=task_id,
                payload=conflict.to_dict(),
            )

    # ------------------------------------------------------- review bundles

    def build_patch_bundle(
        self, patches: list[PatchArtifact], *, base_revision: str
    ) -> str:
        """Produce a reviewable bundle without committing or publishing.

        The bundle is a plain text document containing the base revision and
        each patch in turn.  Nothing is pushed, committed to the main
        repository, or otherwise made visible outside the local filesystem.
        """
        parts = [
            "# agent-squad patch bundle",
            f"# base revision: {base_revision}",
            f"# patches: {len(patches)}",
            "",
        ]
        for patch in patches:
            parts.append(f"## task {patch.task_id} (artifact {patch.artifact_id})")
            parts.append(f"# sha256: {patch.sha256}")
            parts.append(f"# files: {', '.join(patch.files) or '(none)'}")
            parts.append("")
            parts.append(patch.content)
            parts.append("")
        return "\n".join(parts)
