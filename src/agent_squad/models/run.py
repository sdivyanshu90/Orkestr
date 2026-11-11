"""Run-level models: status, verification levels and strategy decisions."""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .task import utcnow


class RunStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED_WITH_FAILURES = "completed_with_failures"


RUN_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.COMPLETED_WITH_FAILURES,
    }
)


class VerificationLevel(str, Enum):
    """How far a unit of work has progressed through the evidence chain.

    These levels are strictly ordered and must never be conflated.  A task
    that a worker *claims* is done is at :attr:`WORKER_REPORTED`; it is not
    at :attr:`CHECKS_PASSED` until the orchestrator independently executed
    the specified checks and recorded the result.
    """

    NONE = "none"
    WORKER_REPORTED = "worker_reported"
    PATCH_PRODUCED = "patch_produced"
    VALIDATION_EXECUTED = "validation_executed"
    CHECKS_PASSED = "checks_passed"
    INTEGRATION_VALIDATED = "integration_validated"
    HUMAN_APPROVED = "human_approved"

    @property
    def rank(self) -> int:
        return _VERIFICATION_ORDER.index(self)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, VerificationLevel):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, VerificationLevel):
            return NotImplemented
        return self.rank <= other.rank


_VERIFICATION_ORDER = [
    VerificationLevel.NONE,
    VerificationLevel.WORKER_REPORTED,
    VerificationLevel.PATCH_PRODUCED,
    VerificationLevel.VALIDATION_EXECUTED,
    VerificationLevel.CHECKS_PASSED,
    VerificationLevel.INTEGRATION_VALIDATED,
    VerificationLevel.HUMAN_APPROVED,
]


class StrategyName(str, Enum):
    """Execution strategies the selector can choose between."""

    SINGLE = "single"
    STATIC_TWO = "static_two"
    STATIC_FOUR = "static_four"
    ADAPTIVE = "adaptive"
    DAG = "dag"

    @property
    def worker_count(self) -> int:
        return {
            StrategyName.SINGLE: 1,
            StrategyName.STATIC_TWO: 2,
            StrategyName.STATIC_FOUR: 4,
            StrategyName.ADAPTIVE: 0,  # decided at runtime
            StrategyName.DAG: 0,  # decided at runtime
        }[self]


class StrategyDecision(BaseModel):
    """An explainable record of *why* a strategy was chosen.

    The decision engine is deterministic and heuristic.  The rationale list
    is part of the persisted record so that a later analyst can audit the
    reasoning and compare it with the observed outcome.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: StrategyName
    worker_count: int
    independent_tasks: int = 0
    critical_path_tasks: int = 0
    estimated_parallel_speedup: float = 0.0
    estimated_coordination_overhead_tokens: int = 0
    estimated_context_duplication_tokens: int = 0
    estimated_integration_risk: float = 0.0
    estimated_total_cost_usd: float = 0.0
    rationale: tuple[str, ...] = ()
    factors: dict[str, Any] = Field(default_factory=dict)
    created_at: _dt.datetime = Field(default_factory=utcnow)

    def explain(self) -> str:
        return (
            f"strategy={self.strategy.value} workers={self.worker_count} "
            f"independent_tasks={self.independent_tasks} "
            f"critical_path={self.critical_path_tasks} "
            f"speedup~{self.estimated_parallel_speedup:.2f} "
            f"overhead_tokens={self.estimated_coordination_overhead_tokens}"
        )


class RunSummary(BaseModel):
    """Aggregate outcome of a run, as recorded in the event store."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    total_tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    blocked: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    verification: dict[str, int] = Field(default_factory=dict)
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
