"""Unit tests for the capability and policy layer."""

from __future__ import annotations

import pytest

from agent_squad.config import ApprovalConfig, SandboxConfig
from agent_squad.models.task import Role
from agent_squad.security.capabilities import (
    ALL_CAPABILITIES,
    Capability,
    CapabilitySet,
    capabilities_for_role,
)
from agent_squad.security.policy import Decision, PolicyEngine, ToolRule


@pytest.fixture()
def engine() -> PolicyEngine:
    return PolicyEngine(
        sandbox=SandboxConfig(),
        approval=ApprovalConfig(protected_paths=("infra/",)),
    )


def reviewer_caps() -> CapabilitySet:
    return capabilities_for_role(Role.REVIEWER)


def implementer_caps() -> CapabilitySet:
    return capabilities_for_role(Role.IMPLEMENTER)


def test_reviewer_cannot_write() -> None:
    caps = reviewer_caps()
    assert not caps.allows(Capability.FS_WRITE)
    assert caps.allows(Capability.FS_READ)


def test_capability_set_missing_reports_the_gap() -> None:
    caps = CapabilitySet.of({Capability.FS_READ})
    assert caps.missing([Capability.FS_READ, Capability.FS_WRITE]) == [Capability.FS_WRITE]


def test_capability_set_without_removes() -> None:
    caps = CapabilitySet.of({Capability.FS_READ, Capability.FS_WRITE})
    assert not caps.without(Capability.FS_WRITE).allows(Capability.FS_WRITE)


def test_all_capabilities_are_known() -> None:
    for role in Role:
        for capability in capabilities_for_role(role):
            assert capability in ALL_CAPABILITIES


def test_unknown_capabilities_are_rejected() -> None:
    with pytest.raises(ValueError):
        capabilities_for_role(Role.IMPLEMENTER, extra=["fs:teleport"])


def test_unknown_tool_is_denied(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="not_a_tool",
        arguments={},
    )
    assert decision.decision is Decision.DENY
    assert "not registered" in decision.reason


def test_missing_capability_is_denied(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=reviewer_caps(),
        tool="write_file",
        arguments={"path": "a.py"},
    )
    assert decision.decision is Decision.DENY
    assert Capability.FS_WRITE in decision.reason


def test_permitted_tool_is_allowed(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="write_file",
        arguments={"path": "a.py"},
    )
    assert decision.decision is Decision.ALLOW


def test_path_traversal_is_denied(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="write_file",
        arguments={"path": "../escape.py"},
    )
    assert decision.decision is Decision.DENY
    assert "escapes" in decision.reason


def test_absolute_path_is_denied(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="write_file",
        arguments={"path": "/etc/passwd"},
    )
    assert decision.decision is Decision.DENY


def test_excluded_path_is_denied(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="write_file",
        arguments={"path": "vendor/lib.py"},
        scope_patterns=("vendor/",),
    )
    assert decision.decision is Decision.DENY
    assert "excluded" in decision.reason


def test_protected_path_requires_approval(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="write_file",
        arguments={"path": "infra/main.tf"},
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.required_approval


def test_push_requires_approval(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="push_changes",
        arguments={},
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.action == "git.push"


def test_approved_push_is_allowed(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="push_changes",
        arguments={},
        approved=True,
    )
    assert decision.decision is Decision.ALLOW


def test_git_push_inside_run_command_requires_approval(engine: PolicyEngine) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="run_command",
        arguments={"command": ["git", "push", "origin", "main"]},
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.action == "git.push"


def test_git_status_inside_run_command_does_not_require_approval(
    engine: PolicyEngine,
) -> None:
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="run_command",
        arguments={"command": ["git", "status"]},
    )
    assert decision.decision is Decision.ALLOW


def test_approval_can_be_disabled_by_configuration() -> None:
    engine = PolicyEngine(
        approval=ApprovalConfig(require_approval_for_git_push=False)
    )
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="push_changes",
        arguments={},
    )
    assert decision.decision is Decision.ALLOW


def test_denials_are_recorded_in_the_audit_log(engine: PolicyEngine) -> None:
    engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=reviewer_caps(),
        tool="write_file",
        arguments={"path": "a.py"},
    )
    denials = engine.denials()
    assert len(denials) == 1
    assert denials[0].tool == "write_file"
    assert denials[0].decision is Decision.DENY


def test_audit_records_redact_secret_looking_arguments(engine: PolicyEngine) -> None:
    engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="run_command",
        arguments={"command": ["echo"], "api_key": "sk-abcdefghijklmnop"},
    )
    record = engine.audit_records()[-1]
    assert record.arguments["api_key"] == "***redacted***"


def test_custom_tool_rules_can_be_registered(engine: PolicyEngine) -> None:
    engine.register_tool(
        ToolRule(
            name="danger",
            required_capabilities=(Capability.FS_WRITE,),
            needs_approval=True,
            approval_action="custom.danger",
        )
    )
    decision = engine.authorize(
        run_id="r",
        actor="agent",
        capabilities=implementer_caps(),
        tool="danger",
        arguments={},
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.action == "custom.danger"


def test_review_role_has_no_write_tool_privilege_in_the_default_set() -> None:
    # The reviewer capability set is the authority; the tool registry removes
    # the write tool entirely for reviewers, and this test asserts the
    # capability layer independently denies it too.
    assert Capability.FS_WRITE not in capabilities_for_role(Role.REVIEWER)
