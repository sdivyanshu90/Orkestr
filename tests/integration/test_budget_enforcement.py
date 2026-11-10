"""Budget enforcement tests.

The budget manager must refuse a call *before* it is issued when the
remaining allowance cannot cover it, and the refusal must reach the task as
an explicit budget failure rather than an unbounded spend.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import two_independent_spec

from agent_squad.config import BudgetConfig, ProviderConfig, RunConfig
from agent_squad.orchestrator import Orchestrator
from agent_squad.providers.budget import BudgetManager


def test_the_run_budget_refuses_a_call_that_would_overrun_it() -> None:
    manager = BudgetManager(
        BudgetConfig(run_token_limit=1000), ProviderConfig(name="mock", model="m")
    )
    manager.set_scope_limits("run", token_limit=1000)
    manager.record("run", _usage(900))
    with pytest.raises(Exception) as exc:
        manager.check("run", extra_tokens=200)
    assert "token budget exhausted" in str(exc.value)


def test_a_scope_without_a_limit_is_never_refused() -> None:
    manager = BudgetManager(BudgetConfig(), ProviderConfig(name="mock", model="m"))
    manager.check("run", extra_tokens=10_000_000)


def _usage(tokens: int):
    from agent_squad.providers.base import Usage

    return Usage(input_tokens=tokens, output_tokens=0, estimated=False)


@pytest.mark.integration
async def test_a_run_with_a_tiny_token_budget_fails_explicitly(
    tmp_path: Path, repo: Path
) -> None:
    """A budget that cannot cover the work must produce a budget failure."""
    config = RunConfig(
        run_id="run_budget",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        budget={"run_token_limit": 10},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("budgeted work", request_spec=two_independent_spec())
        states = orchestrator.state.list_task_states(config.run_id)
    finally:
        await orchestrator.aclose()

    assert outcome.status.value != "succeeded"
    failed = [s for s in states if s.error_class == "budget"]
    assert failed, "the budget overrun was not recorded as a budget failure"


@pytest.mark.integration
async def test_a_per_task_budget_fails_only_that_task(tmp_path: Path, repo: Path) -> None:
    """A per-task ceiling must be enforced independently per task."""

    config = RunConfig(
        run_id="run_task_budget",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        spec = two_independent_spec()
        spec["tasks"][0]["token_budget"] = 5
        await orchestrator.run("task budget", request_spec=spec)
        states = orchestrator.state.list_task_states(config.run_id)
    finally:
        await orchestrator.aclose()

    by_id = {s.id: s for s in states}
    assert by_id["one"].error_class == "budget"
    # The other task had no per-task ceiling and should have succeeded.
    assert by_id["two"].status.value == "succeeded"
