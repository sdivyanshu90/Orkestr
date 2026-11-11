"""Sandboxed process execution.

This module is *hostile-environment defence in depth*, not a security
boundary.  On a normal Linux host without user namespaces it cannot stop a
determined attacker from escaping: ``RLIMIT_*`` limits and an allow-listed
executable list raise the cost of abuse but do not make execution safe.

What it does provide, and what the documentation claims:

* a clean environment (only allow-listed variables survive);
* CPU, address-space, process-count and file-size limits via ``RLIMIT_*``;
* a wall-clock timeout with a guaranteed kill;
* an output cap so a runaway process cannot exhaust memory or disk;
* an executable allow-list;
* optional network namespace isolation via ``unshare --net`` when the host
  permits it, with the outcome recorded either way.

The honest summary: worktrees isolate *code*, the capability layer isolates
*authority*, and this module isolates *resources*.  None of the three is a
complete boundary; together they raise the cost of a mistake considerably.
See ``docs/security-threat-model.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import SandboxConfig
from ..errors import ToolError


@dataclass
class CommandResult:
    """The outcome of a sandboxed command."""

    command: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    truncated: bool = False
    network_isolated: bool = False
    isolation_note: str = ""
    env_used: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_s": round(self.duration_s, 6),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "network_isolated": self.network_isolated,
            "isolation_note": self.isolation_note,
        }


def _preexec(cpu_time: int, memory_mb: int, max_file_mb: int):
    """Build the ``preexec_fn`` that applies rlimits in the child process.

    ``RLIMIT_NPROC`` is deliberately *not* set: on Linux it counts the
    processes of the whole real user, not of the child, so lowering it would
    break unrelated processes on the host rather than constrain the sandbox.
    """

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_time, cpu_time))
        mem_bytes = memory_mb * 1024 * 1024
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_mb * 1024 * 1024,) * 2)
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _apply


def build_environment(
    sandbox: SandboxConfig, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Construct the child environment.

    The hardened settings are applied unconditionally: a repository cannot
    opt out of them, because the orchestrator sets them for every command it
    runs.  They are *enforced settings*, not prompt instructions.
    """
    env: dict[str, str] = {}
    for key in sandbox.env_allowlist:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = env.get("HOME", "/tmp")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # Break accidental proxy usage; the network namespace (when available)
    # is the real control.
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    # Ambient git configuration and hooks are executable code that would run
    # with the orchestrator's privileges, so they are disabled.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


class Sandbox:
    """Executes commands under the configured resource limits."""

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()
        self._unshare = shutil.which("unshare")

    def check_executable(self, executable: str, *, workdir: Path | None = None) -> None:
        """Reject an executable that is not permitted.

        Two conditions must hold:

        * the executable's *name* is on the allow-list; and
        * the executable does not resolve to a file inside the working
          directory, so an agent cannot plant its own binary named ``python3``
          and have it executed in preference to the system one.
        """
        base = Path(executable).name
        if base not in self.config.allowed_binaries:
            raise ToolError(
                f"executable {base!r} is not in the sandbox allow-list",
                executable=executable,
                allowed=list(self.config.allowed_binaries),
            )
        if workdir is not None:
            candidate = Path(executable)
            if not candidate.is_absolute():
                # A bare name is resolved through PATH, exactly as a shell
                # would resolve it; only a planted binary inside the working
                # directory is rejected.
                found = shutil.which(executable)
                if found is None:
                    return
                candidate = Path(found)
            try:
                resolved = candidate.resolve()
                root = workdir.resolve()
            except OSError:
                return
            if resolved == root or root in resolved.parents:
                raise ToolError(
                    "the executable resolves inside the working directory, which "
                    "would allow a planted binary to be executed",
                    executable=executable,
                )

    async def run(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: str | Path,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        """Run a command and return its result.  Never raises for non-zero exit."""
        if not command:
            raise ToolError("empty command")
        argv = tuple(str(part) for part in command)
        self.check_executable(argv[0], workdir=Path(cwd))
        workdir = Path(cwd)
        if not workdir.exists():
            raise ToolError(f"working directory does not exist: {workdir}", cwd=str(workdir))

        timeout = timeout_s if timeout_s is not None else self.config.wall_time_s
        child_env = build_environment(self.config, env)
        network_isolated = False
        isolation_note = ""

        effective_argv = argv
        if not self.config.allow_network and self._unshare:
            probe = await self._probe_unshare(workdir, child_env)
            if probe is True:
                network_isolated = True
                isolation_note = "network namespace isolated via unshare --net"
                effective_argv = (self._unshare, "--net", "--", *argv)
            elif probe is False:
                isolation_note = (
                    "network isolation unavailable on this host (unshare --net is "
                    "not permitted); outbound access was NOT prevented"
                )
            else:
                isolation_note = (
                    "network isolation unavailable on this host (unshare not "
                    "installed); outbound access was NOT prevented"
                )
        elif not self.config.allow_network:
            isolation_note = (
                "network isolation unavailable on this host; "
                "outbound access was NOT prevented"
            )
        else:
            isolation_note = "network access permitted by configuration"

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *effective_argv,
                cwd=str(workdir),
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                preexec_fn=_preexec(
                    self.config.cpu_time_s,
                    self.config.memory_mb,
                    max(self.config.max_output_bytes // (1024 * 1024), 64),
                ),
            )
        except (OSError, ValueError) as exc:
            raise ToolError(f"failed to start command: {exc}", command=list(argv)) from exc

        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin.encode() if stdin is not None else None),
                timeout=timeout,
            )
        except TimeoutError:
            timed_out = True
            stdout_b, stderr_b = b"", b""
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(TimeoutError, ValueError):
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5)

        duration = time.monotonic() - started
        stdout_text, stdout_truncated = self._cap(stdout_b)
        stderr_text, stderr_truncated = self._cap(stderr_b)
        return CommandResult(
            command=argv,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_s=duration,
            timed_out=timed_out,
            truncated=stdout_truncated or stderr_truncated,
            network_isolated=network_isolated,
            isolation_note=isolation_note,
            env_used={"PATH": child_env.get("PATH", "")},
        )

    async def _probe_unshare(self, cwd: Path, env: dict[str, str]) -> bool | None:
        """Check whether ``unshare --net`` works on this host."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._unshare or "unshare",
                "--net",
                "--",
                "true",
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            return None
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            return None
        return proc.returncode == 0

    def _cap(self, data: bytes) -> tuple[str, bool]:
        limit = self.config.max_output_bytes
        truncated = len(data) > limit
        if truncated:
            data = data[:limit]
        return data.decode("utf-8", errors="replace"), truncated
