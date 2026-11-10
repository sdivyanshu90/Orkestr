"""Concurrency-limit and summary-timestamp tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from agent_squad.config import ProviderConfig, RunConfig
from agent_squad.orchestrator import Orchestrator
from agent_squad.providers.base import ModelResponse
from agent_squad.providers.registry import ProviderRouter

from helpers import two_independent_spec


class SlowProvider:
    """A provider that records how many calls were in flight at once."""

    name = "slow"
    model = "slow"

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak_in_flight = 0
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.05)
            return ModelResponse(text="ok", provider=self.name, model=self.model)
        finally:
            self.in_flight -= 1

    async def aclose(self):
        return None


@pytest.mark.integration
async def test_the_router_enforces_the_provider_concurrency_limit() -> None:
    provider = SlowProvider()
    router = ProviderRouter([provider], [ProviderConfig(name="openai_compat", model="m", max_concurrency=2)])

    await asyncio.gather(*(router.complete(object()) for _ in range(8)))

    assert provider.calls == 8
    assert provider.peak_in_flight <= 2, (
        f"the provider limit was exceeded: {provider.peak_in_flight} concurrent calls"
    )


@pytest.mark.integration
async def test_a_higher_limit_allows_more_concurrency() -> None:
    provider = SlowProvider()
    router = ProviderRouter([provider], [ProviderConfig(name="openai_compat", model="m", max_concurrency=8)])

    await asyncio.gather(*(router.complete(object()) for _ in range(8)))

    assert provider.peak_in_flight > 2


@pytest.mark.integration
async def test_the_run_summary_records_real_timestamps(tmp_path: Path, repo: Path) -> None:
    config = RunConfig(
        run_id="run_timestamps",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("timestamps", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    assert outcome.summary.started_at is not None
    assert outcome.summary.finished_at is not None
    assert outcome.summary.finished_at >= outcome.summary.started_at
    assert outcome.summary.wall_clock_s > 0.0


@pytest.mark.integration
async def test_the_summary_wall_clock_is_consistent_with_the_timestamps(
    tmp_path: Path, repo: Path
) -> None:
    config = RunConfig(
        run_id="run_consistency",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("consistency", request_spec=two_independent_spec())
    finally:
        await orchestrator.aclose()

    measured = (outcome.summary.finished_at - outcome.summary.started_at).total_seconds()
    assert measured == pytest.approx(outcome.summary.wall_clock_s, abs=0.5)
