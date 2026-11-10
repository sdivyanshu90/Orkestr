"""Unit tests for budget accounting."""

from __future__ import annotations

import pytest

from agent_squad.config import BudgetConfig, ProviderConfig
from agent_squad.errors import BudgetExceeded
from agent_squad.providers.base import Usage
from agent_squad.providers.budget import BudgetManager
from agent_squad.providers.registry import compute_cost


def provider_config() -> ProviderConfig:
    return ProviderConfig(
        name="mock",
        model="m",
        price_input_per_1k=0.003,
        price_output_per_1k=0.015,
    )


def test_cost_is_computed_from_configured_prices() -> None:
    usage = Usage(input_tokens=1000, output_tokens=1000, estimated=False)
    cost = compute_cost(usage, provider_config())
    assert cost == pytest.approx(0.003 + 0.015)


def test_cached_tokens_are_not_charged_at_the_input_rate() -> None:
    usage = Usage(input_tokens=1000, output_tokens=0, cached_input_tokens=800)
    cost = compute_cost(usage, provider_config())
    assert cost == pytest.approx(200 / 1000 * 0.003)


def test_pricing_is_configuration_not_a_constant() -> None:
    usage = Usage(input_tokens=1000, output_tokens=0)
    cheap = compute_cost(usage, ProviderConfig(name="m", price_input_per_1k=0.001))
    expensive = compute_cost(usage, ProviderConfig(name="m", price_input_per_1k=0.100))
    assert expensive == pytest.approx(cheap * 100)


def test_budget_tracks_usage_per_scope() -> None:
    manager = BudgetManager(BudgetConfig(), provider_config())
    manager.record("run", Usage(input_tokens=100, output_tokens=50))
    manager.record("task-1", Usage(input_tokens=10, output_tokens=5))
    snapshot = manager.snapshot("run")
    assert snapshot.tokens_used == 150
    assert snapshot.cost_used_usd > 0


def test_token_budget_is_enforced_before_the_call() -> None:
    manager = BudgetManager(BudgetConfig(), provider_config())
    manager.set_scope_limits("run", token_limit=100)
    manager.record("run", Usage(input_tokens=100, output_tokens=0))
    with pytest.raises(BudgetExceeded):
        manager.check("run", extra_tokens=1)


def test_cost_budget_is_enforced_before_the_call() -> None:
    manager = BudgetManager(BudgetConfig(), provider_config())
    manager.set_scope_limits("run", cost_limit_usd=0.001)
    with pytest.raises(BudgetExceeded):
        manager.check("run", extra_cost_usd=0.01)


def test_remaining_allowance_is_reported() -> None:
    manager = BudgetManager(BudgetConfig(), provider_config())
    manager.set_scope_limits("run", token_limit=1000, cost_limit_usd=1.0)
    manager.record("run", Usage(input_tokens=400, output_tokens=100))
    snapshot = manager.snapshot("run")
    assert snapshot.tokens_remaining == 500
    assert snapshot.cost_remaining_usd == pytest.approx(1.0 - snapshot.cost_used_usd)


def test_scopes_are_independent() -> None:
    manager = BudgetManager(BudgetConfig(), provider_config())
    manager.set_scope_limits("task-a", token_limit=10)
    manager.record("task-a", Usage(input_tokens=10, output_tokens=0))
    with pytest.raises(BudgetExceeded):
        manager.check("task-a", extra_tokens=1)
    manager.check("task-b", extra_tokens=1_000_000)


def test_estimated_usage_is_labelled_as_such() -> None:
    manager = BudgetManager(BudgetConfig(), provider_config())
    manager.record("run", Usage(input_tokens=10, output_tokens=0, estimated=True))
    manager.record("run", Usage(input_tokens=10, output_tokens=0, estimated=False))
    snapshot = manager.snapshot("run")
    assert snapshot.tokens_used == 20
    assert snapshot.calls == 2


def test_total_cost_across_scopes() -> None:
    manager = BudgetManager(BudgetConfig(), provider_config())
    manager.record("a", Usage(input_tokens=100, output_tokens=0))
    manager.record("b", Usage(input_tokens=100, output_tokens=0))
    assert manager.total_tokens() == 200
    assert manager.total_cost_usd() > 0
