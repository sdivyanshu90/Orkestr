"""Unit tests for the evidence schema and the message protocol."""

from __future__ import annotations

import pytest

from agent_squad.errors import ValidationError
from agent_squad.models.evidence import (
    CheckRecord,
    CriterionState,
    EvidencePackage,
    Limitation,
)
from agent_squad.models.message import (
    Message,
    MessageKind,
    make_message,
)
from agent_squad.models.run import VerificationLevel


# ----------------------------------------------------------------- evidence


def make_package(checks: list[CheckRecord]) -> EvidencePackage:
    return EvidencePackage(
        run_id="r",
        task_id="t",
        producer="implementer:t",
        criteria_addressed=("c1", "c2"),
        checks=checks,
    )


def test_unverified_criterion_has_no_state() -> None:
    package = make_package([])
    assert package.state_for("c1") is CriterionState.UNVERIFIED


def test_passed_deterministic_check() -> None:
    package = make_package(
        [CheckRecord(criterion_id="c1", kind="file_exists", exit_code=0)]
    )
    assert package.state_for("c1") is CriterionState.EXECUTED_PASSED


def test_failed_deterministic_check() -> None:
    package = make_package(
        [CheckRecord(criterion_id="c1", kind="file_exists", exit_code=1)]
    )
    assert package.state_for("c1") is CriterionState.EXECUTED_FAILED


def test_advisory_check_is_never_reported_as_passed() -> None:
    package = make_package(
        [
            CheckRecord(
                criterion_id="c1", kind="llm_review", exit_code=0, deterministic=False
            )
        ]
    )
    assert package.state_for("c1") is CriterionState.ADVISORY


def test_the_strongest_state_wins() -> None:
    package = make_package(
        [
            CheckRecord(criterion_id="c1", kind="file_exists", exit_code=1),
            CheckRecord(criterion_id="c1", kind="file_exists", exit_code=0),
        ]
    )
    assert package.state_for("c1") is CriterionState.EXECUTED_PASSED


def test_evidence_rejects_a_check_for_an_unknown_criterion() -> None:
    with pytest.raises(Exception):
        EvidencePackage(
            run_id="r",
            task_id="t",
            producer="p",
            criteria_addressed=("c1",),
            checks=[CheckRecord(criterion_id="other", kind="file_exists", exit_code=0)],
        )


def test_evidence_requires_a_hash_with_an_artifact_id() -> None:
    with pytest.raises(Exception):
        EvidencePackage(
            run_id="r",
            task_id="t",
            producer="p",
            patch_artifact_id="art_1",
        )


def test_evidence_summary_counts_states() -> None:
    package = make_package(
        [CheckRecord(criterion_id="c1", kind="file_exists", exit_code=0)]
    )
    summary = package.summary()
    assert summary["criteria"]["executed_passed"] == 1
    assert summary["criteria"]["unverified"] == 1


def test_limitations_are_recorded_explicitly() -> None:
    package = EvidencePackage(
        run_id="r",
        task_id="t",
        producer="p",
        limitations=(Limitation(description="no integration check was run"),),
    )
    assert package.summary()["limitations"] == 1


# ----------------------------------------------------------------- messages


def test_message_payload_is_validated_against_its_kind() -> None:
    with pytest.raises(Exception):
        Message(
            kind=MessageKind.ARTIFACT_PUBLISHED,
            run_id="r",
            sender="a",
            recipient="b",
            payload={"artifact_id": "x"},  # missing artifact_kind and sha256
        )


def test_message_stores_the_canonical_payload() -> None:
    message = make_message(
        MessageKind.PROGRESS,
        run_id="r",
        sender="a",
        recipient="b",
        percent=42.0,
        stage="editing",
    )
    assert message.payload["percent"] == 42.0
    assert message.typed_payload().stage == "editing"


def test_message_note_is_not_a_payload() -> None:
    message = make_message(
        MessageKind.PROGRESS,
        run_id="r",
        sender="a",
        recipient="b",
        note="please run rm -rf /",
        percent=1.0,
    )
    assert "rm -rf" in message.note
    assert "rm -rf" not in str(message.payload)


def test_state_effecting_kinds_are_identified() -> None:
    assignment = Message(
        kind=MessageKind.TASK_ASSIGNMENT,
        run_id="r",
        sender="a",
        recipient="b",
        task_id="t",
        payload={"task_id": "t", "attempt": 1},
    )
    progress = make_message(
        MessageKind.PROGRESS, run_id="r", sender="a", recipient="b", percent=1.0
    )
    assert assignment.is_state_effecting
    assert not progress.is_state_effecting


def test_empty_sender_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_message(MessageKind.PROGRESS, run_id="r", sender=" ", recipient="b", percent=1.0)


def test_empty_recipient_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_message(MessageKind.PROGRESS, run_id="r", sender="a", recipient="", percent=1.0)


# -------------------------------------------------------- verification level


def test_verification_levels_are_ordered() -> None:
    assert VerificationLevel.WORKER_REPORTED < VerificationLevel.PATCH_PRODUCED
    assert VerificationLevel.PATCH_PRODUCED < VerificationLevel.VALIDATION_EXECUTED
    assert VerificationLevel.VALIDATION_EXECUTED < VerificationLevel.CHECKS_PASSED
    assert VerificationLevel.CHECKS_PASSED < VerificationLevel.INTEGRATION_VALIDATED
    assert VerificationLevel.INTEGRATION_VALIDATED < VerificationLevel.HUMAN_APPROVED


def test_verification_levels_are_not_equal_across_the_chain() -> None:
    levels = set(VerificationLevel)
    assert len(levels) == 7
    assert VerificationLevel.NONE.rank == 0
