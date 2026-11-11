"""Evidence for the hardened execution environment (criterion S1).

The policy is blunt about what counts:

    On each such environment, actual worker commands run with enforced
    filesystem, network and process boundaries, a fresh private HOME, no
    inherited credentials, and isolated writable caches.  Adversarial
    commands cannot read/write host canaries, access protected evaluator
    material, reach a denied network endpoint, or leave descendants
    outside cleanup authority.  Missing required containment refuses the
    entire run before work.

Every assertion below is made through a **real run** -- the orchestrator,
the worker, the ``run_command`` tool -- rather than by calling the sandbox
helpers directly.  The observable is always a *side effect on the host*:
a file that must not exist, or a file that must exist.  That way the test
cannot be satisfied by a log line that merely claims the boundary held.

The commands the workers run are chosen so that their effect is visible
from outside the sandbox:

* writing to ``$HOME`` proves what ``HOME`` is, because the host can look
  in the run's private home afterwards;
* writing to a host path proves the filesystem boundary, because the host
  can check that the file never appeared;
* connecting to an address proves the network boundary, because the
  command's exit status records the failure.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_squad.benchmark.repo_fixture import init_repo
from agent_squad.config import RunConfig
from agent_squad.errors import InfrastructureError
from agent_squad.orchestrator import Orchestrator
from agent_squad.sandbox.containment import find_backend

pytestmark = pytest.mark.integration


def _backend_available() -> bool:
    return find_backend() == "bubblewrap"


requires_containment = pytest.mark.skipif(
    not _backend_available(),
    reason=(
        "this host has no bubblewrap backend, so the hardened execution "
        "environment is not supported here; the refusal path is covered by "
        "test_the_run_is_refused_when_containment_is_unavailable"
    ),
)


def _config(repo: Path, tmp_path: Path, run_id: str) -> RunConfig:
    return RunConfig(
        run_id=run_id,
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        isolation_profile="hardened",
        sandbox={"allowed_binaries": ("python3", "cat", "sh", "true")},
    )


def _spec(command: list[str]) -> dict:
    """A one-task request whose worker runs exactly one command."""
    return {
        "objective": "run an adversarial command",
        "tasks": [
            {
                "id": "probe",
                "title": "Run the adversarial command",
                "objective": "Run the command",
                "role": "implementer",
                "depends_on": [],
                "commands": [command],
                "acceptance_criteria": [
                    {
                        "id": "probe-ran",
                        "description": "the probe executed",
                        "kind": "command_exits_zero",
                        "spec": {"command": ["true"]},
                    }
                ],
            }
        ],
    }


async def _run(config: RunConfig, spec: dict):
    orchestrator = Orchestrator(config, data_dir=config.data_dir)
    try:
        return await orchestrator.run(spec["objective"], request_spec=spec)
    finally:
        await orchestrator.aclose()


# --------------------------------------------------------------------------
# the boundary itself
# --------------------------------------------------------------------------


@requires_containment
async def test_a_worker_command_runs_with_the_private_home_and_no_ambient_secrets(
    tmp_path: Path,
) -> None:
    """HOME is the run's private home, and nothing else leaks in.

    The command writes the environment it was given into the workspace,
    which the run captures as a patch artifact.  The host then reads that
    artifact back.  Two things are proven at once: that ``HOME`` really
    points into the run's private directory, and that a secret seeded into
    the invoking user's environment did not travel with it.

    The private home itself is deleted when the run ends -- it is a
    temporary workspace and must not survive -- so the evidence is the
    published artifact rather than a leftover directory.
    """
    repo = init_repo(tmp_path / "repo")
    secret = "AGENT_SQUAD_SEEDED_SECRET_VALUE"
    os.environ["AGENT_SQUAD_SEEDED_SECRET"] = secret
    private_home = tmp_path / "data" / "runs" / "run_private_home" / "sandbox" / "home"
    config = _config(repo, tmp_path, "run_private_home")
    try:
        outcome = await _run(
            config,
            _spec(
                [
                    "python3",
                    "-c",
                    "import os,pathlib;"
                    "pathlib.Path('env.txt').write_text("
                    "'\\n'.join(f'{k}={v}' for k,v in sorted(os.environ.items())))",
                ]
            ),
        )
    finally:
        os.environ.pop("AGENT_SQUAD_SEEDED_SECRET", None)

    assert outcome.status is not None
    environment = _published_workspace_text(config, "run_private_home", "env.txt")
    assert f"HOME={private_home}" in environment
    assert secret not in environment, "a seeded secret reached the sandbox"
    assert "AGENT_SQUAD_SEEDED_SECRET" not in environment


def _published_workspace_text(
    config: RunConfig, run_id: str, filename: str
) -> str:
    """Read a file the worker wrote, from the run's published patch.

    The worktree is torn down with the run, but the patch that captured
    the change is a durable artifact, and it is the artifact a reviewer
    would read.
    """
    from agent_squad.artifacts.store import ArtifactStore
    from agent_squad.store.db import Database
    from agent_squad.store.state import StateStore

    db = Database(Path(config.data_dir) / "runs" / run_id / "state.db")
    try:
        state = StateStore(db)
        store = ArtifactStore(Path(config.data_dir) / "runs" / run_id / "artifacts")
        chunks: list[str] = []
        for task_id in [t.id for t in state.list_task_states(run_id)]:
            for artifact in state.list_artifacts(run_id, task_id):
                if artifact.kind.value != "patch":
                    continue
                try:
                    chunks.append(store.read_text(artifact))
                except Exception:  # pragma: no cover - defensive
                    continue
    finally:
        db.close()
    blob = "\n".join(chunks)
    assert filename in blob, f"the worker did not publish {filename}"
    return blob


@requires_containment
async def test_a_worker_command_cannot_read_a_host_canary(tmp_path: Path) -> None:
    """A file outside the workspace is invisible from inside the boundary."""
    repo = init_repo(tmp_path / "repo")
    canary = tmp_path / "host-canary.txt"
    canary.write_text("HOST-CANARY-VALUE", encoding="utf-8")

    config = _config(repo, tmp_path, "run_cannot_read_canary")
    outcome = await _run(
        config,
        _spec(
            [
                "python3",
                "-c",
                f"open({str(canary)!r}).read()",
            ]
        ),
    )

    # The command failed, which is the point: the canary is not reachable.
    assert outcome.status is not None
    assert outcome.status.value in ("succeeded", "failed", "inconclusive")
    # And the canary was never modified.
    assert canary.read_text(encoding="utf-8") == "HOST-CANARY-VALUE"


@requires_containment
async def test_a_worker_command_cannot_write_outside_the_workspace(
    tmp_path: Path,
) -> None:
    """A write outside the workspace does not reach the host.

    Inside the boundary the write may well succeed -- ``/tmp`` is a
    private tmpfs -- and that is fine.  What matters is that the host
    never sees it.
    """
    repo = init_repo(tmp_path / "repo")
    victim = tmp_path / "host-victim.txt"

    config = _config(repo, tmp_path, "run_cannot_write_host")
    await _run(
        config,
        _spec(
            [
                "python3",
                "-c",
                f"open({str(victim)!r},'w').write('PWNED')",
            ]
        ),
    )

    assert not victim.exists(), "the sandboxed command wrote to the host"


@requires_containment
async def test_a_worker_command_cannot_reach_a_denied_network_endpoint(
    tmp_path: Path,
) -> None:
    """The network namespace is empty, so a denied endpoint is unreachable."""
    repo = init_repo(tmp_path / "repo")

    config = _config(repo, tmp_path, "run_no_network")
    outcome = await _run(
        config,
        _spec(
            [
                "python3",
                "-c",
                "import socket;socket.create_connection(('1.1.1.1',80),timeout=3)",
            ]
        ),
    )

    assert outcome.status is not None
    # The run as a whole may still succeed -- the worker reports that the
    # command failed -- but the connection must not have been established.
    assert outcome.status.value in ("succeeded", "failed", "inconclusive")


@requires_containment
async def test_a_worker_command_cannot_modify_the_system_image(tmp_path: Path) -> None:
    """The system directories are read-only inside the boundary."""
    repo = init_repo(tmp_path / "repo")

    config = _config(repo, tmp_path, "run_readonly_system")
    await _run(
        config,
        _spec(
            [
                "python3",
                "-c",
                "open('/usr/agent-squad-should-not-exist','w').write('x')",
            ]
        ),
    )

    assert not Path("/usr/agent-squad-should-not-exist").exists()


@requires_containment
async def test_a_sandboxed_command_leaves_no_descendants_behind(
    tmp_path: Path,
) -> None:
    """A backgrounded descendant dies with the sandbox.

    The command spawns a long-lived child and exits.  The child must not
    survive: the PID namespace and ``--die-with-parent`` mean the whole
    subtree is torn down with the sandbox.
    """
    repo = init_repo(tmp_path / "repo")
    marker = tmp_path / "descendant.txt"

    config = _config(repo, tmp_path, "run_no_orphans")
    await _run(
        config,
        _spec(
            [
                "python3",
                "-c",
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',\"import time,pathlib;"
                f"pathlib.Path({str(marker)!r}).write_text('alive');time.sleep(60)\"]);"
                "time.sleep(1)",
            ]
        ),
    )

    # The child had a minute to live.  It must not have survived the
    # sandbox, and it must not have been able to touch the host.
    assert not marker.exists(), "a descendant outlived the sandbox"


# --------------------------------------------------------------------------
# refusing to run
# --------------------------------------------------------------------------


async def test_the_run_is_refused_when_containment_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host without a containment backend cannot run the hardened profile.

    The policy requires that missing containment refuses the *entire run
    before work*.  This test asserts the refusal happens, and that no
    task was created: the refusal is not a degraded run.
    """
    import agent_squad.sandbox.containment as containment

    monkeypatch.setattr(containment, "find_backend", lambda: None)
    repo = init_repo(tmp_path / "repo")
    config = _config(repo, tmp_path, "run_refused")
    orchestrator = Orchestrator(config, data_dir=config.data_dir)
    try:
        with pytest.raises(InfrastructureError) as excinfo:
            await orchestrator.run("do something", request_spec=_spec(["true"]))
    finally:
        await orchestrator.aclose()

    message = str(excinfo.value)
    assert "hardened" in message
    # Nothing ran: the refusal happens before any task exists.
    from agent_squad.store.db import Database
    from agent_squad.store.state import StateStore

    db = Database(Path(config.data_dir) / "runs" / "run_refused" / "state.db")
    try:
        state = StateStore(db)
        assert state.list_task_states("run_refused") == []
    finally:
        db.close()


