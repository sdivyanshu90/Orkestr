"""Hardened execution containment.

The sandbox in :mod:`agent_squad.sandbox.limits` is *resource* isolation:
CPU, memory, wall-clock and output caps.  It is explicitly **not** a
security boundary, and it says so.  This module is the other half: a
*containment* boundary that restricts what a sandboxed process can see
and reach.

The mechanism is a Linux user namespace.  Entering one is what makes the
following enforceable:

* a fresh PID namespace, so every descendant of the command is inside
  the same namespace and dies with it -- a process cannot escape into
  the host's process table;
* a fresh network namespace with no interfaces except loopback, so a
  denied endpoint is genuinely unreachable rather than merely
  discouraged by an environment variable;
* a filesystem assembled from scratch, in which the system is read-only
  and the only writable places are the task's own worktree and a private
  home directory that the run creates for the purpose.

``bubblewrap`` (``bwrap``) is the practical way to enter such a
namespace without root.  ``unshare`` can isolate the network and PID
namespaces, but it cannot assemble a filesystem, so it provides a
strictly weaker profile and is reported as such.

Two rules govern this module:

1. **Nothing is claimed that is not demonstrated.**  A backend is
   available because a probe *executed* inside it and observed the
   boundary, not because a binary exists on ``PATH``.
2. **The probe runs the real command path.**  The probe uses the same
   argument builder as a worker command, so a successful probe is
   evidence about the thing workers actually run.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: The backend this module prefers.
BUBBLEWRAP = "bubblewrap"

#: The backend used when bubblewrap is unavailable.  It provides a
#: weaker boundary and says so in its capability report.
UNSHARE = "unshare"

#: Directories that make up a usable read-only system image.  Each entry
#: is bound read-only *only* if it exists on the host and is not a
#: symlink into another entry (which would make the mount fail).
_SYSTEM_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc",
    "/opt",
    "/srv",
)

#: The host's home directory is never bound in: exposing it would give a
#: sandboxed process read access to the user's credentials and dotfiles.
#: The private home is created inside the run's own directory instead.
_HOST_HOME = "/home"


@dataclass(frozen=True)
class ContainmentCapabilities:
    """What a backend can actually do on this host.

    Every field is the result of a *probe*, not an inspection of the
    binary's help text.
    """

    backend: str
    available: bool
    network_isolated: bool = False
    pid_isolated: bool = False
    filesystem_isolated: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "available": self.available,
            "network_isolated": self.network_isolated,
            "pid_isolated": self.pid_isolated,
            "filesystem_isolated": self.filesystem_isolated,
            "detail": self.detail,
        }


@dataclass
class ContainmentReport:
    """The result of probing a backend with a real command."""

    backend: str
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    detail: str = ""
    observations: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "ok": self.ok,
            "checks": dict(self.checks),
            "detail": self.detail,
            "observations": dict(self.observations),
        }


def find_backend() -> str | None:
    """The containment backend available on this host, if any."""
    if shutil.which("bwrap"):
        return BUBBLEWRAP
    if shutil.which("unshare"):
        return UNSHARE
    return None


def _exists_and_not_symlink(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _ancestors(path: Path) -> list[Path]:
    """The directories that must exist for ``path`` to exist."""
    parts = path.parts
    out: list[Path] = []
    current = Path(parts[0]) if parts else Path("/")
    for part in parts[1:]:
        current = current / part
        out.append(current)
    return out


def build_bwrap_argv(
    command: Sequence[str],
    *,
    workspace: Path,
    private_home: Path,
    env: Mapping[str, str],
    extra_read_write: Sequence[Path] = (),
    extra_read_only: Sequence[Path] = (),
) -> list[str]:
    """Build the ``bwrap`` argument vector for one command.

    The resulting environment has:

    * a read-only system image (``/usr``, ``/etc``, ...) so that a
      sandboxed command cannot modify the host;
    * a ``tmpfs`` at ``/tmp`` so scratch space is isolated and bounded;
    * the task's worktree bound read-write;
    * a private home directory bound read-write, and **no** access to the
      invoking user's home;
    * a fresh PID namespace, so no descendant escapes cleanup authority;
    * an empty network namespace.
    """
    argv: list[str] = [
        "bwrap",
        "--unshare-user",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--new-session",
        "--die-with-parent",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]

    for path in _SYSTEM_PATHS:
        if os.path.islink(path):
            # On a merged-/usr system the compatibility directories are
            # symlinks (``/lib64 -> /usr/lib64``).  They are recreated as
            # symlinks rather than skipped: the dynamic loader is reached
            # through them, and a missing interpreter makes every binary
            # look absent.
            target = os.path.realpath(path)
            if os.path.isdir(target):
                argv += ["--symlink", target, path]
        elif os.path.isdir(path):
            argv += ["--ro-bind", path, path]

    # A writable scratch area, isolated from the host's /tmp.
    argv += ["--tmpfs", "/tmp"]

    # The invoking user's home is hidden, not merely read-only: a
    # sandboxed command must not be able to read the host's canaries or
    # dotfiles at all.
    argv += ["--dir", _HOST_HOME]

    # The private home: a fresh directory the run owns.
    for parent in _ancestors(private_home.parent):
        argv += ["--dir", str(parent)]
    argv += ["--bind", str(private_home), str(private_home)]

    # The worktree, and anything else the run explicitly exposes.
    for parent in _ancestors(workspace.parent):
        argv += ["--dir", str(parent)]
    argv += ["--bind", str(workspace), str(workspace)]
    for path in extra_read_write:
        argv += ["--bind", str(path), str(path)]
    for path in extra_read_only:
        argv += ["--ro-bind", str(path), str(path)]

    # A clean environment: nothing is inherited implicitly.
    argv += ["--clearenv"]
    for key, value in sorted(env.items()):
        argv += ["--setenv", key, str(value)]
    argv += ["--chdir", str(workspace)]
    argv += ["--"]
    argv += [str(part) for part in command]
    return argv


def build_unshare_argv(
    command: Sequence[str],
    *,
    workspace: Path,
    env: Mapping[str, str],
) -> list[str]:
    """Build an ``unshare`` argument vector.

    This is the fallback.  It isolates the network and the PID namespace,
    but it does **not** assemble a filesystem: the command still sees the
    host's directories, read-write.  The capability report says so, and
    the hardened profile refuses a backend that cannot isolate the
    filesystem.
    """
    argv: list[str] = ["unshare", "--net", "--pid", "--fork", "--mount-proc", "--"]
    argv += [str(part) for part in command]
    return argv


def build_argv(
    backend: str,
    command: Sequence[str],
    *,
    workspace: Path,
    private_home: Path,
    env: Mapping[str, str],
    extra_read_write: Sequence[Path] = (),
    extra_read_only: Sequence[Path] = (),
) -> list[str]:
    """Build the argument vector for the given backend."""
    if backend == BUBBLEWRAP:
        return build_bwrap_argv(
            command,
            workspace=workspace,
            private_home=private_home,
            env=env,
            extra_read_write=extra_read_write,
            extra_read_only=extra_read_only,
        )
    return build_unshare_argv(command, workspace=workspace, env=env)


#: The probe program.  It reports what the command *observed* rather
#: than what the configuration claimed, so a probe failure is a real
#: observation and not an assumption.
_PROBE_SOURCE = r"""
import json, os, socket, sys

