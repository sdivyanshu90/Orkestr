"""Unit tests for per-attempt provider accounting.

The ledger is only useful if it is wired to the request path, so these tests
exercise real ``complete()`` calls rather than calling the ledger directly.
Each test pins one property that R4 requires: one terminal record per
attempt, explicit usage provenance, both failure-injection positions,
distinct timings, fallback identity, and cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from agent_squad.config import ProviderConfig
from agent_squad.errors import ModelError, TransientModelError
from agent_squad.providers.base import (
    BaseProvider,
    ChatMessage,
    ModelRequest,
    ModelResponse,
    Usage,
)
from agent_squad.providers.ledger import RequestLedger, RequestPhase, UsageSource
from agent_squad.providers.mock import MockProvider
from agent_squad.providers.registry import ProviderRouter


def make_request(
    *,
    phase: str | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
) -> ModelRequest:
    metadata: dict[str, object] = {}
    if phase is not None:
        metadata["ledger_phase"] = phase
    if task_id is not None:
        metadata["ledger_task_id"] = task_id
    if attempt is not None:
        metadata["ledger_attempt"] = attempt
    return ModelRequest(
        messages=[ChatMessage(role="user", content="do the work")],
        metadata=metadata,
    )


class FailsAfterUsage(BaseProvider):
    """Fails only after the endpoint's usage has been parsed."""

    name = "after_usage"
    model = "after-usage-model"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._observe_usage(
            Usage(input_tokens=11, output_tokens=7, cached_input_tokens=3, estimated=False)
        )
        raise ModelError("decoded the response, then failed", provider=self.name)


class TimesOut(BaseProvider):
    name = "timeout"
    model = "timeout-model"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise TimeoutError("the endpoint stopped responding")


class TimesOutTransient(BaseProvider):
    """The shape an HTTP client timeout takes after provider normalisation."""

    name = "http_timeout"
    model = "http-timeout-model"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise TransientModelError("model request timed out", provider=self.name, timeout=True)


class Hangs(BaseProvider):
    """Blocks until cancelled, optionally after observing usage."""

    name = "hanging"
    model = "hanging-model"

    def __init__(self, *, observe_usage: bool = False, **kwargs: object) -> None:
        super().__init__(model="hanging-model", **kwargs)  # type: ignore[arg-type]
        self.started = asyncio.Event()
        self._observe = observe_usage

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._observe:
            self._observe_usage(Usage(input_tokens=5, output_tokens=2, estimated=False))
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ReportsNoUsage(BaseProvider):
    """A provider that returns a response without any usage measurement."""

    name = "no_usage"
    model = "no-usage-model"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="ok", provider=self.name, model=self.model)


class ReportsTimings(BaseProvider):
    name = "timings"
    model = "timings-model"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._observe_timing("tool", 0.2)
        return ModelResponse(
            text="ok",
            usage=Usage(input_tokens=2, output_tokens=3, estimated=False),
            provider=self.name,
            model=self.model,
            raw={
                "timings": {"prompt_ms": 100.0, "predicted_ms": 50.0},
                "agent_squad_timings": {"verification_s": 0.3, "teardown_s": 0.4},
            },
        )


# ---------------------------------------------------------- one record per attempt


async def test_every_dispatched_attempt_gets_exactly_one_terminal_record() -> None:
    ledger = RequestLedger()
    provider = MockProvider(ledger=ledger, failures=2)

    for _ in range(5):
        with contextlib.suppress(ModelError):
            await provider.complete(make_request())

    counts = ledger.counts()
    assert counts["requests"] == 5
    assert counts["terminal"] == 5
    assert len(ledger.records) == 5
    assert [record.index for record in ledger.records] == [0, 1, 2, 3, 4]
    assert counts["ok"] == 3
    assert counts["failed"] == 2


async def test_a_retry_produces_two_records_never_a_merged_one() -> None:
    ledger = RequestLedger()
    provider = MockProvider(ledger=ledger, failures=1)

    with pytest.raises(TransientModelError):
        await provider.complete(make_request(phase="worker"))
    await provider.complete(make_request(phase="retry", attempt=2))

    records = ledger.records
    assert len(records) == 2
    assert records[0].ok is False
    assert records[0].phase is RequestPhase.WORKER
    assert records[1].ok is True
    assert records[1].phase is RequestPhase.RETRY
    assert records[1].attempt == 2
    assert ledger.counts()["phases"]["retry"] == 1


