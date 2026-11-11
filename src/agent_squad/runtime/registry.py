"""The owned-resource registry.

A run starts processes (model servers, sandboxed children) and writes files
(worktrees, downloaded weights).  If the run is interrupted, those things
outlive it: a `llama-server` keeps holding the GPU, a worktree keeps
consuming disk, and the next run has to work around them.

This module is the run's record of what it owns, and the mechanism that
reclaims it.

Three properties matter:

* **Only owned things are touched.**  A model the user downloaded before the
  run is never removed; only artifacts this run created are.
* **Cleanup is bounded.**  Every process is terminated, then waited for, then
  force-killed, within a fixed grace period.  Cleanup never hangs.
* **Cleanup is idempotent and reconcilable.**  It can be run twice, and it
  can find resources abandoned by a previous run.

The registry is a plain JSON file inside the run directory, so it survives
the process that wrote it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ResourceKind(str, Enum):
    MODEL_SERVER = "model_server"
    CHILD_PROCESS = "child_process"
    WORKTREE = "worktree"
    DOWNLOADED_ARTIFACT = "downloaded_artifact"


@dataclass
class OwnedResource:
    """One thing the run owns and may reclaim."""

    kind: ResourceKind
    identifier: str
    pid: int | None = None
    pgid: int | None = None
    path: str | None = None
    created_at: float = field(default_factory=time.time)
    reclaimed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "identifier": self.identifier,
            "pid": self.pid,
            "pgid": self.pgid,
            "path": self.path,
            "created_at": self.created_at,
            "reclaimed": self.reclaimed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OwnedResource:
        return cls(
            kind=ResourceKind(data["kind"]),
            identifier=data["identifier"],
            pid=data.get("pid"),
            pgid=data.get("pgid"),
            path=data.get("path"),
            created_at=float(data.get("created_at") or 0.0),
            reclaimed=bool(data.get("reclaimed")),
        )


@dataclass
class CleanupReport:
    """What cleanup actually did."""

    processes_terminated: int = 0
    processes_force_killed: int = 0
    processes_already_gone: int = 0
    artifacts_removed: int = 0
    artifacts_preserved: int = 0
    survivors: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when nothing the run owned survived the cleanup."""
        return not self.survivors

    def to_dict(self) -> dict[str, Any]:
        return {
            "processes_terminated": self.processes_terminated,
            "processes_force_killed": self.processes_force_killed,
            "processes_already_gone": self.processes_already_gone,
            "artifacts_removed": self.artifacts_removed,
            "artifacts_preserved": self.artifacts_preserved,
            "survivors": list(self.survivors),
            "errors": list(self.errors),
            "clean": self.clean,
        }


