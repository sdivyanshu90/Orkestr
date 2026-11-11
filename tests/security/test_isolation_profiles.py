"""Isolation profile tests.

The hardened profile's promise is only worth something if it is enforced.
These tests assert the two halves of that:

* a profile that *requires* a boundary refuses to run when the boundary is
  unavailable, rather than degrading silently;
* a profile that does not require it says so, and runs.

The network-isolation check is exercised against the *real* host, so the
result depends on what the machine can actually do -- which is the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.config import RunConfig, SandboxConfig
from agent_squad.errors import InfrastructureError
from agent_squad.security.profiles import (
    DEVELOPMENT_PROFILE,
    HARDENED_PROFILE,
    IsolationGuard,
    IsolationProfile,
    get_profile,
    hardened_environment,
)


def test_the_profiles_are_addressable_by_name() -> None:
    assert get_profile("development").name is IsolationProfile.DEVELOPMENT
    assert get_profile("hardened").name is IsolationProfile.HARDENED
    assert get_profile(IsolationProfile.HARDENED).name is IsolationProfile.HARDENED


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(InfrastructureError):
        get_profile("magic")


def test_the_development_profile_requires_nothing() -> None:
    report = IsolationGuard(DEVELOPMENT_PROFILE).inspect()
    assert report.satisfied
    assert report.missing() == []


def test_the_hardened_profile_reports_what_is_missing() -> None:
    report = IsolationGuard(HARDENED_PROFILE).inspect()
    # The report names each boundary and says whether it is available.
    names = {b.name for b in report.boundaries}
    assert {"network_isolation", "isolated_home", "no_ambient_git"} <= names
    if not report.satisfied:
        assert "network_isolation" in report.missing()
        detail = next(
            b for b in report.boundaries if b.name == "network_isolation"
        ).detail
        assert detail, "a missing boundary must explain itself"


def test_the_hardened_profile_refuses_to_run_when_a_boundary_is_missing() -> None:
    """Failing closed is the whole point."""
    report = IsolationGuard(HARDENED_PROFILE).inspect()
    if report.satisfied:
        pytest.skip("this host can satisfy the hardened profile")
    with pytest.raises(InfrastructureError) as exc:
        IsolationGuard(HARDENED_PROFILE).check()
    message = str(exc.value)
    assert "Refusing to run" in message
    assert "network_isolation" in message


def test_the_hardened_profile_passes_when_the_boundaries_hold() -> None:
    guard = IsolationGuard(HARDENED_PROFILE)
    report = guard.inspect()
    if not report.satisfied:
        pytest.skip("this host cannot satisfy the hardened profile")
    assert guard.check().satisfied


def test_a_profile_that_requires_nothing_never_blocks() -> None:
    assert IsolationGuard(DEVELOPMENT_PROFILE).check().satisfied


def test_the_hardened_environment_disables_ambient_git_and_proxies() -> None:
    env = hardened_environment(SandboxConfig())
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["no_proxy"] == "*"
    assert env["HTTP_PROXY"] == ""


def test_the_sandbox_environment_disables_ambient_git() -> None:
    """The settings are enforced for every command, not requested."""
    from agent_squad.sandbox.limits import build_environment

    env = build_environment(SandboxConfig())
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_run_refuses_to_start_under_an_unsatisfiable_profile(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal happens before any work, not in the middle of it.

    The unsatisfiable condition is *injected* rather than hoped for from
    the host: a test that quietly skips whenever the machine is capable
    is a test that never runs where it matters.  With no containment
    backend the hardened profile cannot keep its promise, so the run must
    refuse and no task may exist afterwards.
    """
    import asyncio

    import agent_squad.sandbox.containment as containment
    from agent_squad.orchestrator import Orchestrator
    from agent_squad.store.db import Database
    from agent_squad.store.state import StateStore

    monkeypatch.setattr(containment, "find_backend", lambda: None)

    config = RunConfig(
        run_id="run_isolation",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        isolation_profile="hardened",
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        with pytest.raises(InfrastructureError):
            asyncio.run(
                orchestrator.run(
                    "an isolated request",
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
            )
    finally:
        asyncio.run(orchestrator.aclose())

    db = Database(tmp_path / "data" / "runs" / "run_isolation" / "state.db")
    try:
        assert StateStore(db).list_task_states("run_isolation") == []
    finally:
        db.close()


def test_the_effective_config_satisfies_the_hardened_profile(
    tmp_path: Path, repo: Path
) -> None:
    """What the orchestrator actually runs with must satisfy the profile.

    This is the complement of the refusal test: the orchestrator creates
    the private home and selects the containment backend *before* the
    guard runs, so the guard must judge the effective configuration, not
    the operator's raw one.
    """
    import asyncio

    from agent_squad.orchestrator import Orchestrator
    from agent_squad.security.profiles import IsolationGuard, get_profile

    config = RunConfig(
        run_id="run_effective",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        isolation_profile="hardened",
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        report = IsolationGuard(
            get_profile("hardened"),
            orchestrator.config.sandbox,
            workspace=orchestrator.paths.worktrees,
            private_home=orchestrator.private_home,
        ).inspect()
        assert report.satisfied, report.to_dict()
        assert not report.missing()
    finally:
        asyncio.run(orchestrator.aclose())


def test_a_development_run_starts_normally(tmp_path: Path, repo: Path) -> None:
    import asyncio

    from agent_squad.orchestrator import Orchestrator

    config = RunConfig(
        run_id="run_dev_isolation",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = asyncio.run(
            orchestrator.run(
                "a request",
                request_spec={
                    "objective": "o",
                    "tasks": [
                        {
                            "id": "t",
                            "title": "t",
                            "objective": "o",
                            "role": "implementer",
                            "depends_on": [],
                            "edits": [{"path": "x.py", "content": "x = 1\n"}],
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
        )
        assert outcome.status.value == "succeeded"
    finally:
        asyncio.run(orchestrator.aclose())