# ------------------------------------------------------------- usage provenance


async def test_a_failure_before_usage_has_no_usage_and_is_unavailable() -> None:
    ledger = RequestLedger()
    provider = MockProvider(ledger=ledger, failures=1)

    with pytest.raises(TransientModelError):
        await provider.complete(make_request())

    (record,) = ledger.records
    assert record.ok is False
    assert record.usage_source is UsageSource.UNAVAILABLE
    assert record.prompt_tokens is None
    assert record.output_tokens is None
    assert record.cached_tokens is None


async def test_a_failure_after_usage_keeps_reported_provenance() -> None:
    """Injection (b): usage arrived, then the attempt failed."""
    ledger = RequestLedger()
    provider = FailsAfterUsage(ledger=ledger)

    with pytest.raises(ModelError):
        await provider.complete(make_request(phase="worker"))

    (record,) = ledger.records
    assert record.ok is False
    assert record.error_class == "model"
    assert record.usage_source is UsageSource.REPORTED
    assert record.prompt_tokens == 11
    assert record.output_tokens == 7
    assert record.cached_tokens == 3


async def test_unreported_usage_is_null_and_unavailable_not_a_measured_zero() -> None:
    ledger = RequestLedger()
    provider = ReportsNoUsage(ledger=ledger)

    await provider.complete(make_request())

    (record,) = ledger.records
    assert record.ok is True
    assert record.usage_source is UsageSource.UNAVAILABLE
    assert record.prompt_tokens is None
    assert record.output_tokens is None


# ------------------------------------------------------------------- timeouts


async def test_a_timeout_is_recorded_as_a_terminal_timeout() -> None:
    ledger = RequestLedger()
    provider = TimesOut(ledger=ledger)

    with pytest.raises(TimeoutError):
        await provider.complete(make_request())

    (record,) = ledger.records
    assert record.ok is False
    assert record.error_class == "timeout"
    assert record.usage_source is UsageSource.UNAVAILABLE


async def test_a_provider_normalised_http_timeout_is_also_terminal() -> None:
    ledger = RequestLedger()
    provider = TimesOutTransient(ledger=ledger)

    with pytest.raises(TransientModelError):
        await provider.complete(make_request())

    (record,) = ledger.records
    assert record.ok is False
    assert record.error_class == "timeout"
    assert record.usage_source is UsageSource.UNAVAILABLE


# ---------------------------------------------------------------- cancellation


async def test_a_cancelled_attempt_is_recorded_and_the_cancellation_is_reraised() -> None:
    ledger = RequestLedger()
    provider = Hangs(ledger=ledger)

    task = asyncio.create_task(provider.complete(make_request()))
    await provider.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    (record,) = ledger.records
    assert record.ok is False
    assert record.cancelled is True
    assert record.error_class == "cancelled"
    assert record.usage_source is UsageSource.UNAVAILABLE
    assert ledger.counts()["cancelled"] == 1


async def test_cancellation_after_usage_keeps_the_reported_numbers() -> None:
    ledger = RequestLedger()
    provider = Hangs(ledger=ledger, observe_usage=True)

    task = asyncio.create_task(provider.complete(make_request()))
    await provider.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    (record,) = ledger.records
    assert record.cancelled is True
    assert record.usage_source is UsageSource.REPORTED
    assert record.prompt_tokens == 5
    assert record.output_tokens == 2


# -------------------------------------------------------------------- timings


async def test_queue_and_load_are_measured_while_unobserved_phases_are_none() -> None:
    ledger = RequestLedger()
    provider = MockProvider(ledger=ledger)

    await provider.complete(make_request())

    (record,) = ledger.records
    # A provider called directly has no concurrency slot to wait for.
    assert record.queue_s is None
    # The load phase did run, so its duration is a real measurement.
    assert record.load_s is not None
    assert record.load_s >= 0.0
    # A non-streaming endpoint reports no first-byte or decode timing.
    assert record.prompt_s is None
    assert record.decode_s is None
    assert record.tool_s is None
    assert record.verification_s is None
    assert record.teardown_s is None
    assert record.latency_s >= 0.0