class ResourceRegistry:
    """The durable record of what a run owns.

    The registry is deliberately conservative: it only ever terminates a
    process whose PID *and* process group it recorded itself, and it only
    removes a path it created.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._resources: dict[str, OwnedResource] = {}

    # ------------------------------------------------------------ recording

    def register_process(
        self,
        *,
        kind: ResourceKind,
        identifier: str,
        pid: int | None = None,
        pgid: int | None = None,
    ) -> OwnedResource:
        resource = OwnedResource(
            kind=kind, identifier=identifier, pid=pid, pgid=pgid
        )
        self._resources[identifier] = resource
        self.flush()
        return resource

    def register_artifact(self, *, identifier: str, path: str | Path) -> OwnedResource:
        resource = OwnedResource(
            kind=ResourceKind.DOWNLOADED_ARTIFACT, identifier=identifier, path=str(path)
        )
        self._resources[identifier] = resource
        self.flush()
        return resource

    def register_worktree(self, *, identifier: str, path: str | Path) -> OwnedResource:
        resource = OwnedResource(
            kind=ResourceKind.WORKTREE, identifier=identifier, path=str(path)
        )
        self._resources[identifier] = resource
        self.flush()
        return resource

    # --------------------------------------------------------------- lookup

    @property
    def resources(self) -> list[OwnedResource]:
        return list(self._resources.values())

    def pending(self) -> list[OwnedResource]:
        return [r for r in self._resources.values() if not r.reclaimed]

    def flush(self) -> None:
        """Persist the registry so it survives the process that wrote it."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "resources": [r.to_dict() for r in self._resources.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            # Losing the registry is bad but must never break the run.
            pass

    @classmethod
    def load(cls, path: str | Path) -> ResourceRegistry:
        registry = cls(path)
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return registry
        for item in payload.get("resources", []):
            try:
                resource = OwnedResource.from_dict(item)
            except (KeyError, ValueError):
                continue
            registry._resources[resource.identifier] = resource
        return registry

    # -------------------------------------------------------------- cleanup

    def cleanup(self, *, grace_s: float = 5.0) -> CleanupReport:
        """Reclaim everything the run owns.

        A process is terminated, waited for, and force-killed if it does not
        exit within the grace period.  A survivor is *reported*, not
        silently ignored: the caller can decide what to do about it.
        """
        report = CleanupReport()
        for resource in self.pending():
            try:
                if resource.kind in (
                    ResourceKind.MODEL_SERVER,
                    ResourceKind.CHILD_PROCESS,
                ):
                    self._cleanup_process(resource, report, grace_s)
                elif resource.kind in (
                    ResourceKind.DOWNLOADED_ARTIFACT,
                    ResourceKind.WORKTREE,
                ):
                    self._cleanup_path(resource, report)
            except Exception as exc:  # pragma: no cover - defensive
                report.errors.append(f"{resource.identifier}: {exc}")
            resource.reclaimed = True
        self.flush()
        return report

    def _cleanup_process(
        self, resource: OwnedResource, report: CleanupReport, grace_s: float
    ) -> None:
        pid = resource.pid
        if pid is None:
            return
        refusal = _refusal_reason(pid, resource.pgid)
        if refusal:
            # A cleanup routine that can signal init, or its own process
            # group, can take the machine down with it.  That is never
            # acceptable, so the refusal is recorded rather than acted on.
            report.survivors.append(f"{resource.identifier}: {refusal}")
            report.errors.append(f"{resource.identifier}: {refusal}")
            return
        if not _pid_alive(pid):
            report.processes_already_gone += 1
            return
        # The whole process group is signalled, so a child that spawned
        # grandchildren does not leave them behind.
        target = resource.pgid if resource.pgid else pid
        try:
            os.killpg(target, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                report.processes_already_gone += 1
                return
        report.processes_terminated += 1

        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)

        # It ignored SIGTERM; escalate, but only once, and only for the
        # group we own.
        try:
            os.killpg(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGKILL)
        report.processes_force_killed += 1
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)
        report.survivors.append(f"{resource.identifier} (pid {pid})")

    def _cleanup_path(self, resource: OwnedResource, report: CleanupReport) -> None:
        if not resource.path:
            return
        target = Path(resource.path)
        if not target.exists():
            report.artifacts_preserved += 1
            return
        import shutil

        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
            report.artifacts_removed += 1
        except OSError as exc:
            report.errors.append(f"{resource.identifier}: {exc}")


def _refusal_reason(pid: int, pgid: int | None) -> str | None:
    """Why a process must **not** be signalled, or ``None`` if it is safe.

    Two classes of target are refused outright:

    * **init** (PID 1) and the kernel's idle task.  Signalling PID 1 in a
      container or a WSL instance tears down the whole session, and no
      cleanup routine has any business doing that.
    * **the cleanup process's own process group.**  A cleanup that killed
      its own group would kill the run that invoked it.

    A registry that has been corrupted, or a caller that has supplied a
    nonsense pid, must fail *safe*.
    """
    if pid <= 1:
        return f"refusing to signal pid {pid}: it is init or the kernel idle task"
    if pid == os.getpid():
        return "refusing to signal the cleanup process itself"
    if pgid is not None:
        if pgid <= 1:
            return f"refusing to signal process group {pgid}: it is init"
        if pgid == os.getpgrp():
            return "refusing to signal the cleanup process's own process group"
    return None


def _pid_alive(pid: int) -> bool:
    """Whether a process still exists *and* still holds resources.

    A zombie — a process that has exited but has not been reaped — still
    answers ``kill(pid, 0)``, but it holds no memory, no handles and no
    GPU context.  Treating a zombie as alive would make cleanup report a
    survivor that is, in fact, already gone.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists but belongs to somebody else.
        return True
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """Whether the process has exited but not been reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # The state character follows the (possibly space-containing) comm
    # field, which is parenthesised.
    try:
        fields = stat[stat.rindex(")") + 1 :].split()
        return bool(fields) and fields[0] == "Z"
    except (ValueError, IndexError):
        return False


def reconcile(
    registry: ResourceRegistry, *, grace_s: float = 5.0
) -> CleanupReport:
    """Reclaim resources abandoned by a previous run.

    A run that was killed cannot clean up after itself, so the next run
    finds the previous run's registry and reclaims what it still owns.
    """
    return registry.cleanup(grace_s=grace_s)
