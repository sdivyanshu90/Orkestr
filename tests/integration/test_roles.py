"""Role coverage.

Four agent roles exist: supervisor, implementer, test engineer and
reviewer.  Only two of them were ever exercised end to end, which means
their tool sets, their capabilities and their isolation guarantees were
untested in practice.

These tests drive each role through a real run and assert the properties
that distinguish them:

* the reviewer receives **no** write tool, so it cannot modify the tree;
* the reviewer's capability set excludes write and delete;
* the test engineer receives the write tool and may create test files;
* the supervisor receives no write tool either -- it plans, it does not
  implement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.agents.runner import WorkerRunner
from agent_squad.config import RunConfig
from agent_squad.models.task import Role
from agent_squad.orchestrator import Orchestrator
from agent_squad.security.capabilities import Capability, capabilities_for_role


def build_tools_for(role: Role) -> set[str]:
    runner = WorkerRunner.__new__(WorkerRunner)
    return set(runner.build_tools(role).names())


def test_the_tool_sets_differ_by_role() -> None:
    implementer = build_tools_for(Role.IMPLEMENTER)
    test_engineer = build_tools_for(Role.TEST_ENGINEER)
    reviewer = build_tools_for(Role.REVIEWER)
    supervisor = build_tools_for(Role.SUPERVISOR)

    assert "write_file" in implementer
    assert "write_file" in test_engineer
    assert "write_file" not in reviewer
    assert "write_file" not in supervisor


def test_every_role_can_read_and_run_commands() -> None:
    for role in (Role.IMPLEMENTER, Role.TEST_ENGINEER, Role.REVIEWER, Role.SUPERVISOR):
        tools = build_tools_for(role)
        assert "read_file" in tools
        assert "run_command" in tools


def test_the_reviewer_cannot_write_or_delete() -> None:
    caps = capabilities_for_role(Role.REVIEWER)
    assert not caps.allows(Capability.FS_WRITE)
    assert not caps.allows(Capability.FS_DELETE)
    assert not caps.allows(Capability.GIT_COMMIT)
    assert not caps.allows(Capability.GIT_PUSH)
    assert caps.allows(Capability.FS_READ)
    assert caps.allows(Capability.EXEC_RUN)


def test_the_supervisor_cannot_write() -> None:
    caps = capabilities_for_role(Role.SUPERVISOR)
    assert not caps.allows(Capability.FS_WRITE)
    assert caps.allows(Capability.BROADCAST)


def test_the_implementer_may_commit_but_only_with_approval() -> None:
    caps = capabilities_for_role(Role.IMPLEMENTER)
    assert caps.allows(Capability.GIT_COMMIT)
    assert caps.allows(Capability.GIT_PUSH)


@pytest.mark.integration
async def test_a_reviewer_task_runs_and_produces_no_patch(
    tmp_path: Path, repo: Path
) -> None:
    """A reviewer reads and reports; it must not modify the tree."""
    spec = {
        "objective": "Review the existing module",
        "tasks": [
            {
                "id": "review",
                "title": "Review calc.py",
                "objective": "Read calc.py and report on it",
                "role": "reviewer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "review-ran",
                        "description": "the reviewer produced a report",
                        "kind": "file_exists",
                        "spec": {"path": "calc.py"},
                    }
                ],
            }
        ],
    }
    config = RunConfig(
        run_id="run_reviewer",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("review", request_spec=spec)
        states = orchestrator.state.list_task_states(config.run_id)
        patches = [
            a
            for a in orchestrator.state.list_artifacts(config.run_id)
            if a.kind.value == "patch"
        ]
    finally:
        await orchestrator.aclose()

    assert outcome.status.value == "succeeded"
    state = next(s for s in states if s.id == "review")
    assert state.status.value == "succeeded"
    # The reviewer made no changes, so there is no patch artifact.
    assert patches == []
    # The repository is untouched.
    assert "round_half_up" not in (repo / "calc.py").read_text(encoding="utf-8")


@pytest.mark.integration
async def test_a_reviewer_records_an_advisory_opinion(
    tmp_path: Path, repo: Path
) -> None:
    """A reviewer's judgement is advisory and is never counted as verified."""
    spec = {
        "objective": "Review the existing module",
        "tasks": [
            {
                "id": "review",
                "title": "Review calc.py",
                "objective": "Read calc.py and report on it",
                "role": "reviewer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "advisory",
                        "description": "a human-readable opinion",
                        "kind": "llm_review",
                        "spec": {},
                    }
                ],
            }
        ],
    }
    config = RunConfig(
        run_id="run_advisory",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        await orchestrator.run("review", request_spec=spec)
        state = orchestrator.state.get_task_state("review")
    finally:
        await orchestrator.aclose()

    assert state is not None
    verification = state.verification
    assert verification is not None
    # The advisory criterion is reported as advisory, not as passed.
    assert verification["criterion_states"]["advisory"] == "advisory"
    assert "advisory" in verification["advisory"]


@pytest.mark.integration
async def test_a_test_engineer_may_create_test_files(
    tmp_path: Path, repo: Path
) -> None:
    spec = {
        "objective": "Add a test",
        "tasks": [
            {
                "id": "tests",
                "title": "Add a test",
                "objective": "Create tests/test_extra.py",
                "role": "test_engineer",
                "depends_on": [],
                "edits": [
                    {
                        "path": "tests/test_extra.py",
                        "content": (
                            "from calc import add\n\n\n"
                            "def test_add_extra():\n    assert add(1, 1) == 2\n"
                        ),
                    }
                ],
                "acceptance_criteria": [
                    {
                        "id": "test-pass",
                        "description": "the new test passes",
                        "kind": "pytest_selector",
                        "spec": {"selector": "tests/test_extra.py"},
                    }
                ],
            }
        ],
    }
    config = RunConfig(
        run_id="run_test_engineer",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("tests", request_spec=spec)
    finally:
        await orchestrator.aclose()

    assert outcome.status.value == "succeeded"
    report = outcome.outcomes["tests"].verification
    assert report is not None
    assert report.passed
    checks = {c.criterion_id: c for c in report.checks}
    assert checks["test-pass"].exit_code == 0
