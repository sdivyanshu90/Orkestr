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
import time

from ..config import ProviderConfig, RunConfig
from ..errors import ValidationError
from .anthropic import AnthropicProvider
from .base import BaseProvider, ModelProvider, ModelRequest, Usage
from .ledger import RequestLedger, RequestPhase, bind_ledger
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider


def build_provider(
    config: ProviderConfig,
    *,
    ledger: RequestLedger | None = None,
    phase: RequestPhase = RequestPhase.OTHER,
) -> ModelProvider:
    """Instantiate a provider from its configuration.

    A ledger may be injected here so that every provider built for a run
    writes into the same accounting object.  Without one the provider still
    works, it simply records nothing until a router or caller attaches a
    ledger.
    """
    name = config.name.lower()
    if name == "mock":
        return MockProvider(model=config.model, ledger=ledger, phase=phase)
    if name in ("openai_compat", "openai", "local", "vllm", "ollama", "llamacpp"):
        return OpenAICompatProvider(
            model=config.model,
            base_url=config.base_url or "https://api.openai.com/v1",
            api_key=config.api_key(),
            timeout_s=config.timeout_s,
            max_concurrency=config.max_concurrency,
            ledger=ledger,
            phase=phase,
        )
    if name == "anthropic":
        return AnthropicProvider(
            model=config.model,
            base_url=config.base_url or "https://api.anthropic.com",
            api_key=config.api_key(),
            timeout_s=config.timeout_s,
            max_concurrency=config.max_concurrency,
            ledger=ledger,
            phase=phase,
        )
    raise ValidationError(f"unknown provider: {config.name!r}", provider=config.name)


def build_providers(
    run_config: RunConfig, *, ledger: RequestLedger | None = None
) -> list[ModelProvider]:
    """Primary provider first, then fallbacks in declaration order.

    Fallback providers are constructed with ``phase=FALLBACK`` so that a
    provider used outside the router still labels its attempts correctly.
    """
    providers = [build_provider(run_config.provider, ledger=ledger)]
    for fallback in run_config.fallback_providers:
        providers.append(
            build_provider(fallback, ledger=ledger, phase=RequestPhase.FALLBACK)
        )
    return providers


def compute_cost(usage: Usage, config: ProviderConfig) -> float:
    """Cost in the configured currency for one model call."""
    input_tokens = max(0, usage.input_tokens - usage.cached_input_tokens)
    cost = (
        input_tokens / 1000.0 * config.price_input_per_1k
        + usage.output_tokens / 1000.0 * config.price_output_per_1k
    )
    return round(cost, 10)


def _attach_ledger(provider: object, ledger: RequestLedger, seen: set[int] | None = None) -> None:
    """Attach ``ledger`` to a provider and to any recorder wrapping it.

    The orchestrator wraps the primary provider in a replay recorder; walking
    ``inner`` is what makes the provider behind that wrapper participate in
    the same accounting.  Cycles are guarded so a badly written wrapper
    cannot recurse forever.
    """
    if seen is None:
        seen = set()
    if id(provider) in seen:
        return
    seen.add(id(provider))
    attach = getattr(provider, "attach_ledger", None)
    if callable(attach):
        attach(ledger)
    inner = getattr(provider, "inner", None)
    if inner is not None and inner is not provider:
        _attach_ledger(inner, ledger, seen)


def _existing_ledger(provider: object, seen: set[int] | None = None) -> RequestLedger | None:
    """Find the ledger already attached to a provider or its wrapper."""
    if seen is None:
        seen = set()
    if id(provider) in seen:
        return None
    seen.add(id(provider))
    ledger = getattr(provider, "ledger", None)
    if isinstance(ledger, RequestLedger):
        return ledger
    inner = getattr(provider, "inner", None)
    if inner is not None and inner is not provider:
        return _existing_ledger(inner, seen)
    return None


class ProviderRouter:
    """Routes model calls to the primary provider with fallbacks.

    Fallbacks are only used for *transient* failures.  A permanent error
    (bad request, authentication failure) is surfaced immediately, because
    silently switching providers would hide a misconfiguration.


    The router also enforces each provider's declared concurrency limit with
    a semaphore, so a run with many workers cannot exceed the rate the
    endpoint is willing to serve.  The limit is per provider, and the
    fallback providers have their own.

    Every dispatch through the router is accounted for in :attr:`ledger`.
    The queue wait is measured around the semaphore (the provider itself
    cannot see it), and a fallback attempt is tagged ``FALLBACK`` so that it
    is a distinct record rather than being merged with the failed primary
    attempt.
    """

    def __init__(
        self,
        providers: list[ModelProvider],
        configs: list[ProviderConfig],
        *,
        ledger: RequestLedger | None = None,
    ) -> None:
        if len(providers) != len(configs):
            raise ValidationError("providers and configs must be the same length")
        self._providers = providers
        self._configs = configs
        self._semaphores = [
            asyncio.Semaphore(max(1, config.max_concurrency)) for config in configs
        ]
        if ledger is None:
            # The composition root may already have built every provider with
            # one shared ledger.  Adopting it keeps ``router.ledger`` the same
            # object the request path writes to instead of creating a second,
            # empty ledger that would silently fail to reconcile.
            for provider in providers:
                ledger = _existing_ledger(provider)
                if ledger is not None:
                    break
        self.ledger = ledger if ledger is not None else RequestLedger()
        for provider in providers:
            _attach_ledger(provider, self.ledger)

    @property
    def primary(self) -> ModelProvider:
        return self._providers[0]

    def _attempt_request(self, request: object, index: int, queue_s: float) -> object:
        """Tag a dispatch with the queue wait and, for fallbacks, the phase.

        The caller's request is copied rather than mutated: the same request
        may be replayed or retried, and an accounting hint from one attempt
        must not leak into the next.
        """
        if not isinstance(request, ModelRequest):
            return request
        metadata = dict(request.metadata)
        metadata["ledger_queue_s"] = queue_s
        if index > 0:
            metadata["ledger_phase"] = RequestPhase.FALLBACK.value
        return request.model_copy(update={"metadata": metadata})

    async def complete(self, request: object) -> object:
        from ..errors import TransientModelError

        last: Exception | None = None
        for index, provider in enumerate(self._providers):
            queued = time.monotonic()
            try:
                async with self._semaphores[index]:
                    queue_s = time.monotonic() - queued
                    attempt = self._attempt_request(request, index, queue_s)
                    with bind_ledger(self.ledger):
                        return await provider.complete(attempt)  # type: ignore[arg-type]
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
