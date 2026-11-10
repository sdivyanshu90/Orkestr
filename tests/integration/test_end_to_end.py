"""End-to-end test: a real run with the deterministic mock provider.

This exercises the whole pipeline -- planning, scheduling, worktree
isolation, patch capture, independent verification, patch integration and the
evidence chain -- without any network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.models.run import RunStatus
from agent_squad.orchestrator import Orchestrator


@pytest.mark.integration
async def test_end_to_end_run_succeeds(run_config, spec, tmp_path: Path) -> None:
    orchestrator = Orchestrator(run_config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run(
            spec["objective"],
            description=spec["description"],
            request_spec=spec,
        )
    finally:
        await orchestrator.aclose()

    assert outcome.status is RunStatus.SUCCEEDED, outcome.to_dict()
    assert outcome.summary.succeeded == 2
    assert outcome.summary.failed == 0

    # Both tasks were verified independently of the worker's claims.
    for task_outcome in outcome.outcomes.values():
        assert task_outcome.status.value == "succeeded"
        assert task_outcome.verification is not None
        assert task_outcome.verification.passed
        assert task_outcome.verification.level == "checks_passed"

    # The pytest selector criterion really ran.
    verification = outcome.outcomes["tests"].verification
    assert verification is not None
    checks = {c.criterion_id: c for c in verification.checks}
    assert "tests-pass" in checks
    assert checks["tests-pass"].exit_code == 0
    assert checks["tests-pass"].command[0] == "python3"

    # The integration boundary ran the combined test suite.
    assert outcome.integration is not None
    assert outcome.integration.passed, outcome.integration.to_dict()
    assert outcome.integration.conflict_count == 0
    assert len(outcome.integration.applied) == 2

    # The repository itself was not modified: workers only touched worktrees.
    working = (Path(run_config.repo_path) / "calc.py").read_text(encoding="utf-8")
    assert "round_half_up" not in working


@pytest.mark.integration
async def test_run_records_evidence_and_artifacts(run_config, spec, tmp_path: Path) -> None:
    orchestrator = Orchestrator(run_config, data_dir=tmp_path / "data")
    try:
        await orchestrator.run(spec["objective"], request_spec=spec)
        report = orchestrator.run_report(run_config.run_id)
        kinds = {e.type.value for e in orchestrator.log.read(run_config.run_id)}
    finally:
        await orchestrator.aclose()

    assert report["event_count"] > 10
    assert "run.plan_created" in kinds
    assert "run.strategy_selected" in kinds
    assert "task.attempt_started" in kinds
    assert "patch.captured" in kinds
    assert "verification.passed" in kinds
    assert "integration.verified" in kinds

    artifacts = report["artifacts"]
    patches = [a for a in artifacts if a["kind"] == "patch"]
    assert len(patches) == 2
    for patch in patches:
        assert len(patch["sha256"]) == 64
        assert patch["provenance"]["base_revision"]
        assert patch["provenance"]["task_id"]
        assert patch["metadata"]["files"]


@pytest.mark.integration
async def test_patch_bundle_is_reviewable(run_config, spec, tmp_path: Path) -> None:
    orchestrator = Orchestrator(run_config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run(spec["objective"], request_spec=spec)
    finally:
        await orchestrator.aclose()

    assert outcome.patch_bundle is not None
    assert "# agent-squad patch bundle" in outcome.patch_bundle
    assert "diff --git" in outcome.patch_bundle

    bundle_path = Path(run_config.data_dir) / "runs" / run_config.run_id / "patch-bundle.diff"
    assert bundle_path.exists()
    assert "round_half_up" in bundle_path.read_text(encoding="utf-8")