report = {"home": os.environ.get("HOME", ""), "uid": os.getuid()}
canary = sys.argv[1] if len(sys.argv) > 1 else ""
if canary:
    try:
        with open(canary, "rb") as handle:
            report["canary_read"] = handle.read(64).decode("utf-8", "replace")
    except OSError as exc:
        report["canary_read"] = None
        report["canary_error"] = type(exc).__name__
else:
    report["canary_read"] = None
try:
    sock = socket.create_connection(("1.1.1.1", 80), timeout=3)
    sock.close()
    report["network"] = "reachable"
except OSError as exc:
    report["network"] = "unreachable"
    report["network_error"] = type(exc).__name__
try:
    with open("/usr/agent-squad-probe-canary", "w") as handle:
        handle.write("x")
    report["system_writable"] = True
except OSError:
    report["system_writable"] = False
try:
    with open("/agent-squad-should-not-exist", "w") as handle:
        handle.write("x")
    report["root_writable"] = True
except OSError:
    report["root_writable"] = False
sys.stdout.write(json.dumps(report))
"""


async def probe(
    backend: str,
    *,
    workspace: Path,
    private_home: Path,
    canary_path: Path,
    env: Mapping[str, str],
    extra_read_write: Sequence[Path] = (),
    extra_read_only: Sequence[Path] = (),
) -> ContainmentReport:
    """Execute a probe *inside* the backend and report what it observed.

    The probe is the evidence.  A backend is only "available" when this
    function observed the boundary it promises.
    """
    import asyncio
    import json

    probe_dir = private_home / ".probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    script = probe_dir / "probe.py"
    script.write_text(_PROBE_SOURCE, encoding="utf-8")
    os.chmod(script, stat.S_IRUSR | stat.S_IWUSR)

    argv = build_argv(
        backend,
        ["python3", str(script), str(canary_path)],
        workspace=workspace,
        private_home=private_home,
        env=env,
        extra_read_write=extra_read_write,
        extra_read_only=extra_read_only,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (OSError, ValueError, TimeoutError) as exc:
        return ContainmentReport(
            backend=backend,
            ok=False,
            detail=f"the probe could not be executed: {exc}",
        )

    if proc.returncode != 0:
        return ContainmentReport(
            backend=backend,
            ok=False,
            detail=(
                "the probe command failed: "
                + stderr.decode("utf-8", errors="replace").strip()[:400]
            ),
        )
    try:
        observed = json.loads(stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        return ContainmentReport(
            backend=backend,
            ok=False,
            detail=f"the probe did not report usable observations: {exc}",
        )

    checks = {
        "home_is_private": bool(observed.get("home"))
        and str(private_home) in str(observed.get("home")),
        "canary_unreadable": observed.get("canary_read") is None,
        "network_unreachable": observed.get("network") == "unreachable",
        "system_readonly": observed.get("system_writable") is False,
        "workspace_writable": os.access(workspace, os.W_OK),
    }
    if backend == BUBBLEWRAP:
        # Only bubblewrap assembles a filesystem; ``unshare`` does not.
        checks["filesystem_isolated"] = checks["system_readonly"]
    ok = all(checks.values())
    return ContainmentReport(
        backend=backend,
        ok=ok,
        checks=checks,
        detail=(
            "every boundary was observed to hold"
            if ok
            else "the probe observed a boundary that does not hold: "
            + ", ".join(name for name, passed in checks.items() if not passed)
        ),
        observations={
            key: str(value)
            for key, value in observed.items()
            if key in ("home", "uid", "network", "network_error", "canary_error")
        },
    )
