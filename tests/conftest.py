"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fixtures.repo import init_repo, sample_spec  # noqa: E402

from agent_squad.config import RunConfig  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small git repository with a passing test suite."""
    return init_repo(tmp_path / "repo")


@pytest.fixture()
def spec() -> dict:
    """A two-task structured request with a real dependency."""
    return sample_spec()


@pytest.fixture()
def run_config(tmp_path: Path, repo: Path) -> RunConfig:
    """A mock-provider configuration pointing at the fixture repository."""
    return RunConfig(
        run_id="run_test_0001",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        scheduler={"max_workers": 4},
        verification={
            "integration_test_command": ("python3", "-m", "pytest", "-q")
        },
    )
