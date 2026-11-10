"""Typed inter-agent message protocol.

Design rules encoded here:

* A message is *data*, never an instruction.  Free-form text lives in a
  dedicated ``note`` field that is never parsed for commands; executable
  content must be expressed as one of the typed payloads below.
* Payloads are validated by a discriminated union so a malformed or
  incompatible payload is rejected at the boundary.
* Every message carries provenance: sender, recipient, run/task reference,
  a version and a correlation id.
* Messages that would mutate execution state carry an explicit ``state_effect``
  that the recipient must still authorise through the capability layer.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ValidationError
from ..ids import message_id
from .task import utcnow


class MessageKind(str, Enum):
    TASK_ASSIGNMENT = "task_assignment"
    PROGRESS = "progress"
    HANDOFF = "handoff"
    ARTIFACT_PUBLISHED = "artifact_published"
    VERIFICATION_REQUEST = "verification_request"
    FAILURE_REPORT = "failure_report"
    REVISION_REQUEST = "revision_request"
    CANCELLATION = "cancellation"
    ESCALATION = "escalation"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_DECISION = "approval_decision"


#: Kinds that are allowed to influence the scheduler's state machine.
STATE_EFFECTING_KINDS = frozenset(
    {
        MessageKind.TASK_ASSIGNMENT,
        MessageKind.ARTIFACT_PUBLISHED,
        MessageKind.VERIFICATION_REQUEST,
        MessageKind.REVISION_REQUEST,
        MessageKind.CANCELLATION,
        MessageKind.ESCALATION,
        MessageKind.APPROVAL_DECISION,
    }
)

#: Kinds that are purely informational.  They are recorded and surfaced but
#: they can never move a task's status.
INFORMATIONAL_KINDS = frozenset({MessageKind.PROGRESS, MessageKind.HANDOFF})


class TaskAssignmentPayload(BaseModel):
    """The supervisor's assignment of a task to a worker.

    The task itself is identified by the message's ``task_id`` field, so
    the payload does not repeat it; duplicating it would create two
    sources of truth that could disagree.
    """

    model_config = ConfigDict(extra="forbid")
    attempt_number: int = Field(default=1, ge=1)
    workdir: str | None = None
    base_revision: str | None = None
    objective: str = ""


class ProgressPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    percent: float = Field(default=0.0, ge=0.0, le=100.0)
    stage: str = ""
    completed_criteria: tuple[str, ...] = ()


class HandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    artifact_ids: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()


class ArtifactPublishedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    artifact_kind: str
    sha256: str


class VerificationRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_ids: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    workdir: str | None = None


class FailureReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error_class: str
    message: str
    retryable: bool = False
    attempt_number: int = Field(default=1, ge=1)


class RevisionRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    criteria: tuple[str, ...] = ()
    requested_changes: tuple[str, ...] = ()


class CancellationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    scope: Literal["task", "run"] = "task"


class EscalationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    severity: Literal["low", "medium", "high"] = "medium"
    details: str = ""


class ApprovalRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    rationale: str
    arguments: tuple[str, ...] = ()
    risk: Literal["low", "medium", "high"] = "medium"


class ApprovalDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    approved: bool
    decided_by: str
    reason: str = ""


_PAYLOAD_MODELS: dict[MessageKind, type[BaseModel]] = {
    MessageKind.TASK_ASSIGNMENT: TaskAssignmentPayload,
    MessageKind.PROGRESS: ProgressPayload,
    MessageKind.HANDOFF: HandoffPayload,
    MessageKind.ARTIFACT_PUBLISHED: ArtifactPublishedPayload,
    MessageKind.VERIFICATION_REQUEST: VerificationRequestPayload,
    MessageKind.FAILURE_REPORT: FailureReportPayload,
    MessageKind.REVISION_REQUEST: RevisionRequestPayload,
    MessageKind.CANCELLATION: CancellationPayload,
    MessageKind.ESCALATION: EscalationPayload,
    MessageKind.APPROVAL_REQUEST: ApprovalRequestPayload,
    MessageKind.APPROVAL_DECISION: ApprovalDecisionPayload,
}


class Message(BaseModel):
    """A single typed inter-agent message."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=message_id)
    kind: MessageKind
    version: int = Field(
        default=0,
        ge=0,
        description=(
            "0 means 'unversioned': the bus assigns the next version in the "
            "conversation.  A positive version is an explicit claim to "
            "supersede that version, and is rejected if it is not newer."
        ),
    )
    run_id: str
    sender: str
    recipient: str
    task_id: str | None = None
    correlation_id: str | None = None
    created_at: _dt.datetime = Field(default_factory=utcnow)
    note: str = Field(
        default="",
        description="Free-form annotation. Never parsed for instructions.",
    )
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_payload(self) -> Message:
        model = _PAYLOAD_MODELS[self.kind]
        try:
            validated = model.model_validate(self.payload)
        except Exception as exc:
            raise ValueError(
                f"invalid payload for message kind {self.kind.value}: {exc}"
            ) from exc
        # Store the *validated* payload so downstream code sees canonical data.
        object.__setattr__(self, "payload", validated.model_dump(mode="json"))
        return self

    @property
    def is_state_effecting(self) -> bool:
        return self.kind in STATE_EFFECTING_KINDS

    def typed_payload(self) -> BaseModel:
        model = _PAYLOAD_MODELS[self.kind]
        return model.model_validate(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def make_message(
    kind: MessageKind,
    *,
    run_id: str,
    sender: str,
    recipient: str,
    task_id: str | None = None,
    correlation_id: str | None = None,
    note: str = "",
    **payload: Any,
) -> Message:
    """Build a validated message of the given kind."""
    if not sender.strip():
        raise ValidationError("message sender must be non-empty")
    if not recipient.strip():
        raise ValidationError("message recipient must be non-empty")
    return Message(
        kind=kind,
        run_id=run_id,
        sender=sender,
        recipient=recipient,
        task_id=task_id,
        correlation_id=correlation_id,
        note=note,
        payload=payload,
    )
