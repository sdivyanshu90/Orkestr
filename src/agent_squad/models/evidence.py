"""Proof-carrying evidence packages.

An evidence package is the machine-readable claim a worker makes about the
work it did, together with the *actual* records of the checks that were
executed.  The orchestrator can therefore answer, per acceptance criterion:

* was a deterministic check executed?          -> ``executed``
* did the check pass?                            -> ``passed``
* is the claim advisory (model opinion only)?   -> ``advisory``

The distinction matters: a reviewer agent saying "the tests pass" is an
advisory opinion; a pytest invocation that exited 0 and produced a stored log
is a *recorded result* for the specific selector that was run.  Neither is a
proof of overall correctness, and the schema never claims otherwise.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .task import utcnow


class CriterionState(str, Enum):
    """Lifecycle of one acceptance criterion within an evidence package."""

    UNVERIFIED = "unverified"
    """No check was executed for this criterion."""

    ADVISORY = "advisory"
    """Only a model judgement exists (reviewer opinion, manual note)."""

    EXECUTED_FAILED = "executed_failed"
    """A deterministic check ran and failed."""

    EXECUTED_PASSED = "executed_passed"
    """A deterministic check ran and passed."""


class CheckRecord(BaseModel):
    """The record of one independently executed check."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    kind: str
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    duration_s: float = 0.0
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
    log_artifact_id: str | None = None
    deterministic: bool = True
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and self.error is None


class ChangedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    status: str = "modified"
    added_lines: int = 0
    removed_lines: int = 0


class Limitation(BaseModel):
    """A known gap that the producer explicitly declares."""

    model_config = ConfigDict(extra="forbid")
    description: str
    severity: str = "minor"


class EvidencePackage(BaseModel):
    """The complete, self-describing record of one unit of work."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: int = Field(default=1, ge=1)
    run_id: str
    task_id: str
    attempt: int = Field(default=1, ge=1)
    producer: str
    created_at: _dt.datetime = Field(default_factory=utcnow)

    changed_files: tuple[ChangedFile, ...] = ()
    patch_artifact_id: str | None = None
    patch_sha256: str | None = None
    base_revision: str | None = None
    head_revision: str | None = None

    criteria_addressed: tuple[str, ...] = ()
    checks: tuple[CheckRecord, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    residual_risks: tuple[str, ...] = ()
    notes: str = ""
    source_artifact_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> EvidencePackage:
        if self.patch_artifact_id and not self.patch_sha256:
            raise ValueError("patch_artifact_id requires a patch_sha256")
        known = set(self.criteria_addressed)
        for check in self.checks:
            if known and check.criterion_id not in known:
                raise ValueError(
                    f"check references criterion {check.criterion_id!r} which is not "
                    "listed in criteria_addressed"
                )
        return self

    def state_for(self, criterion_id: str) -> CriterionState:
        """Return the strongest state achieved for a criterion."""
        states: list[CriterionState] = []
        for check in self.checks:
            if check.criterion_id != criterion_id:
                continue
            if not check.deterministic:
                states.append(CriterionState.ADVISORY)
            elif check.passed:
                states.append(CriterionState.EXECUTED_PASSED)
            else:
                states.append(CriterionState.EXECUTED_FAILED)
        if not states:
            return CriterionState.UNVERIFIED
        if CriterionState.EXECUTED_PASSED in states:
            return CriterionState.EXECUTED_PASSED
        if CriterionState.EXECUTED_FAILED in states:
            return CriterionState.EXECUTED_FAILED
        return CriterionState.ADVISORY

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for cid in self.criteria_addressed:
            st = self.state_for(cid).value
            counts[st] = counts.get(st, 0) + 1
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "changed_files": len(self.changed_files),
            "criteria": counts,
            "limitations": len(self.limitations),
            "deterministic_checks": sum(1 for c in self.checks if c.deterministic),
        }
