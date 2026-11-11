"""Bounded replanning.

A task that fails verification is retried with the *same* brief, which is
usually useless: if the brief was wrong, the retry is wrong too.  Replanning
is the remedy, and it is deliberately hard to do.

The rules:

* **Bounded.**  A run may revise its plan a configured number of times.  A
  plan that keeps changing is a plan that is not converging, and the run
  stops rather than churning.
* **Contract-preserving.**  A revision may add checks; it may not remove
  or weaken a required one.  The contract is the owner's statement and it
  outranks the planner.
* **Progress-preserving.**  Tasks that were verified are kept as they
  are.  Their work is not redone.
* **A retry is not a replan.**  A revision that does not actually change
  anything is not a replan, and it does not consume a replan budget or
  reset the evidence.
* **Versioned.**  Every revision is emitted as ``PLAN_REVISED`` with the
  version, so the history shows what was attempted and why.

The supervisor's revision prompt states the failure, the failed criteria
and what must not change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError
from ..models.contract import RequestContract
from ..models.task import TaskSpec
from ..planning.planner import PlanRequest, PlanResult, enforce_contract


@dataclass
class RevisionContext:
    """What the supervisor is told when it asks for a revision."""

    objective: str
    description: str = ""
    failed_task_id: str = ""
    failure_reason: str = ""
    failed_criteria: tuple[str, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    current_plan: tuple[dict[str, Any], ...] = ()
    attempt: int = 1

    def to_prompt(self) -> str:
        return json.dumps(
            {
                "objective": self.objective,
                "description": self.description,
                "failed_task_id": self.failed_task_id,
                "failure_reason": self.failure_reason,
                "failed_criteria": list(self.failed_criteria),
                "completed_task_ids": list(self.completed_task_ids),
                "current_plan": list(self.current_plan),
                "attempt": self.attempt,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )


@dataclass
class ReplanOutcome:
    """What a replanning attempt produced."""

    revised: bool = False
    reason: str = ""
    version: int = 0
    plan: PlanResult | None = None
    changed_task_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "revised": self.outcome_dict()
            if hasattr(self, "outcome_dict")
            else self.revised,
            "reason": self.reason,
            "version": self.version,
            "changed_task_ids": list(self.changed_task_ids),
        }


def build_revision_request(
    *,
    run_id: str,
    context: RevisionContext,
    contract: RequestContract,
    repo_path: str,
    base_revision: str | None,
) -> PlanRequest:
    """The planner request for a revision."""
    from ..planning.planner import PlanResult  # noqa: F401  (documented dependency)

    return PlanRequest(
        run_id=run_id,
        objective=context.objective,
        description=context.description,
        repo_path=repo_path,
        base_revision=base_revision,
        request_spec={"objective": context.objective, "tasks": []},
        contract=contract,
    )


def diff_plans(
    previous: tuple[TaskSpec, ...], revised: tuple[TaskSpec, ...]
) -> tuple[str, ...]:
    """The ids of the tasks whose specification changed."""
    before = {t.id: t for t in previous}
    after = {t.id: t for t in revised}
    changed: list[str] = []
    for task_id, spec in after.items():
        old = before.get(task_id)
        if old is None:
            changed.append(task_id)
            continue
        if _material(old) != _material(spec):
            changed.append(task_id)
    for task_id in before:
        if task_id not in after:
            changed.append(task_id)
    return tuple(sorted(set(changed)))


def _material(task: TaskSpec) -> dict[str, Any]:
    """The parts of a task that constitute its substance."""
    return {
        "title": task.title,
        "objective": task.objective,
        "description": task.description,
        "depends_on": list(task.depends_on),
        "criteria": [c.model_dump(mode="json") for c in task.acceptance_criteria],
        "scope": task.scope.model_dump(mode="json"),
        "role": task.role.value,
    }


def merge_revisions(
    *,
    previous: tuple[TaskSpec, ...],
    revised: tuple[TaskSpec, ...],
    completed: dict[str, TaskSpec],
) -> tuple[TaskSpec, ...]:
    """Combine a revision with the work that was already verified.

    A task that was verified is restored exactly: the revision may have
    changed it, but that change is not allowed to discard verified work.
    """
    merged: dict[str, TaskSpec] = {t.id: t for t in revised}
    for task_id, spec in completed.items():
        merged[task_id] = spec
    # Preserve the original order, with new tasks appended.
    ordered: list[TaskSpec] = []
    seen: set[str] = set()
    for task in previous:
        if task.id in merged and task.id not in seen:
            ordered.append(merged[task.id])
            seen.add(task.id)
    for task in merged.values():
        if task.id not in seen:
            ordered.append(task)
            seen.add(task.id)
    return tuple(ordered)


def check_revision(
    *,
    previous: tuple[TaskSpec, ...],
    revised: tuple[TaskSpec, ...],
    contract: RequestContract,
    completed_task_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate a proposed revision.

    Returns the ids of the tasks that changed.  Raises when the revision is
    not admissible.
    """
    changed = diff_plans(previous, revised)
    if not changed:
        raise ValidationError(
            "the proposed revision does not change the plan; a retry with an "
            "unchanged brief is not a replan",
            changed=list(changed),
        )
    # The contract is enforced by the planner, but a revision that arrives
    # from anywhere else must be checked here too.
    from ..planning.planner import PlanResult

    candidate = PlanResult(tasks=list(revised), briefs={})
    enforce_contract(candidate, contract)
    # A verified task may not be dropped or weakened.
    for task_id in completed_task_ids:
        if task_id not in {t.id for t in revised}:
            raise ValidationError(
                f"the revision drops the verified task {task_id!r}",
                task_id=task_id,
            )
    return changed
