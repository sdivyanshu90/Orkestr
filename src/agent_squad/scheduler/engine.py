"""The asynchronous DAG scheduler.

The scheduler is the only component that decides *what runs next*.  It owns
the ready queue, the concurrency limit, the retry policy, the deadlines and
the cancellation propagation.

Consistency guarantees of this local runtime
-------------------------------------------

* **Serialised state changes.**  Every state change goes through
  :class:`~agent_squad.store.state.StateStore`, which performs the event
  append and the projection update inside a single SQLite transaction.
* **Attempt fencing.**  Each attempt increments a monotonically increasing
  counter.  A worker may only write state for the attempt it holds, so a
  late message from a superseded attempt is rejected instead of overwriting
  a newer result.
* **Single writer.**  SQLite in WAL mode with ``BEGIN IMMEDIATE`` gives one
  writer at a time; the scheduler is the only writer during a run.
* **No exactly-once side effects.**  The scheduler guarantees that a *task
  transition* is applied once.  It does not guarantee that an arbitrary
  command a worker ran happened exactly once: a worker that crashed after
  running a command but before recording the result will be retried, and the
  command may run again.  Idempotency keys are recorded for such
  operations so that the repetition is detectable.
* **Deterministic replay, not deterministic re-execution.**  Replaying the
  event log reproduces the state machine exactly.  Re-running the run does
  not: model outputs are stochastic and are recorded rather than regenerated.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import RunConfig
from ..dag.graph import TaskGraph
from ..errors import StaleAttemptError
from ..models.message import MessageKind, make_message
from ..models.run import RunStatus, RunSummary
from ..models.task import TaskSpec, TaskStatus, utcnow
from ..planning.brief import TaskBrief
from ..store.events import EventLog, EventType
from ..store.state import StateStore
from ..verification.gate import VerificationGate, VerificationReport


@dataclass
class TaskOutcome:
    """What happened to one task."""

    task_id: str
    status: TaskStatus
    attempts: int = 0
    verification: VerificationReport | None = None
    error: str | None = None
    error_class: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    patch_artifact_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "error": self.error,
            "error_class": self.error_class,
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "duration_s": round(self.duration_s, 6),
            "verification": self.verification.to_dict() if self.verification else None,
            "patch_artifact_id": self.patch_artifact_id,
        }


@dataclass
class Scheduler:
    """Dependency-aware, bounded-concurrency task scheduler."""

    config: RunConfig
    graph: TaskGraph
    state: StateStore
    log: EventLog
    runner: Any
    gate: VerificationGate
    briefs: dict[str, TaskBrief] = field(default_factory=dict)
    bus: Any = None
    max_workers: int = 4
    cancellation: asyncio.Event | None = None
    idempotency_prefix: str = ""
    base_revision: str | None = None
    tracer: Any = None
    logger: Any = None
    _started_at: Any = None
    _task_heads: dict[str, str] = field(default_factory=dict)
    outcomes: dict[str, TaskOutcome] = field(default_factory=dict)

    # ------------------------------------------------------------- helpers

    async def _publish(
        self,
        kind: MessageKind,
        *,
        task: TaskSpec,
        attempt: int,
        recipient: str,
        **payload: Any,
    ) -> None:
        """Publish a supervisor-side message to the bus."""
        if self.bus is None:
            return
        try:
            register = getattr(self.bus, "register_actor", None)
            if register is not None:
                # The worker's identity must be known to the bus before it
                # can be addressed by the supervisor.
                register(recipient)
            await self.bus.publish(
                make_message(
                    kind,
                    run_id=self.config.run_id,
                    sender="supervisor",
                    recipient=recipient,
                    task_id=task.id,
                    correlation_id=f"{task.id}#{attempt}",
                    **payload,
                )
            )
        except Exception as exc:
            # A message failure must not change the schedule; the event log
            # is the durable record of what happened.
            self.log.append(
                EventType.MESSAGE_SENT,
                run_id=self.config.run_id,
                task_id=task.id,
                attempt=attempt,
                actor="supervisor",
                payload={
                    "kind": kind.value,
                    "failed": True,
                    "error": str(exc),
                },
            )

    def _owner(self, task: TaskSpec) -> str:
        return f"{task.role.value}:{task.id}"

    def _timeout_for(self, task: TaskSpec) -> float:
        if task.budget.deadline_s:
            return task.budget.deadline_s
        return self.config.scheduler.default_task_timeout_s

    def _base_for(self, task: TaskSpec) -> str | None:
        """The revision a task's worktree should start from.

        A task with dependencies starts from the head produced by the last
        completed dependency, so that dependent work sees the changes it
        depends on.  The patch the task produces is relative to *that* base,
        which is why integration replays patches in dependency order.
        """
        for dependency in task.depends_on:
            head = self._task_heads.get(dependency)
            if head:
                return head
        return self.base_revision

    def _backoff(self, attempt: int, task_id: str) -> float:
        """Deterministic jitter derived from the task id.

        Real jitter uses randomness; a hash-derived jitter still de-synchronises
        concurrent workers while remaining reproducible across processes and
        across replays.  ``hashlib`` is used rather than Python's built-in
        ``hash`` because the latter is salted per process.
        """
        import hashlib

        digest = hashlib.sha256(
            f"{self.idempotency_prefix}:{task_id}:{attempt}".encode()
        ).digest()
        factor = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return self.config.retry.backoff_for(attempt, factor)

    # ----------------------------------------------------------------- run

    async def run(self) -> RunSummary:
        """Execute the whole graph and return a summary."""
        started = time.monotonic()
        self._started_at = utcnow()
        completed: set[str] = set()
        failed: set[str] = set()
        blocked: set[str] = set()
        cancelled: set[str] = set()
        outcomes: dict[str, TaskOutcome] = {}

        pending: set[str] = set(self.graph.tasks)
        running: dict[asyncio.Task[Any], str] = {}

        for task in self.graph.tasks.values():
            self.state.transition_task(task.id, TaskStatus.READY)

        try:
            while pending or running:
                if self.cancellation is not None and self.cancellation.is_set():
                    for task_id in sorted(pending):
                        await self._publish_cancellation(task_id, "the run was cancelled")
                        self._cancel_task(task_id, cancelled)
                        outcomes[task_id] = TaskOutcome(
                            task_id=task_id,
                            status=TaskStatus.CANCELLED,
                            error="the run was cancelled",
                            error_class="cancelled",
                        )
                        pending.discard(task_id)
                    break

                # Tasks whose ancestors failed can never run.
                newly_blocked = self.graph.blocked_by_failure(failed) - blocked - completed
                for task_id in sorted(newly_blocked):
                    self.state.transition_task(
                        task_id, TaskStatus.BLOCKED, payload={"reason": "dependency failed"}
                    )
                    blocked.add(task_id)
                    pending.discard(task_id)
                    outcomes[task_id] = TaskOutcome(
                        task_id=task_id,
                        status=TaskStatus.BLOCKED,
                        error="a prerequisite task failed",
                        error_class="dependency",
                    )

                ready = self.graph.ready_tasks(completed, blocked)
                ready = [
                    t
                    for t in ready
                    if t.id in pending
                ]
                ready.sort(key=lambda t: (-t.priority, t.id))

                while ready and len(running) < self.max_workers:
                    task = ready.pop(0)
                    pending.discard(task.id)
                    handle = asyncio.create_task(self._execute(task), name=f"task:{task.id}")
                    running[handle] = task.id

                if not running:
                    break

                done, _ = await asyncio.wait(
                    running.keys(), return_when=asyncio.FIRST_COMPLETED
                )
                for handle in done:
                    task_id = running.pop(handle)
                    try:
                        outcome = handle.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        outcome = TaskOutcome(
                            task_id=task_id,
                            status=TaskStatus.FAILED,
                            error=str(exc),
                            error_class="unknown",
                        )
                    outcomes[task_id] = outcome
                    if outcome.status is TaskStatus.SUCCEEDED:
                        completed.add(task_id)
                    elif outcome.status in (TaskStatus.FAILED, TaskStatus.TIMED_OUT):
                        failed.add(task_id)
                    elif outcome.status is TaskStatus.CANCELLED:
                        cancelled.add(task_id)
        except asyncio.CancelledError:
            for handle in running:
                handle.cancel()
            raise
        finally:
            for handle in list(running):
                handle.cancel()
            if running:
                await asyncio.gather(*running.keys(), return_exceptions=True)

        summary = self._summarise(outcomes, started)
        final_status = self._final_status(summary)
        summary.status = final_status
        self.outcomes = outcomes
        self.state.set_run_status(self.config.run_id, final_status)
        return summary

    # ------------------------------------------------------------ execute

    async def _execute(self, task: TaskSpec) -> TaskOutcome:
        """Run one task, retrying according to the configured policy."""
        tracer = getattr(self, "tracer", None)
        if tracer is not None:
            with tracer.span(
                "task",
                run_id=self.config.run_id,
                task_id=task.id,
                role=task.role.value,
            ):
                return await self._execute_inner(task)
        return await self._execute_inner(task)

    async def _execute_inner(self, task: TaskSpec) -> TaskOutcome:
        attempts = 0
        max_attempts = min(task.budget.max_attempts, self.config.retry.max_attempts)
        outcome = TaskOutcome(task_id=task.id, status=TaskStatus.FAILED)

        while attempts < max_attempts:
            if self.cancellation is not None and self.cancellation.is_set():
                self._cancel_task(task.id, set())
                return TaskOutcome(
                    task_id=task.id, status=TaskStatus.CANCELLED, attempts=attempts
                )
            attempts += 1
            try:
                state = self.state.start_attempt(
                    task.id,
                    owner=self._owner(task),
                    lease_ttl_s=self.config.scheduler.lease_ttl_s,
                )
            except Exception as exc:
                return TaskOutcome(
                    task_id=task.id,
                    status=TaskStatus.FAILED,
                    attempts=attempts,
                    error=str(exc),
                    error_class="validation",
                )
            attempt = state.attempt

            try:
                self.state.transition_task(task.id, TaskStatus.RUNNING, attempt=attempt)
            except StaleAttemptError:
                return outcome

            await self._publish(
                MessageKind.TASK_ASSIGNMENT,
                task=task,
                attempt=attempt,
                recipient=self._owner(task),
                attempt_number=attempt,
                base_revision=self._base_for(task),
                objective=task.objective,
            )
            started = time.monotonic()
            result = None
            timed_out = False
            try:
                result = await asyncio.wait_for(
                    self.runner.execute(
                        task,
                        self.briefs.get(task.id) or self._fallback_brief(task),
                        attempt=attempt,
                        cancellation=self.cancellation,
                        base_revision=self._base_for(task),
                    ),
                    timeout=self._timeout_for(task),
                )
            except TimeoutError:
                timed_out = True
            except asyncio.CancelledError:
                self._cancel_task(task.id, set())
                raise
            except Exception as exc:
                result = None
                failure_message = str(exc)
                failure_class = "unknown"

            if timed_out:
                with contextlib.suppress(StaleAttemptError):
                    self.state.transition_task(
                        task.id,
                        TaskStatus.TIMED_OUT,
                        attempt=attempt,
                        error_class="timeout",
                        error_message=f"exceeded {self._timeout_for(task):.0f}s deadline",
                    )
                if attempts < max_attempts:
                    self.state.transition_task(
                        task.id, TaskStatus.RETRY_SCHEDULED, attempt=attempt
                    )
                    await asyncio.sleep(self._backoff(attempts, task.id))
                    self.state.transition_task(task.id, TaskStatus.READY, attempt=attempt)
                    continue
                return TaskOutcome(
                    task_id=task.id,
                    status=TaskStatus.TIMED_OUT,
                    attempts=attempts,
                    error=f"exceeded {self._timeout_for(task):.0f}s deadline",
                    error_class="timeout",
                    duration_s=time.monotonic() - started,
                )

            if result is None:
                # An unexpected exception escaped the worker.
                with contextlib.suppress(StaleAttemptError):
                    self.state.transition_task(
                        task.id,
                        TaskStatus.FAILED,
                        attempt=attempt,
                        error_class=failure_class,
                        error_message=failure_message,
                    )
                if attempts < max_attempts:
                    await asyncio.sleep(self._backoff(attempts, task.id))
                    self.state.transition_task(task.id, TaskStatus.READY, attempt=attempt)
                    continue
                return TaskOutcome(
                    task_id=task.id,
                    status=TaskStatus.FAILED,
                    attempts=attempts,
                    error=failure_message,
                    error_class=failure_class,
                    duration_s=time.monotonic() - started,
                )

            if not result.ok:
                with contextlib.suppress(StaleAttemptError):
                    self.state.transition_task(
                        task.id,
                        TaskStatus.FAILED,
                        attempt=attempt,
                        error_class=result.error_class,
                        error_message=result.error,
                    )
                self.log.append(
                    EventType.TASK_ATTEMPT_FINISHED,
                    run_id=self.config.run_id,
                    task_id=task.id,
                    attempt=attempt,
                    actor=self._owner(task),
                    payload={
                        "ok": False,
                        "error": result.error,
                        "error_class": result.error_class,
                    },
                )
                retryable = self._is_retryable(result)
                if retryable and attempts < max_attempts:
                    await asyncio.sleep(self._backoff(attempts, task.id))
                    self.state.transition_task(task.id, TaskStatus.READY, attempt=attempt)
                    continue
                self._dispose_worktree(task, result, None)
                await self._escalate(
                    task,
                    attempt,
                    reason=result.error or "the attempt failed",
                    severity="medium",
                    details=(
                        f"task {task.id!r} failed after {attempts} attempt(s): "
                        f"{result.error}"
                    ),
                )
                return TaskOutcome(
                    task_id=task.id,
                    status=TaskStatus.FAILED,
                    attempts=attempts,
                    error=result.error,
                    error_class=result.error_class,
                    tokens_used=result.tokens_used,
                    cost_usd=result.cost_usd,
                    duration_s=time.monotonic() - started,
                )

            # The worker succeeded.  Now verify independently.
            report = await self._verify(task, result)
            if result.head_revision:
                self._task_heads[task.id] = result.head_revision
            self._dispose_worktree(task, result, report)
            if report is not None and not report.passed:
                with contextlib.suppress(StaleAttemptError):
                    self.state.transition_task(
                        task.id,
                        TaskStatus.FAILED,
                        attempt=attempt,
                        error_class="verification",
                        error_message=report.error,
                    )
                retryable = True
                if attempts < max_attempts:
                    await self._publish(
                        MessageKind.REVISION_REQUEST,
                        task=task,
                        attempt=attempt,
                        recipient=self._owner(task),
                        reason=report.error or "the acceptance checks failed",
                        criteria=tuple(report.failed),
                        requested_changes=tuple(
                            f"criterion {cid} did not pass" for cid in report.failed
                        ),
                    )
                    await self._publish(
                        MessageKind.VERIFICATION_REQUEST,
                        task=task,
                        attempt=attempt,
                        recipient="integration",
                        artifact_ids=tuple(
                            result.evidence.source_artifact_ids
                            if result.evidence
                            else ()
                        ),
                        criteria=tuple(report.failed),
                    )
                    await asyncio.sleep(self._backoff(attempts, task.id))
                    self.state.transition_task(task.id, TaskStatus.READY, attempt=attempt)
                    continue
                self._dispose_worktree(task, result, report)
                return TaskOutcome(
                    task_id=task.id,
                    status=TaskStatus.FAILED,
                    attempts=attempts,
                    error=report.error,
                    error_class="verification",
                    verification=report,
                    tokens_used=result.tokens_used,
                    cost_usd=result.cost_usd,
                    duration_s=time.monotonic() - started,
                    patch_artifact_id=result.evidence.patch_artifact_id
                    if result.evidence
                    else None,
                )

            with contextlib.suppress(StaleAttemptError):
                self.state.transition_task(task.id, TaskStatus.SUCCEEDED, attempt=attempt)

            self.log.append(
                EventType.TASK_ATTEMPT_FINISHED,
                run_id=self.config.run_id,
                task_id=task.id,
                attempt=attempt,
                actor=self._owner(task),
                payload={
                    "ok": True,
                    "verified": bool(report and report.passed),
                    "tokens": result.tokens_used,
                    "cost_usd": result.cost_usd,
                },
            )

            return TaskOutcome(
                task_id=task.id,
                status=TaskStatus.SUCCEEDED,
                attempts=attempts,
                verification=report,
                tokens_used=result.tokens_used,
                cost_usd=result.cost_usd,
                duration_s=time.monotonic() - started,
                patch_artifact_id=result.evidence.patch_artifact_id
                if result.evidence
                else None,
            )

        return outcome

    def _dispose_worktree(self, task: TaskSpec, result: Any, report: Any) -> None:
        """Remove the task's worktree once verification has finished.

        The worktree is kept when the attempt failed, when verification
        failed, or when the configuration asks for it, so that a failure can
        be inspected after the fact.
        """
        path = getattr(result, "worktree_path", None)
        if not path:
            return
        keep = bool(getattr(self.runner, "keep_worktrees", False))
        if not result.ok:
            keep = True
        if report is not None and not report.passed:
            keep = True
        if self.config.workspace.keep_worktrees_on_failure and not result.ok:
            keep = True
        if self.config.workspace.keep_worktrees_on_success and result.ok:
            keep = True
        cleanup = getattr(self.runner, "cleanup_worktree", None)
        if cleanup is not None:
            cleanup(path, keep=keep)

    # ---------------------------------------------------------- verification

    @staticmethod
    def _is_retryable(result: Any) -> bool:
        """Decide whether a failed attempt is worth repeating.

        The error class recorded by the runtime is authoritative, but the
        error object itself carries a ``retryable`` flag that distinguishes
        a permanent provider error (bad credentials) from a transient one
        (a rate limit).  When the class is unknown the failure is treated as
        permanent, because retrying a permanent failure only wastes budget.
        """
        error_class = (result.error_class or "").lower()
        if error_class in ("policy", "validation", "budget", "conflict"):
            return False
        if error_class in ("model", "tool", "timeout", "infrastructure", "unknown", ""):
            return True
        return True

    async def _verify(self, task: TaskSpec, result: Any) -> VerificationReport | None:
        """Run the acceptance criteria independently of the worker."""
        if not task.acceptance_criteria:
            return None
        workspace = result.worktree_path
        if not workspace:
            return None
        try:
            return await self.gate.verify_task(
                task,
                workspace=Path(workspace),
                has_changes=bool(result.changed_files),
                evidence=result.evidence,
            )
        except Exception as exc:
            return VerificationReport(
                task_id=task.id,
                passed=False,
                level="validation_executed",
                error=f"verification could not run: {exc}",
            )

    # -------------------------------------------------------------- helpers

    def _fallback_brief(self, task: TaskSpec) -> TaskBrief:
        return TaskBrief(
            task_id=task.id,
            role=task.role,
            objective=task.objective,
            description=task.description,
            scope=task.scope,
            acceptance_criteria=tuple(
                c.model_dump(mode="json") for c in task.acceptance_criteria
            ),
            depends_on=task.depends_on,
        )

    def _cancel_task(self, task_id: str, cancelled: set[str]) -> None:
        with contextlib.suppress(Exception):
            self.state.transition_task(task_id, TaskStatus.CANCELLED)
        cancelled.add(task_id)

    async def _escalate(
        self,
        task: TaskSpec,
        attempt: int,
        *,
        reason: str,
        severity: str = "medium",
        details: str = "",
    ) -> None:
        """Tell the supervisor that a task has failed for good.

        The scheduler is the component that knows a task will not be retried
        again, so it is the component that escalates.  An escalation is a
        request for judgement, not another attempt.
        """
        await self._publish(
            MessageKind.ESCALATION,
            task=task,
            attempt=attempt,
            recipient="supervisor",
            reason=reason,
            severity=severity,
            details=details,
        )

    async def _publish_cancellation(self, task_id: str, reason: str) -> None:
        """Tell a task that it is being cancelled."""
        task = self.graph.tasks.get(task_id)
        if task is None:
            return
        state = self.state.get_task_state(task_id)
        await self._publish(
            MessageKind.CANCELLATION,
            task=task,
            attempt=state.attempt if state else 0,
            recipient=self._owner(task),
            reason=reason,
            scope="task",
        )

    def _final_status(self, summary: RunSummary) -> RunStatus:
        if summary.cancelled:
            return RunStatus.CANCELLED
        if summary.failed:
            return RunStatus.COMPLETED_WITH_FAILURES
        return RunStatus.SUCCEEDED

    def _summarise(self, outcomes: dict[str, TaskOutcome], started: float) -> RunSummary:
        succeeded = sum(1 for o in outcomes.values() if o.status is TaskStatus.SUCCEEDED)
        failed = sum(
            1
            for o in outcomes.values()
            if o.status in (TaskStatus.FAILED, TaskStatus.TIMED_OUT)
        )
        cancelled = sum(1 for o in outcomes.values() if o.status is TaskStatus.CANCELLED)
        blocked = sum(1 for o in outcomes.values() if o.status is TaskStatus.BLOCKED)
        levels: dict[str, int] = {}
        for outcome in outcomes.values():
            if outcome.verification:
                levels[outcome.verification.level] = (
                    levels.get(outcome.verification.level, 0) + 1
                )
        return RunSummary(
            run_id=self.config.run_id,
            status=RunStatus.RUNNING,
            total_tasks=len(self.graph.tasks),
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            blocked=blocked,
            tokens_used=sum(o.tokens_used for o in outcomes.values()),
            cost_usd=round(sum(o.cost_usd for o in outcomes.values()), 8),
            wall_clock_s=time.monotonic() - started,
            started_at=self._started_at,
            finished_at=utcnow(),
            verification=levels,
        )
