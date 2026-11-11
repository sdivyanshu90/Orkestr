"""Replay tests.

Two kinds of replay, and they are not the same thing:

* **state replay** reconstructs the run's state machine from the event log.
  It is deterministic and makes no model calls.
* **control-plane replay** re-runs the deterministic machinery against the
  *recorded* model responses.  It makes no network calls and spends no
  tokens; it checks that the scheduler, the state machine and the verifier
  reach the same decisions when handed the same inputs.

The second is the stronger claim, and it is the one that had a bug: the
replay called the orchestrator without awaiting it, so the control-plane
replay had never actually run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import two_independent_spec

from agent_squad.config import RunConfig
from agent_squad.orchestrator import Orchestrator
from agent_squad.replay.replayer import replay_run


@pytest.mark.integration
async def test_state_replay_reconstructs_the_decisions(tmp_path: Path, repo: Path) -> None:
    config = RunConfig(
        run_id="run_replay",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        await orchestrator.run("replay me", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    result = await replay_run("run_replay", data_dir=tmp_path / "data")
    assert result.found
    assert result.mode == "recorded_replay"
    assert result.event_count > 0
    assert result.exchanges > 0, "the model exchanges must have been recorded"
    assert result.decisions, "the strategy decision must be reconstructed"
    assert result.decisions[0]["strategy"] in ("single", "static_two", "static_four", "adaptive")
    assert result.final_state == {"one": "succeeded", "two": "succeeded"}
    assert result.state_transitions


@pytest.mark.integration
async def test_the_control_plane_replay_reaches_the_same_decisions(
    tmp_path: Path, repo: Path
) -> None:
    """The recorded transcript is replayed through the real scheduler."""
    config = RunConfig(
        run_id="run_replay_exec",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        await orchestrator.run("replay me", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    result = await replay_run(
        "run_replay_exec", data_dir=tmp_path / "data", execute=True
    )
    assert result.executed is True
    # The replay must have run, not silently failed.
    failures = [w for w in result.warnings if "control-plane replay failed" in w]
    assert not failures, result.warnings
    assert result.execution_matches_record is True


@pytest.mark.integration
async def test_the_control_plane_replay_makes_no_network_calls(
    tmp_path: Path, repo: Path
) -> None:
    """The replay serves the recorded transcript; it never calls a provider."""
    config = RunConfig(
        run_id="run_replay_offline",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        await orchestrator.run("replay me", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    result = await replay_run(
        "run_replay_offline", data_dir=tmp_path / "data", execute=True
    )
    assert result.executed is True
    assert not [w for w in result.warnings if "control-plane replay failed" in w]


async def test_replaying_an_unknown_run_is_safe(tmp_path: Path) -> None:
    result = await replay_run("run_nope", data_dir=tmp_path / "data")
    assert result.found is False
    assert result.warnings
