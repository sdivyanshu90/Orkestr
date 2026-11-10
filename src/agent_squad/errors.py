"""Explicit failure taxonomy.

Every failure mode in the orchestrator is represented by a dedicated exception
type.  Generic ``Exception`` raising is reserved for truly exceptional
conditions (programming errors), never for expected operational failures.

The taxonomy exists so that:

* the scheduler can map errors onto retry / fail-fast / escalate policies;
* the event store can persist the failure class alongside the transition;
* the benchmark harness can attribute failures to categories rather than
  treating every non-zero exit as the same thing.
"""

from __future__ import annotations

from enum import Enum


class ErrorClass(str, Enum):
    """Stable, machine-readable classification of a failure.

    The string values are persisted in the event store; they are part of the
    on-disk contract and must remain stable across releases.
    """

    VALIDATION = "validation"
    """Input or model output failed schema / business-rule validation."""

    POLICY = "policy"
    """Capability check or policy gate denied an operation."""

    BUDGET = "budget"
    """A token, cost, time or iteration budget was exhausted."""

    MODEL = "model"
    """The model provider failed or returned an unusable response."""

    TOOL = "tool"
    """A tool invocation failed (non-zero exit, IO error, timeout)."""

    CONFLICT = "conflict"
    """Integration conflict detected between independently produced patches."""

    VERIFICATION = "verification"
    """Acceptance checks failed."""

    CANCELLED = "cancelled"
    """The operation was cancelled cooperatively."""

    TIMEOUT = "timeout"
    """A deadline elapsed before the operation completed."""

    STALE = "stale"
    """An update was rejected because it referenced an outdated attempt."""

    INFRASTRUCTURE = "infrastructure"
    """Local runtime failure (git, sqlite, filesystem)."""

    UNKNOWN = "unknown"


class AgentSquadError(Exception):
    """Base class for every error raised by the orchestrator."""

    error_class: ErrorClass = ErrorClass.UNKNOWN
    retryable: bool = False

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, object] = dict(context)

    def to_dict(self) -> dict[str, object]:
        return {
            "error_class": self.error_class.value,
            "message": self.message,
            "retryable": self.retryable,
            "context": {k: _jsonable(v) for k, v in self.context.items()},
        }


def _jsonable(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)


class ValidationError(AgentSquadError):
    error_class = ErrorClass.VALIDATION


class PolicyError(AgentSquadError):
    error_class = ErrorClass.POLICY
    retryable = False


class BudgetExceeded(AgentSquadError):
    error_class = ErrorClass.BUDGET


class ModelError(AgentSquadError):
    error_class = ErrorClass.MODEL


class TransientModelError(ModelError):
    """A model failure that is likely to succeed if retried."""

    retryable = True


class ToolError(AgentSquadError):
    error_class = ErrorClass.TOOL


class ConflictError(AgentSquadError):
    error_class = ErrorClass.CONFLICT


class VerificationError(AgentSquadError):
    error_class = ErrorClass.VERIFICATION


class CancelledError(AgentSquadError):
    error_class = ErrorClass.CANCELLED


class TimeoutError_(AgentSquadError):
    error_class = ErrorClass.TIMEOUT
    retryable = True


class StaleAttemptError(AgentSquadError):
    """Raised when a worker tries to update state for an outdated attempt."""

    error_class = ErrorClass.STALE


class InfrastructureError(AgentSquadError):
    error_class = ErrorClass.INFRASTRUCTURE
    retryable = True


class DependencyError(AgentSquadError):
    """A required dependency is missing, circular, or unsatisfiable."""

    error_class = ErrorClass.VALIDATION
