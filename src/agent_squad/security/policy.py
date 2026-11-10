"""Policy engine.

Every tool invocation passes through :meth:`PolicyEngine.authorize` before it
runs.  The engine answers three questions:

1. **Capability** -- does the caller's capability set permit the required
   capabilities of this tool?
2. **Scope**      -- is the operation confined to the paths the task is
   allowed to touch?
3. **Authority**  -- does the operation require explicit human approval
   (network access, commits, pushes, deletions, protected paths)?

Only after all three answers are "yes" may the tool run.  The decision and
its reasoning are recorded in the audit log, including denials, because a
denial is exactly the event an operator needs to see.

The engine never reads model output to decide what is allowed.  It reads the
*declared intent* of a tool call and the *static* policy configuration.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..config import ApprovalConfig, SandboxConfig
from ..errors import PolicyError
from .capabilities import Capability, CapabilitySet


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str = ""
    required_approval: bool = False
    action: str = ""
    capabilities: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "required_approval": self.required_approval,
            "action": self.action,
            "capabilities": list(self.capabilities),
        }


@dataclass
class ToolRule:
    """The capabilities a tool requires and whether it needs human approval."""

    name: str
    required_capabilities: tuple[str, ...]
    needs_approval: bool = False
    approval_action: str = ""
    path_argument: str | None = None
    description: str = ""


DEFAULT_TOOL_RULES: tuple[ToolRule, ...] = (
    ToolRule(
        "read_file",
        (Capability.FS_READ,),
        path_argument="path",
        description="Read a file from the task scope.",
    ),
    ToolRule(
        "list_dir",
        (Capability.FS_READ,),
        path_argument="path",
        description="List a directory inside the task scope.",
    ),
    ToolRule(
        "write_file",
        (Capability.FS_WRITE,),
        path_argument="path",
        description="Create or overwrite a file inside the task scope.",
    ),
    ToolRule(
        "run_command",
        (Capability.EXEC_RUN,),
        description="Execute an authorised command in the sandbox.",
    ),
    ToolRule(
        "git_diff",
        (Capability.EXEC_GIT,),
        description="Show the working tree diff.",
    ),
    ToolRule(
        "git_status",
        (Capability.EXEC_GIT,),
        description="Show the working tree status.",
    ),
    ToolRule(
        "commit_changes",
        (Capability.GIT_COMMIT,),
        needs_approval=True,
        approval_action="git.commit",
        description="Create a git commit (requires human approval).",
    ),
    ToolRule(
        "push_changes",
        (Capability.GIT_PUSH,),
        needs_approval=True,
        approval_action="git.push",
        description="Push to a remote (requires human approval).",
    ),
    ToolRule(
        "http_request",
        (Capability.NET_HTTP,),
        needs_approval=True,
        approval_action="net.http",
        description="Perform an outbound HTTP request (requires human approval).",
    ),
    ToolRule(
        "delete_path",
        (Capability.FS_DELETE,),
        needs_approval=True,
        approval_action="fs.delete",
        path_argument="path",
        description="Delete a path (requires human approval).",
    ),
    ToolRule(
        "emit_artifact",
        (Capability.ARTIFACT_WRITE,),
        description="Publish a structured artifact to the artifact store.",
    ),
    ToolRule(
        "request_approval",
        (Capability.APPROVAL_REQUEST,),
        description="Ask a human to authorise a protected action.",
    ),
)


@dataclass
class AuditRecord:
    run_id: str
    task_id: str | None
    actor: str
    tool: str
    arguments: dict[str, Any]
    decision: Decision
    reason: str
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "actor": self.actor,
            "tool": self.tool,
            "arguments": self.arguments,
            "decision": self.decision.value,
            "reason": self.reason,
            "approval_id": self.approval_id,
        }


class PolicyEngine:
    """Authorises tool invocations against capabilities, scope and approval rules."""

    def __init__(
        self,
        *,
        sandbox: SandboxConfig | None = None,
        approval: ApprovalConfig | None = None,
        tool_rules: tuple[ToolRule, ...] | None = None,
    ) -> None:
        self.sandbox = sandbox or SandboxConfig()
        self.approval = approval or ApprovalConfig()
        self._rules: dict[str, ToolRule] = {r.name: r for r in (tool_rules or DEFAULT_TOOL_RULES)}
        self._audit: list[AuditRecord] = []

    # ---------------------------------------------------------------- rules

    def register_tool(self, rule: ToolRule) -> None:
        self._rules[rule.name] = rule

    def rule_for(self, tool: str) -> ToolRule | None:
        return self._rules.get(tool)

    # ----------------------------------------------------------- authorising

    def authorize(
        self,
        *,
        run_id: str,
        actor: str,
        capabilities: CapabilitySet,
        tool: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
        scope_root: str | None = None,
        scope_patterns: tuple[str, ...] = (),
        approved: bool = False,
    ) -> PolicyDecision:
        """Decide whether ``tool`` may run."""
        rule = self._rules.get(tool)
        if rule is None:
            decision = PolicyDecision(
                decision=Decision.DENY,
                reason=f"tool {tool!r} is not registered in the policy",
            )
            self._record(run_id, task_id, actor, tool, arguments, decision)
            return decision

        missing = capabilities.missing(rule.required_capabilities)
        if missing:
            decision = PolicyDecision(
                decision=Decision.DENY,
                reason=(
                    f"missing required capabilities: {', '.join(missing)} "
                    f"(required: {', '.join(rule.required_capabilities)})"
                ),
                capabilities=rule.required_capabilities,
            )
            self._record(run_id, task_id, actor, tool, arguments, decision)
            return decision

        path_decision = self._check_path(rule, arguments, scope_root, scope_patterns)
        if path_decision is not None:
            self._record(run_id, task_id, actor, tool, arguments, path_decision)
            return path_decision

        needs_approval, action = self._requires_approval(rule, arguments)
        if needs_approval and not approved:
            decision = PolicyDecision(
                decision=Decision.REQUIRE_APPROVAL,
                reason=f"action {action!r} requires explicit human approval",
                required_approval=True,
                action=action,
                capabilities=rule.required_capabilities,
            )
            self._record(run_id, task_id, actor, tool, arguments, decision)
            return decision

        decision = PolicyDecision(
            decision=Decision.ALLOW,
            reason="authorised",
            capabilities=rule.required_capabilities,
        )
        self._record(run_id, task_id, actor, tool, arguments, decision)
        return decision

    def _check_path(
        self,
        rule: ToolRule,
        arguments: dict[str, Any],
        scope_root: str | None,
        scope_patterns: tuple[str, ...],
    ) -> PolicyDecision | None:
        if not rule.path_argument:
            return None
        raw = arguments.get(rule.path_argument)
        if raw is None:
            return None
        path = str(raw)
        normalised = posixpath.normpath(path)
        if normalised.startswith("..") or normalised.startswith("/"):
            return PolicyDecision(
                decision=Decision.DENY,
                reason=f"path {path!r} escapes the permitted scope",
            )
        if scope_root is not None:
            resolved = posixpath.normpath(posixpath.join(scope_root, normalised))
            if not resolved.startswith(posixpath.normpath(scope_root)):
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"path {path!r} escapes the workspace root",
                )
        for pattern in scope_patterns:
            if fnmatch.fnmatch(normalised, pattern) or normalised.startswith(
                pattern.rstrip("/") + "/"
            ):
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"path {path!r} is excluded by the task scope",
                )
        for protected in self.approval.protected_paths:
            if fnmatch.fnmatch(normalised, protected) or normalised.startswith(
                protected.rstrip("/") + "/"
            ):
                return PolicyDecision(
                    decision=Decision.REQUIRE_APPROVAL,
                    reason=f"path {path!r} is protected and requires human approval",
                    required_approval=True,
                    action=f"fs.protected:{normalised}",
                )
        return None

    def _requires_approval(
        self, rule: ToolRule, arguments: dict[str, Any]
    ) -> tuple[bool, str]:
        if rule.needs_approval:
            action = rule.approval_action or rule.name
            if action == "git.commit" and not self.approval.require_approval_for_git_commit:
                return False, action
            if action == "git.push" and not self.approval.require_approval_for_git_push:
                return False, action
            if action == "net.http" and not self.approval.require_approval_for_network:
                return False, action
            if action == "fs.delete" and not self.approval.require_approval_for_delete:
                return False, action
            return True, action
        if rule.name == "run_command":
            command = arguments.get("command") or []
            if isinstance(command, str):
                command = command.split()
            executable = str(command[0]) if command else ""
            if executable == "git":
                subcommands = [str(c) for c in command[1:]]
                if "push" in subcommands and self.approval.require_approval_for_git_push:
                    return True, "git.push"
                if "commit" in subcommands and self.approval.require_approval_for_git_commit:
                    return True, "git.commit"
        return False, ""

    # ---------------------------------------------------------------- audit

    def _record(
        self,
        run_id: str,
        task_id: str | None,
        actor: str,
        tool: str,
        arguments: dict[str, Any],
        decision: PolicyDecision,
    ) -> None:
        self._audit.append(
            AuditRecord(
                run_id=run_id,
                task_id=task_id,
                actor=actor,
                tool=tool,
                arguments=_redact(arguments),
                decision=decision.decision,
                reason=decision.reason,
            )
        )

    def audit_records(self) -> list[AuditRecord]:
        return list(self._audit)

    def denials(self) -> list[AuditRecord]:
        return [r for r in self._audit if r.decision is Decision.DENY]


class PolicyDeniedError(PolicyError):
    """Raised when the policy engine refuses a tool invocation."""


_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "authorization",
    "private_key",
    "credential",
)


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    """Redact obviously sensitive argument values before they reach the audit log."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            out[key] = "***redacted***"
        elif isinstance(value, str) and len(value) > 2000:
            out[key] = value[:2000] + "...[truncated]"
        else:
            out[key] = value
    return out
