"""Tool protocol and registry.

A tool is a small, typed operation with an explicit capability requirement.
Tools never decide for themselves whether they may run -- the
:class:`~agent_squad.security.policy.PolicyEngine` does, using the rule
registered alongside the tool.

Tool results are returned to the model as text, but the *authoritative*
record of what happened is written to the event log and (for filesystem
changes) to the artifact store.  The model's view is a convenience; the
orchestrator's view is the truth.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import SandboxConfig
from ..errors import ToolError
from ..security.capabilities import CapabilitySet
from ..security.policy import ToolRule


@dataclass
class ToolContext:
    """Everything a tool needs, and nothing it could use to widen its scope."""

    run_id: str
    task_id: str | None
    actor: str
    workdir: Path
    capabilities: CapabilitySet
    sandbox: SandboxConfig
    scope_patterns: tuple[str, ...] = ()
    scope_root: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    protected_paths: tuple[str, ...] = ()
    """Repository-relative paths that are externally owned material.

    The protected material is outside the worker's authority in both
    directions: it cannot be rewritten, and it cannot be read either,
    because a worker that can read the oracle can write a check that
    satisfies it.
    """

    protected_root: Path | None = None
    """Where the staged copy of the protected material lives.

    The staged copy is deliberately *outside* the worktree: the file tools
    resolve every path inside the workdir, so material that lives outside
    it is unreachable without an explicit grant.
    """

    def resolve(self, relative: str) -> Path:
        """Resolve a task-relative path inside the workdir, refusing escapes."""
        if self.is_protected(relative):
            raise ToolError(
                f"path {relative!r} is protected verification material: it is "
                "externally owned and outside the worker's authority",
                path=relative,
            )
        candidate = (self.workdir / relative).resolve()
        root = (self.scope_root or self.workdir).resolve()
        if candidate != root and root not in candidate.parents:
            raise ToolError(
                f"path {relative!r} resolves outside the workspace", path=relative
            )
        return candidate

    def is_protected(self, relative: str) -> bool:
        """Whether a task-relative path is externally owned material."""
        candidate = posixpath.normpath(str(relative))
        for pattern in self.protected_paths:
            if fnmatch.fnmatch(candidate, pattern) or candidate.startswith(
                pattern.rstrip("/") + "/"
            ):
                return True
        if self.protected_root is not None:
            root = str(self.protected_root)
            if candidate.startswith(root.rstrip("/") + "/") or candidate == root:
                return True
        return False


@dataclass
class ToolResult:
    """The outcome of a tool invocation."""

    ok: bool
    content: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content,
            "data": self.data,
            "error": self.error,
            "error_class": self.error_class,
        }


@runtime_checkable
class Tool(Protocol):
    name: str
    rule: ToolRule

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        ...


class ToolRegistry:
    """Name -> tool mapping with the policy rule for each tool."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def rules(self) -> list[ToolRule]:
        return [tool.rule for tool in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
