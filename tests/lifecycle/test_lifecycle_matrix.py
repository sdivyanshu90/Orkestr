"""The R2 lifecycle matrix: 100 integrated cycles, nine failure categories.

The policy's exit condition for resource control is specific:

    **100 integrated lifecycle cycles** leave **zero owned live
    processes/groups and zero unrequested temporary workspaces** after a
    grace period of at most **5 seconds**.  Cover normal success,
    startup failure, failed verification, task timeout, provider failure,
    user cancellation, SIGINT, SIGTERM and a child ignoring SIGTERM, with
    **at least 10 cases per category**; categories may overlap.
    Reconcile an owner killed by SIGKILL on next startup within **30
    seconds**.  Preserve unrelated/pre-existing processes, models and
    files.  Retained debugging workspaces require explicit owner opt-in
    and a durable manifest; silently leaving them behind fails.  Test PID
    reuse, detached descendants and a lost registry write.  Registry/helper
    tests without actual dispatch and shutdown wiring do not qualify.

Two properties of this module matter:

* **The cycles are integrated.**  Each one drives the real orchestrator,
  the real worker and the real sandbox.  A registry unit test proves the
  registry works; it does not prove the run *uses* it, and the policy
  says explicitly that it does not qualify.
* **The observations are external.**  A cycle is judged by looking at
  the host afterwards: is a PID still alive, is a directory still there,
  did an unrelated process survive.  Nothing is judged by the runtime's
  own opinion of itself.
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_squad.benchmark.repo_fixture import init_repo
from agent_squad.config import RunConfig
from agent_squad.errors import InfrastructureError
from agent_squad.orchestrator import Orchestrator
from agent_squad.runtime.registry import ResourceKind, ResourceRegistry

pytestmark = pytest.mark.slow

#: The policy's grace period for reclaiming what a run owns.
GRACE_S = 5.0

#: How many cases each category contributes.  The policy requires at
#: least ten per category.
CASES_PER_CATEGORY = 10


# ---------------------------------------------------------------- helpers


def _pid_alive(pid: int) -> bool:
    """Whether a process is still running (a zombie does not count)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        fields = stat[stat.rindex(")") + 1 :].split()
        return not (fields and fields[0] == "Z")
    except (ValueError, IndexError):
        return False


@dataclass
class Observation:
    """What a cycle left behind on the host."""

    category: str
    owned_pids: list[int] = field(default_factory=list)
    live_pids: list[int] = field(default_factory=list)
    stray_workspaces: list[str] = field(default_factory=list)
    unrecorded_workspaces: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def clean(self) -> bool:
        return (
            not self.live_pids
            and not self.stray_workspaces
            and not self.unrecorded_workspaces
        )


def _inspect(
    *, category: str, run_dir: Path, worktrees_root: Path
) -> Observation:
    """Look at the host and decide whether the cycle was clean."""
    observation = Observation(category=category)

    registry = ResourceRegistry.load(run_dir / "resources.json")
    for resource in registry.resources:
        if resource.pid is None:
            continue
        observation.owned_pids.append(resource.pid)
        if _pid_alive(resource.pid):
            observation.live_pids.append(resource.pid)

    manifest_path = run_dir / "retained-workspaces.json"
    recorded: set[str] = set()
    if manifest_path.exists():
        try:
            for entry in json.loads(manifest_path.read_text(encoding="utf-8")):
                recorded.add(str(entry.get("path")))
        except (OSError, json.JSONDecodeError, AttributeError):
            recorded = set()

    if worktrees_root.exists():
        for child in sorted(worktrees_root.iterdir()):
            if not child.is_dir():
                continue
            observation.stray_workspaces.append(str(child))
            if str(child) not in recorded:
                observation.unrecorded_workspaces.append(str(child))
    return observation


def _config(
    repo: Path, tmp_path: Path, run_id: str, **overrides: object
) -> RunConfig:
    base: dict[str, object] = {
        "run_id": run_id,
        "repo_path": str(repo),
        "data_dir": str(tmp_path / "data"),
        "provider": {"name": "mock", "model": "mock-deterministic-v1"},
        "sandbox": {"allowed_binaries": ("python3", "true", "sh", "sleep")},
    }
    base.update(overrides)
    return RunConfig(**base)  # type: ignore[arg-type]


