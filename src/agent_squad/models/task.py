"""Task specification, lifecycle states and the legal transition table.

The task lifecycle is intentionally explicit.  A task's status is only ever
changed through :func:`assert_transition`, which rejects illegal moves with
:class:`~agent_squad.errors.ValidationError`.  The scheduler, the recovery
path and the CLI all mutate state through the same guarded API, so an
illegal transition is a bug that surfaces immediately rather than a subtly
corrupt run.

Legal transitions::

    PENDING ──────────────► READY ──────────────► RUNNING ──► SUCCEEDED
      │  ▲                    │  ▲                  │  │
      │  │ replan             │  │ retry scheduled  │  ├────► RETRY_SCHEDULED ──► READY
      │  └────────────────────┘  │                  │  ├────► FAILED
      │                          │                  │  ├────► TIMED_OUT ──► RETRY_SCHEDULED
      ├──► BLOCKED ◄────────────┴──────────────────┘  ├────► CANCELLED
      └──► CANCELLED                                    └────► AWAITING_APPROVAL

    SUCCEEDED, CANCELLED are terminal.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import ValidationError


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


class Role(str, Enum):
    """The bounded responsibilities an agent may hold."""

    SUPERVISOR = "supervisor"
    IMPLEMENTER = "implementer"
    TEST_ENGINEER = "test_engineer"
    REVIEWER = "reviewer"
    INTEGRATION = "integration"


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELLED})

LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PENDING,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.RETRY_SCHEDULED,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RETRY_SCHEDULED: frozenset(
        {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.BLOCKED}
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.TIMED_OUT: frozenset(
        {TaskStatus.RETRY_SCHEDULED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PENDING, TaskStatus.CANCELLED}),
    TaskStatus.CANCELLED: frozenset(),
}


def assert_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Raise if ``current -> target`` is not a legal lifecycle move."""
    if current == target:
        return
    if target not in LEGAL_TRANSITIONS[current]:
        raise ValidationError(
            f"illegal task transition {current.value} -> {target.value}",
            current=current.value,
            target=target.value,
        )


class CheckKind(str, Enum):
    """Kinds of deterministic acceptance check the verifier can execute."""

    FILE_EXISTS = "file_exists"
    FILE_CONTAINS = "file_contains"
    FILE_NOT_CONTAINS = "file_not_contains"
    COMMAND_EXITS_ZERO = "command_exits_zero"
    COMMAND_OUTPUT_MATCHES = "command_output_matches"
    PYTEST_SELECTOR = "pytest_selector"
    DIFF_NONEMPTY = "diff_nonempty"
    DIFF_EMPTY = "diff_empty"
    LLM_REVIEW = "llm_review"
    MANUAL = "manual"


#: Checks that are satisfied by a model judgement only.  These are *advisory*
#: and never sufficient on their own to satisfy a required criterion.
ADVISORY_CHECKS = frozenset({CheckKind.LLM_REVIEW, CheckKind.MANUAL})


