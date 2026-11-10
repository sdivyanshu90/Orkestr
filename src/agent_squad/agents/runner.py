"""Worker runner: the unit that actually executes one task.

The runner owns the lifecycle of a single attempt:

1. create an isolated git worktree at the task's base revision;
2. build the tool context and the prompt from the task brief;
3. run the bounded agent loop;
4. capture the resulting diff as a patch artifact with a content hash;
5. build a proof-carrying evidence package describing what was changed and
   what was actually observed;
6. publish the artifacts and report the outcome.

The runner never decides that a task "passed".  It reports what happened; the
verification gate decides what that means.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..artifacts.store import ArtifactStore
from ..config import RunConfig
from ..models.artifact import ArtifactKind
from ..models.evidence import ChangedFile, EvidencePackage, Limitation
from ..models.message import Message, MessageKind, make_message
from ..models.task import ADVISORY_CHECKS, Role, TaskSpec
from ..planning.brief import TaskBrief
from ..security.capabilities import capabilities_for_role
from ..store.events import EventLog, EventType
from ..store.state import StateStore
from ..tools.base import ToolContext, ToolRegistry
from ..tools.exec import GitDiffTool, GitStatusTool, RunCommandTool
from ..tools.fs import ListDirTool, ReadFileTool, WriteFileTool
from ..workspace.worktree import Worktree, WorktreeManager, git
from .base import AgentLoop, WorkerResult, build_provenance
from .messaging import MessageBus


@dataclass
class WorkerRunner:
    """Executes tasks in isolated worktrees."""

    config: RunConfig
    state: StateStore
    log: EventLog
    artifacts: ArtifactStore
    worktrees: WorktreeManager
    bus: MessageBus | None = None
    provider: Any = None
    supervisor: str = "supervisor"
    keep_worktrees: bool = False
    policy: Any = None
    approver: Any = None
    budget: Any = None

    # ------------------------------------------------------------- registry

    def build_tools(self, role: Role) -> ToolRegistry:
        """Tools available to a role.

        The reviewer is read-only by construction: it never receives the
        write tool, so a misbehaving reviewer cannot modify the tree.
        """
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(ListDirTool())
        registry.register(RunCommandTool())
        registry.register(GitDiffTool())
        registry.register(GitStatusTool())
        if role in (Role.IMPLEMENTER, Role.TEST_ENGINEER):
            registry.register(WriteFileTool())
        return registry

    # -------------------------------------------------------------- execute

    async def execute(
        self,
        task: TaskSpec,
        brief: TaskBrief,
        *,
        attempt: int,
        cancellation: Any = None,
        base_revision: str | None = None,
    ) -> WorkerResult:
        started = time.monotonic()
        worktree: Worktree | None = None
        worker_identity = f"{task.role.value}:{task.id}"
        if self.bus is not None:
            # The worker's identity is derived from its task, so it is
            # registered when the worker starts rather than enumerated up
            # front.  This happens before any message is published.
            self.bus.register_actor(worker_identity)
        try:
            worktree = self.worktrees.create(task.id, base_revision=base_revision)
        except Exception as exc:
            return WorkerResult(
                task_id=task.id,
                attempt=attempt,
                ok=False,
                error=f"failed to create worktree: {exc}",
                error_class="infrastructure",
                duration_s=time.monotonic() - started,
            )

        self.log.append(
            EventType.AGENT_STARTED,
            run_id=task.run_id,
            task_id=task.id,
            attempt=attempt,
            actor=task.role.value,
            payload={
                "worktree": str(worktree.path),
                "base_revision": worktree.base_revision,
                "role": task.role.value,
            },
        )

        await self._publish(
            MessageKind.PROGRESS,
            task=task,
            attempt=attempt,
            stage="worktree_created",
            percent=5.0,
            recipient=self.supervisor,
        )

        capabilities = capabilities_for_role(task.role)
        if self.budget is not None:
            self.budget.set_scope_limits(
                f"task:{task.id}",
                token_limit=task.budget.max_tokens,
                cost_limit_usd=task.budget.max_cost_usd,
            )
        tool_context = ToolContext(
            run_id=task.run_id,
            task_id=task.id,
            actor=f"{task.role.value}:{task.id}",
            workdir=worktree.path,
            capabilities=capabilities,
            sandbox=self.config.sandbox,
            scope_patterns=task.scope.exclude_paths,
            scope_root=worktree.path,
        )

        prompt = self._build_prompt(task, brief, worktree)
        tools = self.build_tools(task.role)

        loop = AgentLoop(
            provider=self.provider,
            tools=tools,
            config=self.config,
            bus=self.bus,
            run_id=task.run_id,
            task_id=task.id,
            actor=task.role.value,
            max_iterations=self._max_iterations(task.role),
            max_turn_tokens=self._turn_tokens(task.role),
            max_task_tokens=task.budget.max_tokens,
            cost_limit_usd=task.budget.max_cost_usd,
            on_model_call=self._model_call_hook(task, attempt),
            on_tool_call=self._tool_hook(task, attempt),
            on_usage=self._usage_hook(task),
            policy=self.policy,
            approver=self.approver,
            on_policy_decision=self._policy_hook(task, attempt),
            budget=self.budget,
            budget_scope=f"task:{task.id}",
        )

        try:
            result = await loop.run(
                prompt=prompt,
                tool_context=tool_context,
                capabilities=capabilities,
                cancellation=cancellation,
            )
        except Exception as exc:  # pragma: no cover - defensive
            result = WorkerResult(
                task_id=task.id,
                attempt=attempt,
                ok=False,
                error=str(exc),
                error_class="unknown",
            )
        result.attempt = attempt

        # ---- capture the change ----------------------------------------
        try:
            diff_text, changed = self._capture_diff(worktree)
        except Exception as exc:
            diff_text, changed = "", []
            result.ok = False
            result.error = result.error or f"failed to capture patch: {exc}"
            result.error_class = result.error_class or "tool"

        result.changed_files = changed

        # Commit the worktree so that dependent tasks can be based on it.
        # The patch artifact captured above is relative to the *base* of this
        # worktree, so integration can replay the patches in dependency order.
        head_revision = ""
        head_tree = ""
        if changed:
            try:
                head_revision = self.worktrees.commit_in_worktree(
                    worktree, f"agent-squad: {task.title}"
                )
                head_tree = self.worktrees.tree_of(head_revision)
                result.head_revision = head_revision
            except Exception:
                result.head_revision = None

        patch_artifact = None
        if diff_text.strip():
            patch_artifact = self._store_patch(
                task, attempt, result, worktree, diff_text, changed, head_revision, head_tree
            )

        evidence = self._build_evidence(task, brief, attempt, result, patch_artifact)
        result.evidence = evidence

        if patch_artifact is not None:
            self.state.save_artifact(patch_artifact)
            self.log.append(
                EventType.PATCH_CAPTURED,
                run_id=task.run_id,
                task_id=task.id,
                attempt=attempt,
                actor=task.role.value,
                payload={
                    "artifact_id": patch_artifact.id,
                    "sha256": patch_artifact.sha256,
                    "base_revision": worktree.base_revision,
                    "files": [c.path for c in changed],
                },
            )
            await self._publish(
                MessageKind.ARTIFACT_PUBLISHED,
                task=task,
                attempt=attempt,
                recipient=self.supervisor,
                artifact_id=patch_artifact.id,
                artifact_kind=patch_artifact.kind.value,
                sha256=patch_artifact.sha256,
            )

        if result.ok:
            await self._publish(
                MessageKind.HANDOFF,
                task=task,
                attempt=attempt,
                recipient=self.supervisor,
                summary=result.summary[:4000] or f"{task.title} completed",
                artifact_ids=tuple(
                    a.id for a in [patch_artifact] if a is not None
                ),
                open_questions=tuple(
                    limitation.description for limitation in evidence.limitations
                ),
            )
        else:
            await self._publish(
                MessageKind.FAILURE_REPORT,
                task=task,
                attempt=attempt,
                recipient=self.supervisor,
                error_class=result.error_class or "unknown",
                message=result.error or "the attempt failed",
                retryable=result.error_class not in ("policy", "validation", "budget"),
                attempt_number=attempt,
            )
            # A failure that the runtime will not retry is escalated rather
            # than left in the log: it needs a human or a supervisor's
            # judgement, not another attempt.
            if result.error_class in ("policy", "budget"):
                await self._publish(
                    MessageKind.ESCALATION,
                    task=task,
                    attempt=attempt,
                    recipient=self.supervisor,
                    reason=result.error or "the attempt failed",
                    severity="high" if result.error_class == "policy" else "medium",
                    details=(
                        f"the {task.role.value} could not complete {task.id!r}: "
                        f"{result.error}"
                    ),
                )

        self.log.append(
            EventType.AGENT_FINISHED,
            run_id=task.run_id,
            task_id=task.id,
            attempt=attempt,
            actor=task.role.value,
            payload={
                "ok": result.ok,
                "error": result.error,
                "error_class": result.error_class,
                "tokens": result.tokens_used,
                "cost_usd": result.cost_usd,
                "tool_calls": len(result.tool_calls),
            },
        )

        self._cleanup(task, worktree, result)
        result.duration_s = time.monotonic() - started
        result.worktree_path = str(worktree.path)
        result.base_revision = worktree.base_revision
        return result

    # --------------------------------------------------------------- prompt

    def _build_prompt(self, task: TaskSpec, brief: TaskBrief, worktree: Worktree) -> str:
        parts = [
            f"Working directory: {worktree.path}",
            f"Base revision: {worktree.base_revision}",
            "",
            f"Task: {task.title}",
            f"Objective: {task.objective}",
        ]
        if task.description:
            parts.append(f"Description: {task.description}")
        if task.depends_on:
            parts.append(
                "Prerequisites completed by other agents: " + ", ".join(task.depends_on)
            )
        if task.acceptance_criteria:
            parts.append("Acceptance criteria you are responsible for:")
            for criterion in task.acceptance_criteria:
                parts.append(f"  - ({criterion.id}) {criterion.description}")
        parts.append("")
        parts.append("Task brief (authoritative):")
        parts.append(brief.to_prompt_block())
        parts.append("")
        parts.append(
            "When you are done, stop calling tools and state which acceptance "
            "criteria you verified and which you did not."
        )
        return "\n".join(parts)

    # -------------------------------------------------------------- capture

    def _capture_diff(self, worktree: Worktree) -> tuple[str, list[Any]]:
        """Capture the raw diff and the changed-file list of a worktree.

        The changes are staged first: ``git diff`` alone does not report
        untracked files, and a worker that creates a new module would
        otherwise produce an empty patch.
        """
        self.worktrees.stage_all(worktree)
        diff = self.worktrees.diff(worktree, staged=True)
        entries = self.worktrees.changed_files(worktree)
        changed: list[Any] = []
        for path, status in entries:
            changed.append(ChangedFile(path=path, status=_normalise_status(status)))
        return diff, changed

    def _store_patch(
        self,
        task: TaskSpec,
        attempt: int,
        result: WorkerResult,
        worktree: Worktree,
        diff: str,
        changed: list[Any],
        head_revision: str,
        head_tree: str,
    ) -> Any:
        base_tree = self.worktrees.tree_of(worktree.base_revision)
        provenance = build_provenance(
            run_id=task.run_id,
            task_id=task.id,
            attempt=attempt,
            producer=f"{task.role.value}:{task.id}",
            base_revision=worktree.base_revision,
            tool_calls=tuple(result.tool_calls),
        )
        return self.artifacts.put_text(
            diff,
            kind=ArtifactKind.PATCH,
            provenance=provenance,
            metadata={
                "base_revision": worktree.base_revision,
                "base_tree": base_tree,
                "head_revision": head_revision,
                "head_tree": head_tree,
                "worktree": str(worktree.path),
                "task_id": task.id,
                "files": [c.path for c in changed],
            },
            media_type="text/x-diff",
        )

    def _build_evidence(
        self,
        task: TaskSpec,
        brief: TaskBrief,
        attempt: int,
        result: WorkerResult,
        patch_artifact: Any,
    ) -> EvidencePackage:
        """Assemble the evidence package for this attempt.

        The package records *what the worker observed*.  It does not assert
        that acceptance criteria passed; the verification gate fills that in
        by running the checks independently.
        """
        criteria_addressed = tuple(c.id for c in task.acceptance_criteria)
        limitations = []
        if result.error:
            limitations.append(
                Limitation(description=f"attempt reported an error: {result.error}")
            )
        if not result.changed_files and task.role in (Role.IMPLEMENTER, Role.TEST_ENGINEER):
            limitations.append(
                Limitation(description="the attempt produced no file changes")
            )
        unverified = [
            c.id for c in task.acceptance_criteria if c.kind in ADVISORY_CHECKS
        ]
        if unverified:
            limitations.append(
                Limitation(
                    description=(
                        "criteria "
                        + ", ".join(unverified)
                        + " are advisory only and were not independently verified"
                    )
                )
            )
        return EvidencePackage(
            run_id=task.run_id,
            task_id=task.id,
            attempt=attempt,
            producer=f"{task.role.value}:{task.id}",
            changed_files=tuple(result.changed_files),
            patch_artifact_id=patch_artifact.id if patch_artifact else None,
            patch_sha256=patch_artifact.sha256 if patch_artifact else None,
            base_revision=patch_artifact.provenance.base_revision if patch_artifact else None,
            criteria_addressed=criteria_addressed,
            checks=(),
            limitations=tuple(limitations),
            residual_risks=tuple(result.summary.splitlines()[:3]) if result.summary else (),
            notes=result.summary[:2000],
        )

    # -------------------------------------------------------------- cleanup

    def _cleanup(self, task: TaskSpec, worktree: Worktree, result: WorkerResult) -> None:
        """Hook point for worktree disposal.

        The worktree is deliberately *not* removed here: the verification
        gate still has to run the acceptance criteria inside it.  The
        scheduler disposes of it once verification is complete.
        """
        return None

    def cleanup_worktree(self, worktree_path: str | Path, *, keep: bool = False) -> None:
        """Remove a worktree by path (used by the scheduler after verification)."""
        if keep:
            return
        path = Path(worktree_path)
        git(
            ["worktree", "remove", "--force", "--", str(path)],
            cwd=self.worktrees.repo_path,
            check=False,
        )
        if path.exists():
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        git(["worktree", "prune"], cwd=self.worktrees.repo_path, check=False)

    # ---------------------------------------------------------------- hooks

    def _max_iterations(self, role: Role) -> int:
        role_config = self.config.roles.get(role.value)
        return role_config.max_iterations if role_config else 12

    def _turn_tokens(self, role: Role) -> int:
        role_config = self.config.roles.get(role.value)
        return role_config.max_turn_tokens if role_config else 60_000

    def _model_call_hook(self, task: TaskSpec, attempt: int):
        async def hook(**kwargs: Any) -> None:
            self.log.append(
                EventType.MODEL_CALL,
                run_id=task.run_id,
                task_id=task.id,
                attempt=attempt,
                actor=task.role.value,
                payload={
                    "provider": kwargs.get("provider"),
                    "model": kwargs.get("model"),
                    "input_tokens": kwargs.get("usage").input_tokens,
                    "output_tokens": kwargs.get("usage").output_tokens,
                    "estimated": kwargs.get("usage").estimated,
                    "latency_s": round(float(kwargs.get("latency_s") or 0.0), 6),
                    "iteration": kwargs.get("iteration"),
                },
            )

        return hook

    def _tool_hook(self, task: TaskSpec, attempt: int):
        async def hook(**kwargs: Any) -> None:
            self.log.append(
                EventType.TOOL_INVOKED,
                run_id=task.run_id,
                task_id=task.id,
                attempt=attempt,
                actor=task.role.value,
                payload={
                    "tool": kwargs.get("tool"),
                    "ok": kwargs.get("ok"),
                    "error": kwargs.get("error"),
                },
            )

        return hook

    def _usage_hook(self, task: TaskSpec):
        async def hook(**kwargs: Any) -> None:
            tokens = int(kwargs.get("tokens") or 0)
            cost = float(kwargs.get("cost_usd") or 0.0)
            self.state.update_usage(task.id, tokens=tokens, cost_usd=cost)
            if self.budget is not None:
                usage = kwargs.get("usage")
                if usage is None:
                    from ..providers.base import Usage

                    usage = Usage(input_tokens=0, output_tokens=tokens, estimated=True)
                self.budget.record(f"task:{task.id}", usage)
                self.budget.record("run", usage)

        return hook

    def _policy_hook(self, task: TaskSpec, attempt: int):
        async def hook(**kwargs: Any) -> None:
            decision = str(kwargs.get("decision") or "")
            if decision == "allow":
                return
            self.log.append(
                EventType.POLICY_DENIED,
                run_id=task.run_id,
                task_id=task.id,
                attempt=attempt,
                actor=task.role.value,
                payload={
                    "tool": kwargs.get("tool"),
                    "decision": decision,
                    "reason": kwargs.get("reason"),
                },
            )

        return hook

    async def _publish(
        self,
        kind: MessageKind,
        *,
        task: TaskSpec,
        attempt: int,
        recipient: str,
        **payload: Any,
    ) -> Message | None:
        """Publish a typed message to the bus.

        The message is data, not an instruction: the payload is validated
        against the schema for its kind, and a message that fails validation
        is recorded as a failure rather than being smuggled through.
        """
        if self.bus is None:
            return None
        try:
            return await self.bus.publish(
                make_message(
                    kind,
                    run_id=task.run_id,
                    sender=f"{task.role.value}:{task.id}",
                    recipient=recipient,
                    task_id=task.id,
                    correlation_id=f"{task.id}#{attempt}",
                    **payload,
                )
            )
        except Exception as exc:
            # A message failure must not fail the work; it is recorded.
            self.log.append(
                EventType.MESSAGE_SENT,
                run_id=task.run_id,
                task_id=task.id,
                attempt=attempt,
                actor=task.role.value,
                payload={
                    "kind": kind.value,
                    "failed": True,
                    "error": str(exc),
                },
            )
            return None


# Imported lazily to avoid a circular import at module load time.
def _normalise_status(status: str) -> str:
    mapping = {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "??": "added",
        "C": "added",
    }
    return mapping.get(status.strip()[:1], "modified")