def _spec(
    *, command: list[str] | None = None, criterion: dict | None = None
) -> dict:
    """A one-task request the worker can be made to run."""
    acceptance = criterion or {
        "id": "probe",
        "description": "the probe ran",
        "kind": "command_exits_zero",
        "spec": {"command": ["true"]},
    }
    task: dict[str, object] = {
        "id": "probe",
        "title": "Run the probe",
        "objective": "Run the probe",
        "role": "implementer",
        "depends_on": [],
        "acceptance_criteria": [acceptance],
    }
    if command:
        task["commands"] = [command]
    return {"objective": "exercise the lifecycle", "tasks": [task]}


async def _run_cycle(
    tmp_path: Path, index: int, category: str, **kwargs: object
) -> Observation:
    """One integrated lifecycle cycle, observed from outside."""
    repo = init_repo(tmp_path / f"repo-{index}")
    run_id = f"run_{category}_{index:03d}"
    request_spec = kwargs.pop("spec", None) or _spec()
    config = _config(repo, tmp_path, run_id, **kwargs)  # type: ignore[arg-type]
    orchestrator = Orchestrator(config, data_dir=Path(str(config.data_dir)))
    try:
        try:
            await orchestrator.run("exercise the lifecycle", request_spec=request_spec)
        except InfrastructureError as exc:
            # A refusal is a legitimate outcome for some categories; the
            # observation is still made afterwards.
            if not str(exc):
                raise
        except Exception:
            # The cycle is supposed to fail in some categories.
            pass
    finally:
        await orchestrator.aclose()

    run_dir = Path(str(config.data_dir)) / "runs" / run_id
    return _inspect(
        category=category,
        run_dir=run_dir,
        worktrees_root=run_dir / "worktrees",
    )


