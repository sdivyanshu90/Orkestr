"""Integration-engine tests.

These exercise the boundary where independently produced changes are
combined.  The cases that matter are the ones where a patch is *not* what
the orchestrator expected: stale bases, overlapping files, and the
distinction between "the patches conflict" and "the patches were computed
against different trees".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import two_independent_spec

from agent_squad.config import RunConfig
from agent_squad.integration.engine import IntegrationEngine
from agent_squad.integration.patch import PatchArtifact
from agent_squad.orchestrator import Orchestrator
from agent_squad.workspace.worktree import WorktreeManager


def make_patch(
    task_id: str,
    *,
    base_revision: str,
    base_tree: str,
    head_tree: str,
    files: tuple[str, ...] = (),
) -> PatchArtifact:
    return PatchArtifact(
        artifact_id=f"art_{task_id}",
        task_id=task_id,
        base_revision=base_revision,
        sha256="0" * 64,
        content="",
        files=files,
        base_tree=base_tree,
        head_tree=head_tree,
    )


def test_two_independent_patches_are_not_stale(tmp_path: Path, repo: Path) -> None:
    """An unrelated patch applied first must not make the second one stale."""
    config = RunConfig(run_id="r", repo_path=str(repo), data_dir=str(tmp_path))
    worktrees = WorktreeManager(config.repo_path, tmp_path / "wt")
    base = worktrees.head_revision()
    base_tree = worktrees.tree_of(base)

    engine = IntegrationEngine(config=config, worktrees=worktrees)
    patches = [
        make_patch("a", base_revision=base, base_tree=base_tree, head_tree=base_tree),
        make_patch("b", base_revision=base, base_tree=base_tree, head_tree=base_tree),
    ]
    stale = engine.detect_stale(patches, base, dependencies={})
    assert stale == []


def test_a_dependent_patch_is_stale_against_the_wrong_base(
    tmp_path: Path, repo: Path
) -> None:
    """A dependent patch must be based on its dependency's head."""
    config = RunConfig(run_id="r", repo_path=str(repo), data_dir=str(tmp_path))
    worktrees = WorktreeManager(config.repo_path, tmp_path / "wt")
    base = worktrees.head_revision()
    base_tree = worktrees.tree_of(base)

    engine = IntegrationEngine(config=config, worktrees=worktrees)
    patches = [
        make_patch(
            "impl",
            base_revision=base,
            base_tree=base_tree,
            head_tree="a" * 40,
        ),
        make_patch(
            "tests",
            base_revision="b" * 40,
            base_tree="c" * 40,  # not the dependency's head tree
            head_tree="d" * 40,
        ),
    ]
    stale = engine.detect_stale(
        patches, base, dependencies={"tests": ("impl",)}
    )
    assert [p.task_id for p in stale] == ["tests"]


def test_overlapping_files_are_detected_before_application(
    tmp_path: Path, repo: Path
) -> None:
    config = RunConfig(run_id="r", repo_path=str(repo), data_dir=str(tmp_path))
    worktrees = WorktreeManager(config.repo_path, tmp_path / "wt")
    engine = IntegrationEngine(config=config, worktrees=worktrees)
    patches = [
        make_patch(
            "a",
            base_revision=worktrees.head_revision(),
            base_tree="",
            head_tree="",
            files=("calc.py",),
        ),
        make_patch(
            "b",
            base_revision=worktrees.head_revision(),
            base_tree="",
            head_tree="",
            files=("calc.py", "other.py"),
        ),
    ]
    overlaps = engine.detect_overlaps(patches)
    assert len(overlaps) == 1
    first, second, shared = overlaps[0]
    assert {first.task_id, second.task_id} == {"a", "b"}
    assert shared == ("calc.py",)


@pytest.mark.integration
async def test_parallel_tasks_integrate_cleanly(tmp_path: Path, repo: Path) -> None:
    """Two independent tasks must both integrate, in any worker order."""
    config = RunConfig(
        run_id="run_parallel",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        scheduler={"max_workers": 4},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("parallel", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    assert outcome.integration is not None
    assert outcome.integration.passed, outcome.integration.to_dict()
    assert len(outcome.integration.applied) == 2
    assert outcome.integration.conflict_count == 0


@pytest.mark.integration
async def test_a_dependent_chain_integrates_in_order(
    tmp_path: Path, repo: Path
) -> None:
    """A stacked patch must be applied on top of its dependency."""
    from agent_squad.benchmark.repo_fixture import sample_spec

    config = RunConfig(
        run_id="run_chain",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        verification={"integration_test_command": ("python3", "-m", "pytest", "-q")},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("chain", request_spec=sample_spec())
    finally:
        await orchestrator.aclose()

    assert outcome.integration is not None
    assert outcome.integration.passed, outcome.integration.to_dict()
    assert len(outcome.integration.applied) == 2
    assert outcome.integration.stale == []


@pytest.mark.integration
async def test_the_patch_bundle_is_reviewable_and_inert(
    tmp_path: Path, repo: Path
) -> None:
    """The bundle must not commit or push anything."""
    from agent_squad.benchmark.repo_fixture import sample_spec

    config = RunConfig(
        run_id="run_bundle",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        before = orchestrator.worktrees.head_revision()
        outcome = await orchestrator.run("bundle", request_spec=sample_spec())
        after = orchestrator.worktrees.head_revision()
    finally:
        await orchestrator.aclose()

    assert before == after, "the repository must not be modified by a run"
    assert outcome.patch_bundle is not None
    assert "diff --git" in outcome.patch_bundle
    assert "# base revision:" in outcome.patch_bundle
