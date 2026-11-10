"""Command execution and git tools."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..errors import ToolError
from ..sandbox.limits import Sandbox
from ..security.capabilities import Capability
from ..security.policy import ToolRule
from .base import ToolContext, ToolResult


class RunCommandTool:
    """Execute an authorised command inside the task's worktree."""

    name = "run_command"
    rule = ToolRule(
        name="run_command",
        required_capabilities=(Capability.EXEC_RUN,),
        description=(
            "Run a command in the task worktree. The executable must be on the "
            "sandbox allow-list; the command runs with a stripped environment "
            "and resource limits."
        ),
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command = arguments.get("command")
        if isinstance(command, str):
            command = command.split()
        if not command or not isinstance(command, (list, tuple)):
            return ToolResult(
                ok=False, error="command must be a non-empty list", error_class="validation"
            )
        argv = [str(part) for part in command]
        sandbox = Sandbox(context.sandbox)
        try:
            result = await sandbox.run(argv, cwd=context.workdir)
        except ToolError as exc:
            return ToolResult(ok=False, error=exc.message, error_class="tool")
        content = result.stdout
        if result.stderr:
            content = f"{content}\n[stderr]\n{result.stderr}"
        data = result.to_dict()
        # The isolation note travels with the result so that the fact that
        # outbound access was *not* prevented is recorded, not just held in
        # memory.
        data["isolation_note"] = result.isolation_note
        return ToolResult(
            ok=result.ok,
            content=content,
            data=data,
            error=None
            if result.ok
            else (
                f"command exited with {result.exit_code}"
                + (" (timed out)" if result.timed_out else "")
            ),
            error_class=None if result.ok else "tool",
        )


class GitDiffTool:
    name = "git_diff"
    rule = ToolRule(
        name="git_diff",
        required_capabilities=(Capability.EXEC_GIT,),
        description="Show the working tree diff (optionally limited to a path).",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        argv = ["git", "diff", "--no-color"]
        path = arguments.get("path")
        if path:
            argv.extend(["--", str(path)])
        result = await _git(context, argv)
        return result


class GitStatusTool:
    name = "git_status"
    rule = ToolRule(
        name="git_status",
        required_capabilities=(Capability.EXEC_GIT,),
        description="Show the porcelain status of the working tree.",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return await _git(context, ["git", "status", "--porcelain"])


async def _git(context: ToolContext, argv: list[str]) -> ToolResult:
    sandbox = Sandbox(context.sandbox)
    try:
        result = await sandbox.run(argv, cwd=context.workdir)
    except ToolError as exc:
        return ToolResult(ok=False, error=exc.message, error_class="tool")
    return ToolResult(
        ok=result.ok,
        content=result.stdout or result.stderr,
        data=result.to_dict(),
        error=None if result.ok else f"git exited with {result.exit_code}",
        error_class=None if result.ok else "tool",
    )


class CommitChangesTool:
    name = "commit_changes"
    rule = ToolRule(
        name="commit_changes",
        required_capabilities=(Capability.GIT_COMMIT,),
        needs_approval=True,
        approval_action="git.commit",
        description="Create a git commit in the task worktree (requires human approval).",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        message = str(arguments.get("message") or "agent-squad change")
        await _git(context, ["git", "add", "-A"])
        result = await _git(context, ["git", "commit", "-m", message])
        if not result.ok:
            return result
        head = await _git(context, ["git", "rev-parse", "HEAD"])
        return ToolResult(
            ok=True,
            content=result.content,
            data={**result.data, "head": head.content.strip()},
        )


class PushChangesTool:
    name = "push_changes"
    rule = ToolRule(
        name="push_changes",
        required_capabilities=(Capability.GIT_PUSH,),
        needs_approval=True,
        approval_action="git.push",
        description="Push the task branch to its remote (requires human approval).",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        remote = str(arguments.get("remote") or "origin")
        branch = str(arguments.get("branch") or "HEAD")
        return await _git(context, ["git", "push", remote, branch])


def git_available() -> bool:
    return shutil.which("git") is not None


def worktree_root(path: str | Path) -> Path:
    return Path(path).resolve()