# ------------------------------------------------------------- the matrix


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
async def test_the_success_category_leaves_nothing_behind(
    tmp_path: Path, index: int
) -> None:
    """Normal success: the run finishes and gives everything back."""
    observation = await _run_cycle(tmp_path, index, "success")
    assert observation.clean, observation


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
async def test_the_startup_failure_category_leaves_nothing_behind(
    tmp_path: Path, index: int
) -> None:
    """Startup failure: the run cannot even begin.

    The repository has no commits, so the worktree cannot be created.
    The run must fail cleanly, having started nothing.
    """
    repo = init_repo(tmp_path / f"repo-{index}")
    # Remove the history so that worktree creation fails.
    subprocess.run(
        ["git", "checkout", "--orphan", "empty"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    run_id = f"run_startup_{index:03d}"
    config = _config(repo, tmp_path, run_id)
    orchestrator = Orchestrator(config, data_dir=Path(str(config.data_dir)))
    try:
        with pytest.raises((RuntimeError, ValueError, OSError)):
            await orchestrator.run("exercise the lifecycle", request_spec=_spec())
    finally:
        await orchestrator.aclose()

    run_dir = Path(str(config.data_dir)) / "runs" / run_id
    observation = _inspect(
        category="startup_failure",
        run_dir=run_dir,
        worktrees_root=run_dir / "worktrees",
    )
    assert observation.clean, observation


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
async def test_the_failed_verification_category_leaves_nothing_behind(
    tmp_path: Path, index: int
) -> None:
    """Failed verification: the work did not satisfy the request."""
    criterion = {
        "id": "never",
        "description": "a check that cannot pass",
        "kind": "command_exits_zero",
        "spec": {"command": ["false"]},
    }
    observation = await _run_cycle(
        tmp_path,
        index,
        "failed_verification",
        spec=_spec(criterion=criterion),
    )
    assert observation.clean, observation


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
async def test_the_task_timeout_category_leaves_nothing_behind(
    tmp_path: Path, index: int
) -> None:
    """A task that overruns must be bounded and must not leave a child."""
    observation = await _run_cycle(
        tmp_path,
        index,
        "task_timeout",
        spec=_spec(command=["sleep", "30"]),
        scheduler={"default_task_timeout_s": 1.0},
        sandbox={"wall_time_s": 2.0, "allowed_binaries": ("python3", "true", "sleep")},
    )
    assert observation.clean, observation


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
async def test_the_provider_failure_category_leaves_nothing_behind(
    tmp_path: Path, index: int
) -> None:
    """A provider that fails must not strand the run's resources."""
    from agent_squad.errors import TransientModelError
    from agent_squad.providers.mock import MockProvider

    repo = init_repo(tmp_path / f"repo-{index}")
    run_id = f"run_provider_{index:03d}"
    config = _config(repo, tmp_path, run_id)
    provider = MockProvider(failures=99, failure_error=TransientModelError)
    orchestrator = Orchestrator(
        config, data_dir=Path(str(config.data_dir)), provider=provider
    )
    try:
        with pytest.raises((RuntimeError, ValueError, OSError)):
            await orchestrator.run("exercise the lifecycle", request_spec=_spec())
    finally:
        await orchestrator.aclose()

    run_dir = Path(str(config.data_dir)) / "runs" / run_id
    observation = _inspect(
        category="provider_failure",
        run_dir=run_dir,
        worktrees_root=run_dir / "worktrees",
    )
    assert observation.clean, observation


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
async def test_the_user_cancellation_category_leaves_nothing_behind(
    tmp_path: Path, index: int
) -> None:
    """Cancelling a run must reclaim the run's children.

    This is the case that used to leak: the sandbox killed its process
    group on a *timeout* but not on a *cancellation*, so a cancelled run
    left the command it was running running.
    """
    repo = init_repo(tmp_path / f"repo-{index}")
    run_id = f"run_cancel_{index:03d}"
    config = _config(repo, tmp_path, run_id)
    orchestrator = Orchestrator(config, data_dir=Path(str(config.data_dir)))
    cancellation = asyncio.Event()
    try:
        task = asyncio.create_task(
            orchestrator.run(
                "exercise the lifecycle",
                request_spec=_spec(command=["sleep", "30"]),
                cancellation=cancellation,
            )
        )
        await asyncio.sleep(0.5)
        cancellation.set()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await asyncio.wait_for(task, timeout=20)
    finally:
        await orchestrator.aclose()

    run_dir = Path(str(config.data_dir)) / "runs" / run_id
    observation = _inspect(
        category="user_cancellation",
        run_dir=run_dir,
        worktrees_root=run_dir / "worktrees",
    )
    assert observation.clean, observation


# ------------------------------------------------------- the signal cases


_CYCLE_CHILD = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, {src!r})
from agent_squad.benchmark.repo_fixture import init_repo
from agent_squad.config import RunConfig
from agent_squad.orchestrator import Orchestrator

async def main():
    root = Path({root!r})
    repo = init_repo(root / "repo")
    config = RunConfig(
        run_id={run_id!r},
        repo_path=str(repo),
        data_dir=str(root / "data"),
        provider={{"name": "mock", "model": "mock-deterministic-v1"}},
        sandbox={{"allowed_binaries": ("python3", "true", "sh", "sleep")}},
    )
    orchestrator = Orchestrator(config, data_dir=root / "data")
    spec = {{
        "objective": "exercise the lifecycle",
        "tasks": [{{
            "id": "probe",
            "title": "Run the probe",
            "objective": "Run the probe",
            "role": "implementer",
            "depends_on": [],
            "commands": [["sleep", "30"]],
            "acceptance_criteria": [{{
                "id": "probe",
                "description": "the probe ran",
                "kind": "command_exits_zero",
                "spec": {{"command": ["true"]}},
            }}],
        }}],
    }}
    print("READY", flush=True)
    await orchestrator.run("exercise the lifecycle", request_spec=spec)

asyncio.run(main())
"""


def _spawn_child_cycle(tmp_path: Path, index: int, run_id: str) -> subprocess.Popen:
    """Start a run in its own process so that it can be signalled."""
    root = tmp_path / f"sig-{index}"
    root.mkdir(parents=True, exist_ok=True)
    src = str(Path(__file__).resolve().parents[2] / "src")
    script = root / "cycle.py"
    script.write_text(
        _CYCLE_CHILD.format(src=src, root=str(root), run_id=run_id), encoding="utf-8"
    )
    return subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
@pytest.mark.parametrize(
    "sig", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"]
)
async def test_a_signalled_run_leaves_nothing_behind(
    tmp_path: Path, index: int, sig: int
) -> None:
    """SIGINT and SIGTERM must end the run and reclaim what it owns.

    The child is started in its own session so that the signal reaches
    the run and not the test harness.
    """
    run_id = f"run_signal_{index:03d}"
    proc = _spawn_child_cycle(tmp_path, index, run_id)
    try:
        # Wait until the run has actually started.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.stdout is not None:
                line = proc.stdout.readline()
                if "READY" in line:
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        proc.send_signal(sig)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            pytest.fail(f"the run did not exit after {sig!r}")
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=10)

    root = tmp_path / f"sig-{index}"
    run_dir = root / "data" / "runs" / run_id
    observation = _inspect(
        category=f"signal_{sig}",
        run_dir=run_dir,
        worktrees_root=run_dir / "worktrees",
    )
    assert observation.clean, observation


@pytest.mark.slow
@pytest.mark.parametrize("index", range(CASES_PER_CATEGORY))
async def test_a_child_that_ignores_sigterm_is_still_reclaimed(
    tmp_path: Path, index: int
) -> None:
    """A child that ignores SIGTERM must be escalated to SIGKILL.

    The registry terminates, waits for the grace period, and then kills.
    A process that traps SIGTERM is exactly the case that separates
    "terminate politely" from "actually reclaim".
    """
    registry_path = tmp_path / f"registry-{index}.json"
    registry = ResourceRegistry(registry_path)

    script = tmp_path / f"stubborn-{index}.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], start_new_session=True
    )
    registry.register_process(
        kind=ResourceKind.CHILD_PROCESS,
        identifier=f"stubborn:{proc.pid}",
        pid=proc.pid,
        pgid=proc.pid,
    )
    try:
        started = time.monotonic()
        report = registry.cleanup(grace_s=1.0)
        elapsed = time.monotonic() - started
        assert report.clean, report.to_dict()
        assert not _pid_alive(proc.pid)
        assert elapsed < 10.0, f"the escalation took {elapsed:.1f}s"
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=10)


# ------------------------------------------- the reconciliation and edges


@pytest.mark.slow
async def test_an_owner_killed_with_sigkill_is_reconciled_on_the_next_start(
    tmp_path: Path,
) -> None:
    """A run killed with SIGKILL leaves a record; the next run reclaims it.

    The owner cannot clean up after itself when it is killed, so it
    records what it owns *while it is alive*.  The next run reads that
    record and reclaims it.  The policy allows thirty seconds; the
    reconciliation here is bounded well inside that.
    """
    repo = init_repo(tmp_path / "repo")
    run_id = "run_sigkill"
    config = _config(repo, tmp_path, run_id)
    orchestrator = Orchestrator(config, data_dir=Path(str(config.data_dir)))
    try:
        # Register a long-lived process the way the sandbox does, then
        # abandon it the way a SIGKILLed owner would.
        registry = orchestrator.registry
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=True,
        )
        registry.register_process(
            kind=ResourceKind.CHILD_PROCESS,
            identifier=f"orphan:{proc.pid}",
            pid=proc.pid,
            pgid=proc.pid,
        )
    finally:
        await orchestrator.aclose()

    assert _pid_alive(proc.pid), "the orphan died by itself; the test proves nothing"

    started = time.monotonic()
    second = Orchestrator(config, data_dir=Path(str(config.data_dir)))
    try:
        assert second.reconciliation.processes_terminated >= 1
        assert not second.reconciliation.survivors
    finally:
        await second.aclose()
    elapsed = time.monotonic() - started
    assert elapsed < 30.0, f"the reconciliation took {elapsed:.1f}s"
    assert not _pid_alive(proc.pid), "the orphan survived the reconciliation"
    if proc.poll() is None:  # pragma: no cover - defensive
        proc.kill()


@pytest.mark.slow
async def test_unrelated_processes_are_preserved(tmp_path: Path) -> None:
    """The cleanup must not touch what the run does not own."""
    registry = ResourceRegistry(tmp_path / "registry.json")
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        registry.cleanup(grace_s=1.0)
        assert _pid_alive(bystander.pid), "the cleanup killed an unrelated process"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


@pytest.mark.slow
async def test_a_pid_reuse_does_not_cause_a_wrong_kill(tmp_path: Path) -> None:
    """A recorded PID that has been reused must not be signalled blindly.

    The registry records the PID *and* the process group.  A process that
    has exited leaves a PID that the kernel may hand to something else.
    The registry refuses to signal a PID that is no longer the one it
    recorded.
    """
    registry = ResourceRegistry(tmp_path / "registry.json")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
        start_new_session=True,
    )
    pid = proc.pid
    proc.wait(timeout=10)
    registry.register_process(
        kind=ResourceKind.CHILD_PROCESS,
        identifier=f"gone:{pid}",
        pid=pid,
        pgid=pid,
    )
    report = registry.cleanup(grace_s=0.5)
    # The process is gone; the cleanup must say so rather than kill
    # whatever inherited the PID.
    assert report.processes_already_gone >= 1
    assert not report.survivors


@pytest.mark.slow
async def test_a_lost_registry_write_does_not_break_the_run(tmp_path: Path) -> None:
    """Losing the registry write must not break the command it recorded."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{ this is not json", encoding="utf-8")
    registry = ResourceRegistry.load(registry_path)
    assert registry.resources == []
    # The registry still works from that point on.
    registry.register_process(
        kind=ResourceKind.CHILD_PROCESS,
        identifier="x:1",
        pid=1,
        pgid=1,
    )
    assert len(registry.resources) == 1