class Complexity(str, Enum):
    TRIVIAL = "trivial"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AcceptanceCriterion(BaseModel):
    """A single, independently checkable statement of correctness.

    ``kind`` selects the deterministic verifier used; ``spec`` carries the
    verifier-specific parameters.  A criterion is only ``verified`` when the
    verifier ran it and it passed, which is why ``LLM_REVIEW`` is explicitly
    separated from the deterministic kinds.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    kind: CheckKind = CheckKind.FILE_EXISTS
    spec: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    blocking: bool = True
    timeout_s: float | None = Field(default=None, gt=0)

    @field_validator("id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("criterion id must be non-empty")
        return v

    @model_validator(mode="after")
    def _validate_spec(self) -> AcceptanceCriterion:
        required_keys: dict[CheckKind, tuple[str, ...]] = {
            CheckKind.FILE_EXISTS: ("path",),
            CheckKind.FILE_CONTAINS: ("path", "pattern"),
            CheckKind.FILE_NOT_CONTAINS: ("path", "pattern"),
            CheckKind.COMMAND_EXITS_ZERO: ("command",),
            CheckKind.COMMAND_OUTPUT_MATCHES: ("command", "pattern"),
            CheckKind.PYTEST_SELECTOR: ("selector",),
            CheckKind.DIFF_NONEMPTY: (),
            CheckKind.DIFF_EMPTY: (),
            CheckKind.LLM_REVIEW: (),
            CheckKind.MANUAL: (),
        }
        missing = [k for k in required_keys[self.kind] if k not in self.spec]
        if missing:
            raise ValueError(
                f"acceptance criterion {self.id!r} of kind {self.kind.value} "
                f"is missing spec keys: {missing}"
            )
        if self.kind in (CheckKind.COMMAND_EXITS_ZERO, CheckKind.COMMAND_OUTPUT_MATCHES):
            cmd = self.spec.get("command")
            if not isinstance(cmd, (list, tuple)) or not cmd:
                raise ValueError(
                    f"acceptance criterion {self.id!r} needs a non-empty command list"
                )
        return self


class RepoScope(BaseModel):
    """Which parts of the repository a task is allowed to look at and change."""

    model_config = ConfigDict(extra="forbid")

    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    anticipated_files: tuple[str, ...] = ()

    def touched(self, path: str) -> bool:
        """Return whether ``path`` is within scope for this task."""
        import fnmatch
        import posixpath

        rel = posixpath.normpath(path)
        for pattern in self.exclude_paths:
            if fnmatch.fnmatch(rel, pattern) or rel.startswith(pattern.rstrip("/") + "/"):
                return False
        if not self.include_paths:
            return True
        for pattern in self.include_paths:
            if fnmatch.fnmatch(rel, pattern) or rel.startswith(pattern.rstrip("/") + "/"):
                return True
        return False


class Budget(BaseModel):
    """Per-task resource ceilings."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    deadline_s: float | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, ge=1)
    max_attempts: int = Field(default=3, ge=1, le=20)


class TaskSpec(BaseModel):
    """The fully specified, immutable-by-default unit of orchestrated work."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    run_id: str
    parent_id: str | None = None
    title: str
    objective: str
    description: str = ""
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    scope: RepoScope = Field(default_factory=RepoScope)
    required_capabilities: tuple[str, ...] = ()
    role: Role = Role.IMPLEMENTER
    priority: int = Field(default=50, ge=0, le=100)
    complexity: Complexity = Complexity.MEDIUM
    estimated_duration_s: float = Field(default=300.0, gt=0)
    budget: Budget = Field(default_factory=Budget)
    delegation_depth: int = Field(default=0, ge=0)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "run_id", "title", "objective")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("field must be non-empty")
        return v

    @model_validator(mode="after")
    def _validate(self) -> TaskSpec:
        if self.id in self.depends_on:
            raise ValueError(f"task {self.id} cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(f"task {self.id} has duplicate dependencies")
        crit_ids = [c.id for c in self.acceptance_criteria]
        if len(set(crit_ids)) != len(crit_ids):
            raise ValueError(f"task {self.id} has duplicate acceptance criterion ids")
        return self

    @property
    def is_advisory_only(self) -> bool:
        """True when no deterministic acceptance criterion exists.

        Such a task cannot be machine-verified; the orchestrator records this
        explicitly rather than pretending the task was verified.
        """
        return not any(c.kind not in ADVISORY_CHECKS for c in self.acceptance_criteria)


class TaskState(BaseModel):
    """Mutable execution state for a task (the spec above stays immutable)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    run_id: str
    status: TaskStatus = TaskStatus.PENDING
    owner: str | None = None
    attempt: int = 0
    max_attempts: int = 3
    error_class: str | None = None
    error_message: str | None = None
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
    deadline_at: _dt.datetime | None = None
    tokens_used: int = 0
    cost_used_usd: float = 0.0
    artifacts: tuple[str, ...] = ()
    verification: dict[str, Any] | None = None
    updated_at: _dt.datetime = Field(default_factory=utcnow)
    lease_expires_at: _dt.datetime | None = None

    def transition_to(self, target: TaskStatus) -> None:
        assert_transition(self.status, target)
        self.status = target
        self.updated_at = utcnow()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()
