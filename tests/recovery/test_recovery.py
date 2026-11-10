"""Recovery tests.

These cover the crash-recovery path: a run that was interrupted mid-flight
must be recoverable from its durable record, and recovery must be
idempotent.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_squad.config import RunConfig
from agent_squad.models.task import Budget, TaskSpec, TaskStatus
from agent_squad.orchestrator import Orchestrator
from agent_squad.recovery import (
    find_run,
    recover_run,
    resume_run,
    verify_side_effects,
)
from agent_squad.store.db import Database
from agent_squad.store.events import EventLog, EventType
from agent_squad.store.state import StateStore

from helpers import two_independent_spec


def build_run_dir(tmp_path: Path, run_id: str = "r1") -> Path:
    """Create the directory layout a stored run uses."""
    root = tmp_path / "data" / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def seed_interrupted_task(
    tmp_path: Path,
    *,
    run_id: str = "r1",
    task_id: str = "t1",
    max_attempts: int = 3,
    lease_ttl_s: float = -1,
) -> Path:
    """Create a run whose task was interrupted mid-attempt."""
    root = build_run_dir(tmp_path, run_id)
    db = Database(root / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    state.create_run(run_id)
    spec = TaskSpec(
        id=task_id,
        run_id=run_id,
        title=task_id,
        objective="interrupted work",
        budget=Budget(max_attempts=max_attempts),
    )
    state.upsert_task(spec)
    state.transition_task(task_id, TaskStatus.READY)
    state.start_attempt(task_id, owner="dead-worker", lease_ttl_s=lease_ttl_s)
    state.transition_task(task_id, TaskStatus.RUNNING, attempt=1)
    db.close()
    return root


def test_find_run_locates_a_stored_run(tmp_path: Path) -> None:
    config = RunConfig(
        run_id="run_find",
        repo_path=str(tmp_path),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "m"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        orchestrator.state.create_run("run_find")
    finally:
        orchestrator.close()
    assert find_run("run_find", tmp_path / "data") is not None
    assert find_run("run_missing", tmp_path / "data") is None


def test_recovery_of_an_unknown_run_is_safe(tmp_path: Path) -> None:
    report = recover_run("run_nope", data_dir=tmp_path / "data")
    assert report.found is False
    assert report.resumable is False


def test_the_projection_is_rebuilt_from_the_event_log(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    state.create_run("r1")

    spec = TaskSpec(
        id="t1", run_id="r1", title="t", objective="o", budget=Budget(max_attempts=2)
    )
    state.upsert_task(spec)
    state.transition_task("t1", TaskStatus.READY)
    state.start_attempt("t1", owner="w", lease_ttl_s=60)
    state.transition_task("t1", TaskStatus.RUNNING, attempt=1)

    # Corrupt the projection to simulate a crash between the event append
    # and the projection update.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state_json = ? WHERE task_id = ?",
            (json.dumps({"id": "t1", "run_id": "r1", "status": "pending"}), "t1"),
        )
    assert state.get_task_state("t1").status is TaskStatus.PENDING

    rebuilt = state.rebuild_projection("r1")
    assert rebuilt["tasks"] == 1
    assert rebuilt["events_replayed"] >= 3
    assert state.get_task_state("t1").status is TaskStatus.RUNNING
    assert state.get_task_state("t1").attempt == 1
    db.close()


def test_recovery_returns_interrupted_tasks_to_the_queue(tmp_path: Path) -> None:
    seed_interrupted_task(tmp_path)
    report = recover_run("r1", data_dir=tmp_path / "data")
    assert report.found
    assert "t1" in report.interrupted_tasks
    assert "t1" in report.resumable_tasks
    assert report.resumable is True
    assert any("ready queue" in action for action in report.actions)


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    seed_interrupted_task(tmp_path)
    first = recover_run("r1", data_dir=tmp_path / "data")
    second = recover_run("r1", data_dir=tmp_path / "data")
    assert first.resumable_tasks == ["t1"]
    # The second pass finds the task already queued: the *state* is
    # unchanged, which is what idempotency means here.
    assert second.resumable_tasks == ["t1"]
    db = Database(tmp_path / "data" / "runs" / "r1" / "state.db")
    state = StateStore(db, EventLog(db))
    assert state.get_task_state("t1").status is TaskStatus.READY
    assert state.get_task_state("t1").attempt == 1
    db.close()


def test_a_task_with_no_attempts_left_is_blocked(tmp_path: Path) -> None:
    seed_interrupted_task(tmp_path, max_attempts=1)
    report = recover_run("r1", data_dir=tmp_path / "data")
    assert "t1" in report.exhausted_tasks
    assert "t1" not in report.resumable_tasks


def test_a_live_lease_is_not_resumed(tmp_path: Path) -> None:
    seed_interrupted_task(tmp_path, lease_ttl_s=600)
    report = recover_run("r1", data_dir=tmp_path / "data")
    assert "t1" in report.interrupted_tasks
    assert "t1" not in report.resumable_tasks
    assert any("live lease" in action for action in report.actions)


def test_unresolved_side_effects_are_reported(tmp_path: Path) -> None:
    seed_interrupted_task(tmp_path)
    unresolved = verify_side_effects("r1", data_dir=tmp_path / "data")
    assert len(unresolved) == 1
    assert unresolved[0]["task_id"] == "t1"
    assert unresolved[0]["idempotency_key"] == "t1#1"


def test_a_completed_attempt_is_not_reported_as_unresolved(tmp_path: Path) -> None:
    root = build_run_dir(tmp_path)
    db = Database(root / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    state.create_run("r1")
    spec = TaskSpec(
        id="t1", run_id="r1", title="t", objective="o", budget=Budget(max_attempts=3)
    )
    state.upsert_task(spec)
    state.transition_task("t1", TaskStatus.READY)
    state.start_attempt("t1", owner="w", lease_ttl_s=60)
    log.append(
        EventType.TASK_ATTEMPT_FINISHED,
        run_id="r1",
        task_id="t1",
        attempt=1,
        payload={"ok": True},
    )
    db.close()
    assert verify_side_effects("r1", data_dir=tmp_path / "data") == []


@pytest.mark.integration
async def test_a_completed_run_recovers_with_nothing_to_resume(
    tmp_path: Path, repo: Path
) -> None:
    config = RunConfig(
        run_id="run_done",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        await orchestrator.run("done", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    report = recover_run("run_done", data_dir=tmp_path / "data")
    assert report.resumable is False
    assert sorted(report.completed_tasks) == ["one", "two"]


@pytest.mark.integration
async def test_resume_continues_an_interrupted_run(tmp_path: Path, repo: Path) -> None:
    """A run whose worker was interrupted must be resumable."""
    run_id = "run_resume"
    root = build_run_dir(tmp_path, run_id)
    config = RunConfig(
        run_id=run_id,
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    (root / "run-config.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")

    db = Database(root / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    state.create_run(run_id)
    spec = TaskSpec(
        id="one",
        run_id=run_id,
        title="one",
        objective="interrupted work",
        budget=Budget(max_attempts=3),
    )
    state.upsert_task(spec)
    state.transition_task("one", TaskStatus.READY)
    state.start_attempt("one", owner="dead-worker", lease_ttl_s=-1)
    state.transition_task("one", TaskStatus.RUNNING, attempt=1)
    db.close()

    report = recover_run(run_id, data_dir=tmp_path / "data")
    assert "one" in report.resumable_tasks

    result = await resume_run(run_id, data_dir=tmp_path / "data")
    assert result["resumed"] is True
    assert result["status"] in ("succeeded", "completed_with_failures")


@pytest.mark.slow
def test_a_killed_process_leaves_a_recoverable_run(tmp_path: Path, repo: Path) -> None:
    """Kill the process mid-run and check the run can be recovered."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = tmp_path / "run_once.py"
    script.write_text(
        "\n".join(
            [
                "import asyncio, sys",
                f"sys.path.insert(0, {src!r})",
                "from agent_squad.config import RunConfig",
                "from agent_squad.orchestrator import Orchestrator",
                "config = RunConfig(",
                "    run_id='run_kill',",
                f"    repo_path={str(repo)!r},",
                f"    data_dir={str(tmp_path / 'data')!r},",
                "    provider={'name': 'mock', 'model': 'm'},",
                ")",
                "orch = Orchestrator(config, data_dir=" + repr(str(tmp_path / "data")) + ")",
                "asyncio.run(orch.run('kill me', request_spec="
                + repr(two_independent_spec())
                + "))",
            ]
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = src
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

    report = recover_run("run_kill", data_dir=tmp_path / "data")
    assert report.found is True
    # Whatever state the process reached, the record must be readable and
    # consistent enough to reason about.
    assert report.actions