async def test_the_router_measures_the_concurrency_slot_wait() -> None:
    ledger = RequestLedger()
    slow = MockProvider(latency_s=0.05)
    router = ProviderRouter(
        [slow, MockProvider()],
        [
            ProviderConfig(name="mock", model="m", max_concurrency=1),
            ProviderConfig(name="mock", model="m", max_concurrency=1),
        ],
        ledger=ledger,
    )

    # Start the second request only once the first holds the only slot, so
    # the queue measurement is deterministic rather than dependent on how
    # the event loop happens to schedule two coroutines.
    first = asyncio.create_task(router.complete(make_request(phase="worker")))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(router.complete(make_request(phase="worker")))
    await asyncio.gather(first, second)

    queues = [record.queue_s for record in ledger.records]
    assert len(queues) == 2
    assert all(queue is not None for queue in queues)
    assert max(queue for queue in queues if queue is not None) >= 0.03


async def test_phase_timings_are_recorded_distinctly_when_observable() -> None:
    ledger = RequestLedger()
    provider = ReportsTimings(ledger=ledger)

    await provider.complete(make_request())

    (record,) = ledger.records
    assert record.prompt_s == pytest.approx(0.1)
    assert record.decode_s == pytest.approx(0.05)
    assert record.tool_s == pytest.approx(0.2)
    assert record.verification_s == pytest.approx(0.3)
    assert record.teardown_s == pytest.approx(0.4)
    assert record.queue_s is None
    assert record.load_s is not None


# ------------------------------------------------------------------- fallback


async def test_a_fallback_attempt_is_its_own_record_with_fallback_phase() -> None:
    ledger = RequestLedger()
    primary = MockProvider(failures=1)
    primary.name = "primary"
    fallback = MockProvider()
    fallback.name = "fallback"
    router = ProviderRouter(
        [primary, fallback],
        [
            ProviderConfig(name="mock", model="primary"),
            ProviderConfig(name="mock", model="fallback"),
        ],
        ledger=ledger,
    )

    await router.complete(make_request())

    records = ledger.records
    assert len(records) == 2
    assert records[0].ok is False
    assert records[0].provider == "primary"
    assert records[1].ok is True
    assert records[1].provider == "fallback"
    assert records[1].phase is RequestPhase.FALLBACK
    assert ledger.counts()["phases"]["fallback"] == 1


async def test_the_router_adopts_a_ledger_already_attached_to_a_provider() -> None:
    """The composition root's ledger must remain the one the router reports."""
    shared = RequestLedger()
    primary = MockProvider(ledger=shared)
    router = ProviderRouter(
        [primary, MockProvider()],
        [
            ProviderConfig(name="mock", model="primary"),
            ProviderConfig(name="mock", model="fallback"),
        ],
    )

    assert router.ledger is shared
    await router.complete(make_request(phase="worker"))

    assert len(shared) == 1
    assert router.ledger.records == shared.records


# ---------------------------------------------------------------- reconciliation


async def test_a_plan_schema_request_is_labelled_planner_without_caller_tagging() -> None:
    ledger = RequestLedger()
    provider = MockProvider(ledger=ledger)
    request = ModelRequest(
        messages=[ChatMessage(role="user", content="plan this")],
        response_schema={"type": "object", "properties": {}},
        metadata={"schema_name": "plan"},
    )

    await provider.complete(request)

    (record,) = ledger.records
    assert record.phase is RequestPhase.PLANNER
    assert ledger.counts()["phases"]["planner"] == 1


async def test_phase_counts_reconcile_exactly_with_what_the_caller_made() -> None:
    ledger = RequestLedger()
    provider = MockProvider(ledger=ledger)

    for phase in ("planner", "worker", "worker", "reviewer", "retry"):
        await provider.complete(make_request(phase=phase))

    expected = {
        "planner": 1,
        "worker": 2,
        "reviewer": 1,
        "retry": 1,
        "fallback": 0,
        "other": 0,
    }
    counts = ledger.counts()
    assert counts["requests"] == 5
    assert counts["phases"] == expected
    assert ledger.summary()["phases"] == expected
    assert ledger.totals()["requests"] == 5
    assert len(ledger.records) == counts["requests"]
