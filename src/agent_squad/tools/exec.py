"""Command execution and git tools.

Two properties are enforced here rather than assumed:

* **Repository-local and global git configuration cannot execute.**  A
  repository that ships a ``.git/hooks/pre-commit`` script, or that sets
  ``core.hooksPath`` or an alias in ``.git/config``, would otherwise run
  code with the runner's privileges the moment anybody committed.  Every
  git invocation made here neutralises the ambient configuration, and the
  environment given to *every* command does the same, so a worker that
  runs ``git commit`` itself cannot install a hook either.
* **A command cannot reach outside the worker's authority.**  The command
  is scanned for the paths it would touch and for the protected effects
  it performs, so ``sh -c "rm -rf protected"`` is refused for the same
  reason ``delete_path`` is.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import shutil
from pathlib import Path
from typing import Any

from ..errors import ToolError
from ..sandbox.limits import Sandbox
from ..security.capabilities import Capability
from ..security.policy import ToolRule, scan_command
from .base import ToolContext, ToolResult

#: Git settings that neutralise ambient configuration.
#:
#: ``core.hooksPath`` is the important one: without it, a repository
#: decides which code runs when the runner commits.  The rest close the
#: smaller doors -- a credential helper and an external diff driver are
#: both programs the repository gets to choose.
GIT_HARDENING_ARGS: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
    "-c",
    "diff.external=",
)

#: The environment that stops a command from inheriting git authority.
#:
#: ``GIT_CONFIG_COUNT`` injects configuration at a priority above the
#: repository's own ``.git/config`` and above the user's global config,
#: which is what makes the neutralisation stick.
HARDENED_GIT_ENV: dict[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": os.devnull,
}


def _command_refusal(context: ToolContext, argv: list[str]) -> str | None:
    """The reason a command may not run, or ``None`` when it may."""
    scan = scan_command(argv)
    root = (context.scope_root or context.workdir).resolve()
    for path in scan.referenced_paths:
        if context.is_protected(path):
            return (
                f"the command refers to {path!r}, which is protected "
                "verification material: it is externally owned and outside "
                "the worker's authority"
            )
    for path in scan.write_paths:
        normalised = posixpath.normpath(path)
        for pattern in context.scope_patterns:
            if fnmatch.fnmatch(normalised, pattern) or normalised.startswith(
                pattern.rstrip("/") + "/"
            ):
                return (
                    f"the command would write {path!r}, which is excluded by "
                    "the task scope"
                )
        # A write must stay inside the workspace.  A relative escape
        # (``../outside``) and an absolute path elsewhere are the two ways
        # a command reaches the host, and both are refused here rather
        # than relying on the sandbox to contain them -- the development
        # profile has no filesystem boundary.
        try:
            target = (context.workdir / path).resolve()
        except (OSError, ValueError):
            continue
        if target != root and root not in target.parents:
            return (
                f"the command would write {path!r}, which resolves outside "
                "the task workspace"
            )
    return None


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
        refusal = _command_refusal(context, argv)
        if refusal is not None:
            return ToolResult(ok=False, error=refusal, error_class="policy")
        sandbox = Sandbox(context.sandbox)
        try:
            result = await sandbox.run(argv, cwd=context.workdir, env=HARDENED_GIT_ENV)
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
        argv = ["diff", "--no-color"]
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
        return await _git(context, ["status", "--porcelain"])


async def _git(context: ToolContext, argv: list[str]) -> ToolResult:
    """Run a git command with the ambient configuration neutralised.

    The repository chooses which hooks run and which helpers git calls;
    both are programs, and both would run with the runner's privileges.
    The hardening arguments and the environment together make the
    repository's choice inert.
    """
    sandbox = Sandbox(context.sandbox)
    try:
        result = await sandbox.run(
            ["git", *GIT_HARDENING_ARGS, *argv],
            cwd=context.workdir,
            env=HARDENED_GIT_ENV,
        )
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
        await _git(context, ["add", "-A"])
        result = await _git(context, ["commit", "-m", message])
        if not result.ok:
            return result
        head = await _git(context, ["rev-parse", "HEAD"])
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
        return await _git(context, ["push", remote, branch])


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
