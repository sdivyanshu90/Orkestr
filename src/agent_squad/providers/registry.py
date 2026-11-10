"""Provider construction and pricing.

Pricing is *configuration*, never a hardcoded constant: the orchestrator
reads per-token prices from :class:`~agent_squad.config.ProviderConfig` so
that a run can be costed against whatever the operator's contract says.  The
same table is used for budgeting (pre-flight) and for accounting (after the
fact), and the accounting path always records whether the number was
*reported* by the provider or *estimated* locally.
"""

from __future__ import annotations

import asyncio

from ..config import ProviderConfig, RunConfig
from ..errors import ValidationError
from .anthropic import AnthropicProvider
from .base import BaseProvider, ModelProvider, Usage
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider


def build_provider(config: ProviderConfig) -> ModelProvider:
    """Instantiate a provider from its configuration."""
    name = config.name.lower()
    if name == "mock":
        return MockProvider(model=config.model)
    if name in ("openai_compat", "openai", "local", "vllm", "ollama", "llamacpp"):
        return OpenAICompatProvider(
            model=config.model,
            base_url=config.base_url or "https://api.openai.com/v1",
            api_key=config.api_key(),
            timeout_s=config.timeout_s,
            max_concurrency=config.max_concurrency,
        )
    if name == "anthropic":
        return AnthropicProvider(
            model=config.model,
            base_url=config.base_url or "https://api.anthropic.com",
            api_key=config.api_key(),
            timeout_s=config.timeout_s,
            max_concurrency=config.max_concurrency,
        )
    raise ValidationError(f"unknown provider: {config.name!r}", provider=config.name)


def build_providers(run_config: RunConfig) -> list[ModelProvider]:
    """Primary provider first, then fallbacks in declaration order."""
    providers = [build_provider(run_config.provider)]
    for fallback in run_config.fallback_providers:
        providers.append(build_provider(fallback))
    return providers


def compute_cost(usage: Usage, config: ProviderConfig) -> float:
    """Cost in the configured currency for one model call."""
    input_tokens = max(0, usage.input_tokens - usage.cached_input_tokens)
    cost = (
        input_tokens / 1000.0 * config.price_input_per_1k
        + usage.output_tokens / 1000.0 * config.price_output_per_1k
    )
    return round(cost, 10)


class ProviderRouter:
    """Routes model calls to the primary provider with fallbacks.

    Fallbacks are only used for *transient* failures.  A permanent error
    (bad request, authentication failure) is surfaced immediately, because
    silently switching providers would hide a misconfiguration.


    The router also enforces each provider's declared concurrency limit with
    a semaphore, so a run with many workers cannot exceed the rate the
    endpoint is willing to serve.  The limit is per provider, and the
    fallback providers have their own.
    """

    def __init__(self, providers: list[ModelProvider], configs: list[ProviderConfig]) -> None:
        if len(providers) != len(configs):
            raise ValidationError("providers and configs must be the same length")
        self._providers = providers
        self._configs = configs
        self._semaphores = [
            asyncio.Semaphore(max(1, config.max_concurrency)) for config in configs
        ]

    @property
    def primary(self) -> ModelProvider:
        return self._providers[0]

    async def complete(self, request: object) -> object:
        from ..errors import TransientModelError

        last: Exception | None = None
        for index, provider in enumerate(self._providers):
            try:
                async with self._semaphores[index]:
                    return await provider.complete(request)  # type: ignore[arg-type]
            except TransientModelError as exc:
                last = exc
                continue
        assert last is not None
        raise last

    async def aclose(self) -> None:
        for provider in self._providers:
            await provider.aclose()


__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "MockProvider",
    "ModelProvider",
    "OpenAICompatProvider",
    "ProviderRouter",
    "Usage",
    "build_provider",
    "build_providers",
    "compute_cost",
]
