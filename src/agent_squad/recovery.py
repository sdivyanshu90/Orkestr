"""Crash recovery.

A run can be interrupted at any point: the process can be killed, the host
can reboot, a worker can hang.  Recovery is the process of deciding what the
run should do next given the durable record of what it had already done.

The rules recovery follows:

* **The event log is the truth.**  The projection is repaired from it, not
  the other way round.
* **A task that was RUNNING when the process died is not assumed to have
  finished.**  It is returned to the ready queue *only* if its attempt budget
  allows, and only after its lease has expired.
* **Side effects are not repeated blindly.**  A worker may have run a
  command before it died.  Recovery records the fact that the attempt was
  interrupted so that the operator (or a reconciliation step) can decide
  whether repeating it is safe.  Recovery does not attempt to undo it.
* **Recovery is idempotent.**  Running recovery twice produces the same
  state as running it once.

Recovery is deliberately conservative: it never invents work, never marks a
task succeeded without evidence, and never silently discards a failed
attempt.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents.runner import WorkerRunner
from .dag.graph import TaskGraph
from .models.task import TaskStatus, utcnow
from .scheduler.engine import Scheduler
from .store.db import Database
from .store.events import EventLog, EventType
from .store.state import StateStore

SUPERVISOR = "supervisor"


@dataclass
class RecoveryReport:
    """What recovery found and what it did."""

    run_id: str
    found: bool = False
    run_status: str | None = None
    rebuilt: dict[str, int] = field(default_factory=dict)
    interrupted_tasks: list[str] = field(default_factory=list)
    resumable_tasks: list[str] = field(default_factory=list)
    exhausted_tasks: list[str] = field(default_factory=list)
    completed_tasks: list[str] = field(default_factory=list)
    unresolved_side_effects: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    resumable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "found": self.found,
            "run_status": self.run_status,
            "rebuilt": self.rebuilt,
            "interrupted_tasks": self.interrupted_tasks,
            "resumable_tasks": self.resumable_tasks,
            "exhausted_tasks": self.exhausted_tasks,
            "completed_tasks": self.completed_tasks,
            "unresolved_side_effects": self.unresolved_side_effects,
            "actions": self.actions,
            "resumable": self.resumable,
        }


def find_run(run_id: str, data_dir: str | Path | None = None) -> Path | None:
    """Locate the directory of a stored run."""
    base = Path(data_dir or ".agent-squad").resolve() / "runs"
    if not base.exists():
        return None
    direct = base / run_id
    if (direct / "state.db").exists():
        return direct
    for entry in sorted(base.iterdir()):
        if entry.name.startswith(run_id) and (entry / "state.db").exists():
            return entry
    return None


def recover_run(
    run_id: str, *, data_dir: str | Path | None = None, force: bool = False
) -> RecoveryReport:
    """Inspect an interrupted run and return it to a resumable state."""
    root = find_run(run_id, data_dir)
    report = RecoveryReport(run_id=run_id)
    if root is None:
        report.actions.append("no run directory found")
        return report

    report.found = True
    db = Database(root / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)

    try:
        run = state.get_run(run_id)
        if run is None:
            report.actions.append("run record missing")
            return report
        report.run_status = run["status"]

        # 1. Repair the projection from the log.
        report.rebuilt = state.rebuild_projection(run_id)
        report.actions.append(
            f"replayed {report.rebuilt['events_replayed']} event(s) into the projection"
        )

        log.append(
            EventType.RECOVERY_STARTED,
            run_id=run_id,
            payload={"interrupted": [], "force": force},
        )

        # 2. Classify tasks.
        specs = {t.id: t for t in state.list_task_specs(run_id)}
        states = state.list_task_states(run_id)
        now = utcnow()

        for task_state in states:
            spec = specs.get(task_state.id)
            if task_state.status is TaskStatus.SUCCEEDED:
                report.completed_tasks.append(task_state.id)
                continue
            if task_state.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                continue
            if task_state.status in (TaskStatus.RUNNING, TaskStatus.READY):
                report.interrupted_tasks.append(task_state.id)
                # A lease that has not expired might belong to a live worker.
                lease = task_state.lease_expires_at
                if lease is not None and lease > now and not force:
                    report.actions.append(
                        f"task {task_state.id} still holds a live lease; not resumed"
                    )
                    continue
                max_attempts = (
                    spec.budget.max_attempts if spec else task_state.max_attempts
                )
                if task_state.attempt >= max_attempts:
                    report.exhausted_tasks.append(task_state.id)
                    report.actions.append(
                        f"task {task_state.id} exhausted its {max_attempts} attempt(s)"
                    )
                    with contextlib.suppress(Exception):
                        state.transition_task(
                            task_state.id,
                            TaskStatus.BLOCKED,
                            error_class="recovery",
                            error_message="interrupted with no attempt budget left",
                        )
                    continue
                try:
                    if task_state.status is not TaskStatus.READY:
                        state.transition_task(
                            task_state.id,
                            TaskStatus.READY,
                            error_class=None,
                            error_message=None,
                        )
                except Exception as exc:
                    report.actions.append(
                        f"task {task_state.id} could not be reset: {exc}"
                    )
                    continue
                report.resumable_tasks.append(task_state.id)
                report.actions.append(f"task {task_state.id} returned to the ready queue")
                report.unresolved_side_effects.append(
                    {
                        "task_id": task_state.id,
                        "attempt": task_state.attempt,
                        "reason": (
                            "the worker was interrupted mid-attempt; any command it "
                            "had already executed may have taken effect and will "
                            "not be undone"
                        ),
                    }
                )

        # 3. Record the recovery.
        run_status = run["status"]
        if report.resumable_tasks:
            report.resumable = True
            if run_status in ("cancelled", "failed"):
                from .models.run import RunStatus

                state.set_run_status(
                    run_id, RunStatus.RUNNING, payload={"reason": "resumed after recovery"}
                )
        report.unresolved_side_effects = verify_side_effects(
            run_id, data_dir=data_dir
        )
        log.append(
            EventType.RECOVERY_COMPLETED,
            run_id=run_id,
            payload={
                "interrupted": report.interrupted_tasks,
                "resumable": report.resumable_tasks,
                "exhausted": report.exhausted_tasks,
                "unresolved_side_effects": len(report.unresolved_side_effects),
            },
        )
        return report
    finally:
        db.close()


def verify_side_effects(run_id: str, *, data_dir: str | Path | None = None) -> list[dict]:
    """List operations whose external effects could not be confirmed.

    The runtime records an *idempotency key* for every task attempt.  An
    attempt that started but never finished has an unknown side-effect
    state; this function surfaces those attempts so that an operator or a
    reconciliation job can decide what to do.
    """
    root = find_run(run_id, data_dir)
    if root is None:
        return []
    db = Database(root / "state.db")
    try:
        log = EventLog(db)
        events = log.read(run_id)
        started: dict[tuple[str, int], dict[str, Any]] = {}
        finished: set[tuple[str, int]] = set()
        for event in events:
            if event.type is EventType.TASK_ATTEMPT_STARTED and event.task_id:
                started[(event.task_id, int(event.attempt or 0))] = event.to_dict()
            elif event.type is EventType.TASK_ATTEMPT_FINISHED and event.task_id:
                finished.add((event.task_id, int(event.attempt or 0)))
        unresolved = []
        for key, event in started.items():
            if key in finished:
                continue
            unresolved.append(
                {
                    "task_id": key[0],
                    "attempt": key[1],
                    "started_at": event["created_at"],
                    "idempotency_key": f"{key[0]}#{key[1]}",
                    "reason": "attempt started but never recorded an outcome",
                }
            )
        return unresolved
    finally:
        db.close()


async def resume_run(
    run_id: str, *, data_dir: str | Path | None = None, force: bool = False
) -> dict[str, Any]:
    """Recover an interrupted run and continue executing it.

    The run's configuration and plan are read back from durable storage, the
    interrupted tasks are returned to the ready queue, and the scheduler is
    started again from that point.  Work that already succeeded is not
    repeated: its state and its patch artifacts are still in the store.
    """
    from .orchestrator import Orchestrator

    root = find_run(run_id, data_dir)
    if root is None:
        return {"run_id": run_id, "resumed": False, "reason": "run not found"}

    report = recover_run(run_id, data_dir=data_dir, force=force)
    if not report.resumable_tasks:
        return {
            "run_id": run_id,
            "resumed": False,
            "reason": "no task can be resumed",
            "recovery": report.to_dict(),
        }

    config_path = root / "run-config.json"
    if not config_path.exists():
        return {
            "run_id": run_id,
            "resumed": False,
            "reason": "the run configuration was not persisted",
            "recovery": report.to_dict(),
        }

    from .config import RunConfig

    config = RunConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    orchestrator = Orchestrator(config, data_dir=root.parent.parent)
    try:
        specs = orchestrator.state.list_task_specs(run_id)
        if not specs:
            return {
                "run_id": run_id,
                "resumed": False,
                "reason": "the run has no recoverable task specifications",
                "recovery": report.to_dict(),
            }
        graph = TaskGraph()
        graph.add_all(specs)
        graph.link()

        runner = WorkerRunner(
            config=config,
            state=orchestrator.state,
            log=orchestrator.log,
            artifacts=orchestrator.artifacts,
            worktrees=orchestrator.worktrees,
            bus=orchestrator.bus,
            provider=orchestrator.provider,
            supervisor=SUPERVISOR,
            policy=orchestrator.policy,
            approver=orchestrator.approver.request,
            budget=orchestrator.budget,
        )
        scheduler = Scheduler(
            config=config,
            graph=graph,
            state=orchestrator.state,
            log=orchestrator.log,
            runner=runner,
            gate=orchestrator.gate,
            briefs={},
            bus=orchestrator.bus,
            max_workers=config.scheduler.max_workers,
            idempotency_prefix=config.run_id,
            base_revision=(
                orchestrator.worktrees.head_revision()
                if orchestrator.worktrees.has_commits()
                else None
            ),
        )
        summary = await scheduler.run()
        return {
            "run_id": run_id,
            "resumed": True,
            "status": summary.status.value,
            "summary": summary.model_dump(mode="json"),
            "recovery": report.to_dict(),
        }
    finally:
        await orchestrator.aclose()
