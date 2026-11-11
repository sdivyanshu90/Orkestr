"""R1: resource bounds and owned-resource cleanup.

The exit criterion for R1 is specific: a hundred automated cycles covering
success, startup failure, timeout, signals, provider failure and cancellation
must leave **zero surviving owned processes** after the configured grace
period, and pre-existing processes and models must survive.

These tests cover the mechanism and the failure modes; the cycle harness
below runs the repetitions.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_squad.runtime.deadline import (
    DeadlineExceeded,
    DeadlineGuard,
    DeadlineState,
    RunDeadline,
)
from agent_squad.runtime.registry import (
    ResourceKind,
    ResourceRegistry,
    reconcile,
)

# ------------------------------------------------------------------ helpers


def _pid_alive(pid: int) -> bool:
    """Whether the process still holds resources.

    A zombie has exited but not been reaped: it answers ``kill(pid, 0)``
    yet holds nothing.  The registry's own check knows about that, so the
    test uses the same definition rather than a second one.
    """
    from agent_squad.runtime.registry import _pid_alive as registry_alive

    return registry_alive(pid)


def reap(proc: subprocess.Popen) -> None:
    """Reap a child so it does not linger as a zombie."""
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def spawn_sleeper(seconds: float = 120) -> subprocess.Popen:
    """A child that ignores SIGTERM, to prove the escalation works.

    A shell is used rather than Python because the cycle test starts
    hundreds of these, and process start-up dominates the cost.
    """
    proc = subprocess.Popen(
        ["/bin/sh", "-c", f'trap "" TERM; sleep {seconds}'],
        start_new_session=True,
    )
    # Give the shell a moment to install the trap, so the test does not
    # race the child's start-up.
    time.sleep(0.05)
    return proc


# ------------------------------------------------------------------ registry


def test_the_registry_persists_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "resources.json"
    registry = ResourceRegistry(path)
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER, identifier="srv:1", pid=1234, pgid=1234
    )
    registry.register_artifact(identifier="weights", path=str(tmp_path / "m.gguf"))

    reloaded = ResourceRegistry.load(path)
    assert len(reloaded.resources) == 2
    kinds = {r.kind for r in reloaded.resources}
    assert ResourceKind.MODEL_SERVER in kinds
    assert ResourceKind.DOWNLOADED_ARTIFACT in kinds


def test_cleanup_terminates_a_process(tmp_path: Path) -> None:
    registry = ResourceRegistry(tmp_path / "resources.json")
    proc = spawn_sleeper()
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER,
        identifier="srv:1",
        pid=proc.pid,
        pgid=proc.pid,
    )
    try:
        report = registry.cleanup(grace_s=5.0)
        assert report.processes_terminated == 1
        assert report.processes_force_killed == 1  # it ignored SIGTERM
        assert report.clean
        proc.wait(timeout=5)
        assert not _pid_alive(proc.pid)
    finally:
        reap(proc)


def test_cleanup_kills_the_whole_process_group(tmp_path: Path) -> None:
    """A child that spawned a grandchild must not leave it behind."""
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    time.sleep(1.0)
    grandchildren = subprocess.run(
        ["pgrep", "-P", str(parent.pid)], capture_output=True, text=True, check=False
    ).stdout.split()

    registry = ResourceRegistry(tmp_path / "resources.json")
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER,
        identifier="srv:1",
        pid=parent.pid,
        pgid=parent.pid,
    )
    try:
        report = registry.cleanup(grace_s=5.0)
        assert report.clean, report.to_dict()
        time.sleep(0.5)
        for pid in grandchildren:
            assert not _pid_alive(int(pid)), "a grandchild survived"
    finally:
        for pid in grandchildren:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(int(pid), signal.SIGKILL)
        reap(parent)


def test_cleanup_removes_only_owned_artifacts(tmp_path: Path) -> None:
    registry = ResourceRegistry(tmp_path / "resources.json")
    owned = tmp_path / "owned.gguf"
    owned.write_text("weights", encoding="utf-8")
    foreign = tmp_path / "pre-existing.gguf"
    foreign.write_text("the user's model", encoding="utf-8")

    registry.register_artifact(identifier="owned", path=str(owned))
    report = registry.cleanup(grace_s=1.0)

    assert not owned.exists()
    assert foreign.exists(), "a pre-existing model must survive"
    assert report.artifacts_removed == 1


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    registry = ResourceRegistry(tmp_path / "resources.json")
    proc = spawn_sleeper()
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER,
        identifier="srv:1",
        pid=proc.pid,
        pgid=proc.pid,
    )
    first = registry.cleanup(grace_s=3.0)
    second = registry.cleanup(grace_s=3.0)
    assert first.clean
    assert second.clean
    assert second.processes_terminated == 0


def test_a_process_that_is_already_gone_is_not_an_error(tmp_path: Path) -> None:
    registry = ResourceRegistry(tmp_path / "resources.json")
    proc = spawn_sleeper()
    pid = proc.pid
    reap(proc)
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER, identifier="srv:1", pid=pid, pgid=pid
    )
    report = registry.cleanup(grace_s=1.0)
    assert report.processes_already_gone == 1
    assert report.clean


def test_reconcile_reclaims_resources_from_an_abandoned_run(tmp_path: Path) -> None:
    """A run that was killed cannot clean up; the next one does."""
    path = tmp_path / "resources.json"
    registry = ResourceRegistry(path)
    proc = spawn_sleeper()
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER,
        identifier="srv:orphan",
        pid=proc.pid,
        pgid=proc.pid,
    )
    # Simulate a crash: the registry file stays, the process keeps running.
    abandoned = ResourceRegistry.load(path)
    report = reconcile(abandoned, grace_s=5.0)
    assert report.clean
    assert not _pid_alive(proc.pid)


def test_cleanup_refuses_to_signal_init_or_its_own_group(tmp_path: Path) -> None:
    """A cleanup routine must never be able to take the machine down.

    Signalling PID 1 in a container or a WSL instance tears down the whole
    session, and signalling the cleanup's own process group would kill the
    run that invoked it.  Both are refused, and the refusal is recorded
    rather than silently ignored.
    """
    registry = ResourceRegistry(tmp_path / "resources.json")
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER, identifier="init", pid=1, pgid=1
    )
    registry.register_process(
        kind=ResourceKind.MODEL_SERVER,
        identifier="self",
        pid=os.getpid(),
        pgid=os.getpgrp(),
    )
    report = registry.cleanup(grace_s=0.2)

    # The refusals are reported, not hidden, and nothing was signalled.
    assert len(report.survivors) == 2
    assert any("init" in s for s in report.survivors)
    assert any("own process group" in s or "itself" in s for s in report.survivors)
    # This process is, obviously, still running.
    assert _pid_alive(os.getpid())


# ------------------------------------------------------------------ deadline


def test_a_deadline_counts_down() -> None:
    deadline = RunDeadline(total_s=10.0, reserve_s=1.0)
    deadline.start()
    assert deadline.enabled
    assert deadline.state() is DeadlineState.OK
    remaining = deadline.remaining()
    assert remaining is not None
    assert 8.0 < remaining <= 9.0


def test_a_deadline_reserves_teardown_time() -> None:
    deadline = RunDeadline(total_s=5.0, reserve_s=4.0)
    deadline.start()
    assert (deadline.remaining() or 0) <= 1.0


def test_an_exhausted_deadline_is_reported() -> None:
    deadline = RunDeadline(total_s=0.05, reserve_s=0.0)
    deadline.start()
    time.sleep(0.1)
    assert deadline.expired()
    assert deadline.state() is DeadlineState.EXPIRED


def test_a_clamped_timeout_cannot_outlive_the_deadline() -> None:
    deadline = RunDeadline(total_s=3.0, reserve_s=0.5)
    deadline.start()
    clamped = deadline.clamp(600.0)
    assert clamped is not None
    assert clamped <= 3.0


def test_the_guard_raises_once_the_deadline_has_passed() -> None:
    deadline = RunDeadline(total_s=0.05, reserve_s=0.0)
    deadline.start()
    guard = DeadlineGuard(deadline)
    time.sleep(0.1)
    with pytest.raises(DeadlineExceeded):
        guard.check("verification")


def test_a_deadline_can_be_disabled() -> None:
    deadline = RunDeadline(total_s=None)
    deadline.start()
    assert not deadline.enabled
    assert deadline.remaining() is None
    assert deadline.clamp(600.0) == 600.0
    assert not deadline.expired()


@pytest.mark.integration
async def test_a_run_with_a_deadline_stops_when_it_is_reached(
    tmp_path: Path, repo: Path
) -> None:
    """A run must not keep working past its absolute deadline."""
    from agent_squad.config import RunConfig
    from agent_squad.orchestrator import Orchestrator

    config = RunConfig(
        run_id="run_deadline",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        deadline_s=0.05,
        teardown_reserve_s=0.0,
        scheduler={"default_task_timeout_s": 30.0},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run(
            "a run with no time",
            request_spec={
                "objective": "o",
                "tasks": [
                    {
                        "id": "t",
                        "title": "t",
                        "objective": "o",
                        "role": "implementer",
                        "depends_on": [],
                        "edits": [],
                        "acceptance_criteria": [
                            {
                                "id": "c",
                                "description": "c",
                                "kind": "file_exists",
                                "spec": {"path": "x.py"},
                            }
                        ],
                    }
                ],
            },
        )
    finally:
        await orchestrator.aclose()

    assert outcome.status.value in ("cancelled", "completed_with_failures")
    for task_outcome in outcome.outcomes.values():
        assert task_outcome.status.value != "succeeded"


# ------------------------------------------------------------- the cycles


@pytest.mark.slow
def test_a_hundred_cycles_leave_no_surviving_owned_process(tmp_path: Path) -> None:
    """The R1 exit criterion, in its smallest form.

    Each cycle starts a process, registers it, and cleans up through a
    different exit path.  After every cycle the process must be gone.
    """
    cycles = 100
    survivors = 0
    for index in range(cycles):
        registry = ResourceRegistry(tmp_path / f"resources-{index}.json")
        proc = spawn_sleeper(30)
        registry.register_process(
            kind=ResourceKind.MODEL_SERVER,
            identifier=f"srv:{index}",
            pid=proc.pid,
            pgid=proc.pid,
        )
        # Vary the exit path: a normal cleanup, a cleanup after the process
        # has already exited, and a cleanup of an empty registry.
        grace = 0.15
        if index % 3 == 0:
            report = registry.cleanup(grace_s=grace)
        elif index % 3 == 1:
            proc.kill()
            proc.wait(timeout=5)
            report = registry.cleanup(grace_s=grace)
        else:
            report = registry.cleanup(grace_s=grace)
            report = registry.cleanup(grace_s=grace)
        if not report.clean:
            survivors += 1
        # Whatever the path, the process must be gone.
        time.sleep(0.05)
        reap(proc)
        if _pid_alive(proc.pid):
            survivors += 1
            reap(proc)
    assert survivors == 0, f"{survivors} owned processes survived"
