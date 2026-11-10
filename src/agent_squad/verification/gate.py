"""The verification gate.

The gate is the component that decides whether a piece of work is *done*, and
it deliberately refuses to take a worker's word for it.  It re-executes the
acceptance criteria itself, against the actual workspace, and records what
happened.

The distinction the gate maintains is the one that matters most in this
system:

``worker reported``
    The agent said it finished.
``patch produced``
    A diff exists and its hash is known.
``validation executed``
    The gate ran the specified checks.
``checks passed``
    Those checks exited zero.
``integration validated``
    The combined result of all patches was re-verified.
``human approved``
    A person signed off on a protected action.

Only the first three levels can be reached by a worker alone.  Passing the
gate means *these specific checks ran and produced these specific results* --
never "the change is correct".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import RunConfig
from ..errors import VerificationError
from ..models.evidence import CheckRecord, CriterionState, EvidencePackage
from ..models.task import AcceptanceCriterion, TaskSpec
from ..store.events import EventLog, EventType
from ..store.state import StateStore
from .checks import CheckOutcome, run_criterion


@dataclass
class VerificationReport:
    """The result of gating one task (or one integrated change set)."""

    task_id: str
    passed: bool
    level: str = "none"
    checks: list[CheckRecord] = field(default_factory=list)
    criterion_states: dict[str, str] = field(default_factory=dict)
    unverified: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0
    workspace: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "level": self.level,
            "checks": [c.model_dump(mode="json") for c in self.checks],
            "criterion_states": dict(self.criterion_states),
            "unverified": list(self.unverified),
            "advisory": list(self.advisory),
            "failed": list(self.failed),
            "error": self.error,
            "duration_s": round(self.duration_s, 6),
            "workspace": self.workspace,
        }


class VerificationGate:
    """Runs acceptance criteria independently of the worker."""

    def __init__(
        self,
        config: RunConfig,
        state: StateStore | None = None,
        log: EventLog | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._log = log

    async def verify_task(
        self,
        task: TaskSpec,
        *,
        workspace: Path,
        has_changes: bool = False,
        evidence: EvidencePackage | None = None,
    ) -> VerificationReport:
        """Verify one task against a workspace."""
        import time

        started = time.monotonic()
        criteria = list(task.acceptance_criteria)
        if not criteria:
            report = VerificationReport(
                task_id=task.id,
                passed=False,
                level="worker_reported",
                error="the task declares no acceptance criteria; nothing to verify",
                workspace=str(workspace),
                duration_s=time.monotonic() - started,
            )
            self._record(task, report)
            return report

        report = await self._verify_criteria(
            task.id,
            criteria,
            workspace=workspace,
            has_changes=has_changes,
        )
        report.workspace = str(workspace)
        report.duration_s = time.monotonic() - started

        if evidence is not None:
            merged = list(evidence.checks) + list(report.checks)
            evidence.checks = tuple(merged)
            self._recompute_states(evidence, criteria)

        self._record(task, report)
        return report

    async def _verify_criteria(
        self,
        task_id: str,
        criteria: list[AcceptanceCriterion],
        *,
        workspace: Path,
        has_changes: bool,
    ) -> VerificationReport:
        checks: list[CheckRecord] = []
        states: dict[str, str] = {}
        unverified: list[str] = []
        advisory: list[str] = []
        failed: list[str] = []

        for criterion in criteria:
            outcome: CheckOutcome = await run_criterion(
                criterion,
                workspace=workspace,
                sandbox_config=self._config.sandbox,
                has_changes=has_changes,
                timeout_s=criterion.timeout_s or self._config.verification.command_timeout_s,
            )
            checks.append(outcome.record)
            record = outcome.record
            if not record.deterministic:
                states[criterion.id] = CriterionState.ADVISORY.value
                advisory.append(criterion.id)
            elif record.passed:
                states[criterion.id] = CriterionState.EXECUTED_PASSED.value
            else:
                states[criterion.id] = CriterionState.EXECUTED_FAILED.value
                failed.append(criterion.id)

        for criterion in criteria:
            if criterion.id not in states:
                states[criterion.id] = CriterionState.UNVERIFIED.value
                unverified.append(criterion.id)

        required = {c.id for c in criteria if c.required}
        blocking_failures = [c for c in failed if c in required]
        passed = not blocking_failures

        return VerificationReport(
            task_id=task_id,
            passed=passed,
            level="checks_passed" if passed else "validation_executed",
            checks=checks,
            criterion_states=states,
            unverified=unverified,
            advisory=advisory,
            failed=failed,
            error=(
                "required checks failed: " + ", ".join(sorted(blocking_failures))
                if blocking_failures
                else None
            ),
        )

    @staticmethod
    def _recompute_states(
        evidence: EvidencePackage, criteria: list[AcceptanceCriterion]
    ) -> None:
        for criterion in criteria:
            evidence.criteria_addressed = tuple(
                dict.fromkeys((*evidence.criteria_addressed, criterion.id))
            )

    def _record(self, task: TaskSpec, report: VerificationReport) -> None:
        if self._log is None:
            return
        self._log.append(
            EventType.VERIFICATION_CHECK,
            run_id=task.run_id,
            task_id=task.id,
            payload={
                "passed": report.passed,
                "level": report.level,
                "checks": len(report.checks),
                "failed": report.failed,
                "advisory": report.advisory,
                "unverified": report.unverified,
            },
        )
        self._log.append(
            EventType.VERIFICATION_PASSED if report.passed else EventType.VERIFICATION_FAILED,
            run_id=task.run_id,
            task_id=task.id,
            payload={"level": report.level, "error": report.error},
        )
        if self._state is not None:
            self._state.set_verification(task.id, report.to_dict())