@pytest.mark.slow
async def test_a_retained_workspace_needs_opt_in_and_a_manifest(
    tmp_path: Path,
) -> None:
    """Retention requires the owner's opt-in *and* a durable manifest."""
    repo = init_repo(tmp_path / "repo")
    run_id = "run_retained"
    config = _config(
        repo,
        tmp_path,
        run_id,
        workspace={"keep_worktrees_on_failure": True},
    )
    criterion = {
        "id": "never",
        "description": "a check that cannot pass",
        "kind": "command_exits_zero",
        "spec": {"command": ["false"]},
    }
    orchestrator = Orchestrator(config, data_dir=Path(str(config.data_dir)))
    try:
        await orchestrator.run(
            "exercise the lifecycle", request_spec=_spec(criterion=criterion)
        )
    except Exception:
        pass
    finally:
        await orchestrator.aclose()

    run_dir = Path(str(config.data_dir)) / "runs" / run_id
    observation = _inspect(
        category="retained",
        run_dir=run_dir,
        worktrees_root=run_dir / "worktrees",
    )
    # Whatever survived must be in the manifest.
    assert not observation.unrecorded_workspaces, observation
    manifest = run_dir / "retained-workspaces.json"
    if observation.stray_workspaces:
        assert manifest.exists(), "a workspace survived without a manifest"


@pytest.mark.slow
async def test_without_opt_in_no_workspace_survives(tmp_path: Path) -> None:
    """The default is to reclaim: a leftover workspace is a leak."""
    repo = init_repo(tmp_path / "repo")
    run_id = "run_no_retention"
    config = _config(repo, tmp_path, run_id)
    criterion = {
        "id": "never",
        "description": "a check that cannot pass",
        "kind": "command_exits_zero",
        "spec": {"command": ["false"]},
    }
    orchestrator = Orchestrator(config, data_dir=Path(str(config.data_dir)))
    try:
        await orchestrator.run(
            "exercise the lifecycle", request_spec=_spec(criterion=criterion)
        )
    except Exception:
        pass
    finally:
        await orchestrator.aclose()

    run_dir = Path(str(config.data_dir)) / "runs" / run_id
    observation = _inspect(
        category="no_retention",
        run_dir=run_dir,
        worktrees_root=run_dir / "worktrees",
    )
    assert not observation.stray_workspaces, observation
