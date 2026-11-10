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


class HttpRequestTool:
    """Perform an outbound HTTP request.

    This tool requires the ``net:http`` capability *and* human approval,
    because an outbound request can exfiltrate repository content.  It is
    the only tool in the set that is allowed to leave the host.
    """

    name = "http_request"
    rule = ToolRule(
        name="http_request",
        required_capabilities=(Capability.NET_HTTP,),
        needs_approval=True,
        approval_action="net.http",
        description="Perform an outbound HTTP request (requires human approval).",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import httpx

        url = arguments.get("url")
        if not url or not isinstance(url, str):
            return ToolResult(ok=False, error="url is required", error_class="validation")
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                error="only http and https URLs are permitted",
                error_class="policy",
            )
        method = str(arguments.get("method") or "GET").upper()
        if method not in ("GET", "POST", "HEAD"):
            return ToolResult(
                ok=False,
                error=f"unsupported method {method!r}",
                error_class="validation",
            )
        timeout = float(arguments.get("timeout_s") or 30.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(method, url)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, error=str(exc), error_class="tool")
        return ToolResult(
            ok=response.status_code < 400,
            content=response.text[:20_000],
            data={
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "method": method,
                "url": url,
            },
            error=None
            if response.status_code < 400
            else f"HTTP {response.status_code}",
            error_class=None if response.status_code < 400 else "tool",
        )


def git_available() -> bool:
    return shutil.which("git") is not None


def worktree_root(path: str | Path) -> Path:
    return Path(path).resolve()