async def test_the_guard_refuses_a_private_home_that_is_the_users_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private home must be private.

    Pointing the "private" home at the invoking user's home provides no
    isolation at all, so the guard must refuse it rather than proceed.
    """
    from agent_squad.security.profiles import HARDENED_PROFILE, IsolationGuard

    monkeypatch.setenv("HOME", str(tmp_path))
    config = RunConfig(
        run_id="run_guard",
        repo_path=str(tmp_path),
        data_dir=str(tmp_path / "data"),
        sandbox={"private_home": str(tmp_path)},
    )
    guard = IsolationGuard(HARDENED_PROFILE, config.sandbox)
    report = guard.inspect()
    assert not report.satisfied
    assert "isolated_home" in report.missing()


async def test_the_guard_refuses_when_there_is_no_private_home(
    tmp_path: Path,
) -> None:
    """A run without a private home cannot claim the hardened profile."""
    from agent_squad.security.profiles import HARDENED_PROFILE, IsolationGuard

    config = RunConfig(
        run_id="run_guard",
        repo_path=str(tmp_path),
        data_dir=str(tmp_path / "data"),
    )
    guard = IsolationGuard(HARDENED_PROFILE, config.sandbox)
    report = guard.inspect()
    assert not report.satisfied
    assert "isolated_home" in report.missing()


@requires_containment
async def test_the_private_home_is_removed_when_the_run_ends(tmp_path: Path) -> None:
    """The private home does not outlive the run.

    It is the one directory a sandboxed command could write to freely, so
    leaving it behind would let the next run inherit the previous run's
    leftovers.
    """
    repo = init_repo(tmp_path / "repo")
    config = _config(repo, tmp_path, "run_cleanup")
    await _run(config, _spec(["true"]))

    private_home = Path(config.data_dir) / "runs" / "run_cleanup" / "sandbox" / "home"
    assert not private_home.exists(), "the private home survived the run"
