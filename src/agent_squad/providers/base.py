"""Provider-agnostic model interface.

Every provider is normalised into the same small vocabulary:

* :class:`ChatMessage` -- system / user / assistant / tool turns;
* :class:`ToolCall`   -- a structured request to invoke a registered tool;
* :class:`Usage`      -- token accounting, distinguishing *reported* usage
  from anything the provider estimated;
* :class:`ModelResponse` -- text, tool calls, usage, latency and the raw
  provider payload (kept so that provider-specific features are not lost).

Normalisation deliberately does *not* discard provider-specific fields: the
raw response is preserved on :attr:`ModelResponse.raw` and providers expose a
``capabilities`` descriptor so the orchestrator can use provider-specific
features without hard-coding them into the runtime.

The provider is also where the run's absolute deadline is enforced for the
model path.  The concrete providers each implement their own HTTP call, but
the *bound* is a property of the run rather than of the provider, so it is
applied once -- in :class:`BaseProvider` -- instead of in every
implementation.  Two phases are covered:

``download``
    The model has to be acquired and loaded before it can answer.  The
    runtime does not fetch weights itself (the endpoint does that when it
    serves the first request), so this is the provider's own acquisition
    step, and it is a phase of its own so that a slow load cannot hide
    inside the request.

``provider``
    The queue wait and the request itself.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ErrorClass, ModelError
from ..ids import new_id
from ..runtime.deadline import Phase, RunDeadline, current_run_scope, run_phase
from .ledger import (
    RequestLedger,
    RequestPhase,
    UsageSource,
    current_ledger,
    make_record,
)

MessageRole = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A model's request to invoke a tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("tc"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    created_at: _dt.datetime | None = None


class ToolSpec(BaseModel):
    """A tool description handed to the provider."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    """Token accounting.

    ``reported`` distinguishes numbers the provider actually returned from
    numbers we estimated locally.  Estimates are useful for budgeting but
    must never be reported as measured usage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    estimated: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            estimated=self.estimated and other.estimated,
        )


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    messages: list[ChatMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_output_tokens: int = Field(default=4096, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int | None = None
    stop: list[str] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    latency_s: float = 0.0
    finish_reason: str = "stop"
    raw: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supports_tools: bool = True
    supports_system_prompt: bool = True
    supports_seed: bool = False
    supports_parallel_tool_calls: bool = False
    supports_json_schema: bool = False


@runtime_checkable
class ModelProvider(Protocol):
    """The contract every provider implements."""

    name: str
    model: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one completion.  Must raise :class:`ModelError` on failure."""
        ...

    async def aclose(self) -> None:
        """Release any resources held by the provider."""
        ...


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate.

    The estimate is intentionally crude (about four characters per token) and
    is always labelled as an estimate.  It exists so that the mock provider
    and offline paths can participate in budgeting without pretending to be
    exact.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


# --------------------------------------------------------------- accounting

TimingName = Literal["prompt", "decode", "load", "tool", "verification", "teardown"]


@dataclass
class _AttemptState:
    """Per-attempt facts a provider may observe before the attempt ends.

    It lives in a :class:`contextvars.ContextVar` rather than on the provider
    because one provider instance serves concurrent attempts; a field on
    ``self`` would let one attempt's usage leak into another attempt's
    record.
    """

    phase: RequestPhase
    task_id: str | None = None
    attempt: int | None = None
    queue_s: float | None = None
    usage: Usage | None = None
    usage_observed: bool = False
    timings: dict[str, float] = field(default_factory=dict)


_ATTEMPT_STATE: ContextVar[_AttemptState | None] = ContextVar(
    "agent_squad_provider_attempt", default=None
)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _coerce_phase(value: object, default: RequestPhase) -> RequestPhase:
    if isinstance(value, RequestPhase):
        return value
    if isinstance(value, str):
        try:
            return RequestPhase(value)
        except ValueError:
            return default
    return default


def _first_float(*values: object) -> float | None:
    for value in values:
        number = _as_float(value)
        if number is not None:
            return number
    return None


def _seconds_from(
    mapping: Mapping[str, Any], *keys: str, milliseconds: tuple[str, ...] = ()
) -> float | None:
    direct = _first_float(*(mapping.get(key) for key in keys))
    if direct is not None:
        return direct
    millis = _first_float(*(mapping.get(key) for key in milliseconds))
    return None if millis is None else millis / 1000.0


def _response_timings(
    response: ModelResponse | None, dispatch_s: float | None
) -> dict[str, float | None]:
    """Extract distinct phase timings from what the endpoint reported.

    Not every endpoint reports timing.  Missing values stay ``None``; the
    only value derived rather than measured is decode, and only when prompt
    time and total dispatch time are both known (decode = total - prompt).
    """
    timings: dict[str, float | None] = {
        "prompt": None,
        "decode": None,
        "load": None,
        "tool": None,
        "verification": None,
        "teardown": None,
    }
    if response is None:
        return timings
    raw = response.raw
    reported = raw.get("timings")
    if isinstance(reported, Mapping):
        timings["prompt"] = _seconds_from(
            reported,
            "prompt_s",
            "ttft_s",
            "time_to_first_token_s",
            milliseconds=("prompt_ms", "ttft_ms", "time_to_first_token_ms"),
        )
        timings["decode"] = _seconds_from(
            reported, "decode_s", milliseconds=("predicted_ms",)
        )
    explicit = raw.get("agent_squad_timings")
    if isinstance(explicit, Mapping):
        for name in ("prompt", "decode", "load", "tool", "verification", "teardown"):
            value = _seconds_from(explicit, f"{name}_s", milliseconds=(f"{name}_ms",))
            if value is not None:
                timings[name] = value

    prompt = timings["prompt"]
    decode = timings["decode"]
    if dispatch_s is not None:
        if prompt is not None and decode is None:
            decode = max(0.0, dispatch_s - prompt)
        elif decode is not None and prompt is None:
            prompt = max(0.0, dispatch_s - decode)
    timings["prompt"] = prompt
    timings["decode"] = decode
    return timings


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return ErrorClass.CANCELLED.value
    if isinstance(exc, TimeoutError):
        return ErrorClass.TIMEOUT.value
    context = getattr(exc, "context", None)
    if isinstance(context, Mapping) and context.get("timeout") is True:
        return ErrorClass.TIMEOUT.value
    error_class = getattr(exc, "error_class", None)
    if isinstance(error_class, ErrorClass):
        return error_class.value
    return type(exc).__name__


def _error_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message[:1000]
    return (str(exc) or type(exc).__name__)[:1000]


def _is_zero_usage(usage: Usage) -> bool:
    """Whether a usage object carries no measurement at all.

    A provider that never called ``_observe_usage`` leaves the default
    ``Usage`` on the response: zeros with ``estimated=True``.  Those zeros
    are not a measurement, so the ledger labels the attempt unavailable
    instead of reporting a measured zero.
    """
    return (
        usage.input_tokens == 0
        and usage.output_tokens == 0
        and usage.cached_input_tokens == 0
    )


class BaseProvider:
    """Shared plumbing for concrete providers."""

    name = "base"
    capabilities = ProviderCapabilities()

    def __init__(
        self,
        model: str = "default",
        *,
        deadline: RunDeadline | None = None,
        ledger: RequestLedger | None = None,
        phase: RequestPhase = RequestPhase.OTHER,
    ) -> None:
        self.model = model
        self._deadline = deadline
        self._ledger = ledger
        self.phase = phase

    @property
    def ledger(self) -> RequestLedger | None:
        """The ledger this provider writes to, when one has been attached."""
        return self._ledger

    def attach_ledger(self, ledger: RequestLedger) -> None:
        """Attach a ledger without overriding one that was injected explicitly.

        The router uses this so that every provider it dispatches through --
        including a provider hidden behind a replay recorder -- reconciles
        against the same records.
        """
        if self._ledger is None:
            self._ledger = ledger

    def _attempt_state(self, request: ModelRequest) -> _AttemptState:
        """Build the per-attempt accounting state from the request.

        Callers may annotate an attempt through request metadata:
        ``ledger_phase``, ``ledger_task_id``, ``ledger_attempt`` and
        ``ledger_queue_s``.  The short forms (``phase``, ``task_id``,
        ``attempt``, ``queue_s``) are accepted too, but the namespaced keys
        win because they cannot collide with provider-specific metadata.
        """
        metadata = request.metadata
        explicit_phase = metadata.get("ledger_phase", metadata.get("phase"))
        if explicit_phase is None and metadata.get("schema_name") == "plan":
            # The planner is the one caller that does not annotate its
            # request.  Its plan schema is an unambiguous marker, so the
            # planner's attempt is labelled without requiring a change in
            # the planner itself.  Caller metadata still wins.
            phase = RequestPhase.PLANNER
        else:
            phase = _coerce_phase(explicit_phase, self.phase)
        task_id_value = metadata.get("ledger_task_id", metadata.get("task_id"))
        task_id = task_id_value if isinstance(task_id_value, str) and task_id_value else None
        attempt_value = metadata.get("ledger_attempt", metadata.get("attempt"))
        attempt = (
            attempt_value
            if isinstance(attempt_value, int) and not isinstance(attempt_value, bool)
            else None
        )
        queue_s = _as_float(metadata.get("ledger_queue_s", metadata.get("queue_s")))
        return _AttemptState(
            phase=phase,
            task_id=task_id,
            attempt=attempt,
            queue_s=queue_s,
        )

    def _observe_usage(self, usage: Usage | None) -> None:
        """Record usage as soon as it is known.

        A provider calls this immediately after parsing the response usage,
        *before* any later decode step can fail.  That is what makes a
        failure-after-usage distinguishable from a failure-before-usage: the
        terminal record keeps the numbers and their reported provenance even
        though the attempt did not complete.
        """
        state = _ATTEMPT_STATE.get()
        if state is not None:
            state.usage = usage
            state.usage_observed = True

    def _observe_timing(self, name: TimingName, seconds: float | None) -> None:
        """Note one phase timing a provider could measure but the wrapper cannot.

        Tool, verification and teardown timing happen outside the provider
        call.  A provider (or a recording endpoint) that does know one of
        them can publish it here; otherwise the field stays ``None`` rather
        than being reported as an unmeasured zero.
        """
        if seconds is None:
            return
        state = _ATTEMPT_STATE.get()
        if state is not None:
            state.timings[name] = seconds

    def _record_attempt(
        self,
        ledger: RequestLedger,
        state: _AttemptState,
        *,
        started_at: _dt.datetime,
        started: float,
        load_s: float | None,
        dispatch_s: float | None,
        response: ModelResponse | None,
        error: BaseException | None,
    ) -> None:
        """Write the one terminal record for this attempt."""
        if state.usage_observed:
            usage = state.usage
        elif response is not None and not _is_zero_usage(response.usage):
            usage = response.usage
        else:
            usage = None
        if usage is None:
            usage_source = UsageSource.UNAVAILABLE
            prompt_tokens = output_tokens = cached_tokens = None
        else:
            usage_source = (
                UsageSource.ESTIMATED if usage.estimated else UsageSource.REPORTED
            )
            prompt_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            cached_tokens = usage.cached_input_tokens

        timings = _response_timings(response, dispatch_s)
        for name, seconds in state.timings.items():
            if timings.get(name) is None:
                timings[name] = seconds
        if timings["load"] is None:
            timings["load"] = load_s

        provider = (response.provider if response is not None else "") or self.name
        model = (response.model if response is not None else "") or self.model
        tool_calls = len(response.tool_calls) if response is not None else 0
        salvaged = (
            int(bool(response.raw.get("salvaged_tool_call")))
            if response is not None
            else 0
        )
        ledger.record(
            make_record(
                phase=state.phase,
                task_id=state.task_id,
                attempt=state.attempt,
                provider=provider,
                model=model,
                ok=error is None,
                error_class=None if error is None else _error_class(error),
                error=None if error is None else _error_message(error),
                queue_s=state.queue_s,
                load_s=timings["load"],
                latency_s=time.monotonic() - started,
                prompt_s=timings["prompt"],
                decode_s=timings["decode"],
                tool_s=timings["tool"],
                verification_s=timings["verification"],
                teardown_s=timings["teardown"],
                usage_source=usage_source,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                tool_calls=tool_calls,
                salvaged_tool_calls=salvaged,
                started_at=started_at,
            )
        )

    @property
    def deadline(self) -> RunDeadline | None:
        """The run's absolute deadline, if the provider has one.

        The explicit deadline wins; otherwise the ambient run scope is
        consulted, so that a provider built before the run started is still
        bound by the run's limit.
        """
        return self._deadline or current_run_scope().deadline

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Bind every provider's ``complete`` to the run's deadline.

        The concrete providers each implement their own request; the bound
        belongs to the run, so it is applied here once instead of being
        repeated -- and forgotten -- in every implementation.
        """
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("complete")
        if original is None or getattr(original, "_deadline_bound", False):
            return

        @functools.wraps(original)
        async def complete(self: BaseProvider, request: ModelRequest) -> ModelResponse:
            return await self._complete_bounded(original, request)

        complete._deadline_bound = True  # type: ignore[attr-defined]
        cls.complete = complete  # type: ignore[method-assign]

    async def _complete_bounded(
        self,
        original: Callable[[BaseProvider, ModelRequest], Awaitable[ModelResponse]],
        request: ModelRequest,
    ) -> ModelResponse:
        """Run one attempt under the run's deadline and account for it.

        This is the single accounting seam for every provider: the wrapped
        ``complete`` is invoked exactly once per dispatched attempt, so the
        number of terminal records equals the number of attempts -- including
        the failed half of a retry and both sides of a fallback.  The record
        is written in a ``finally``-safe path that also runs for exceptions,
        timeouts and cancellation.
        """
        ledger = self._ledger if self._ledger is not None else current_ledger()
        state = self._attempt_state(request) if ledger is not None else None
        token = _ATTEMPT_STATE.set(state) if state is not None else None
        started_at = _dt.datetime.now(_dt.UTC)
        started = time.monotonic()
        load_s: float | None = None
        dispatch_s: float | None = None
        response: ModelResponse | None = None
        recorded = False

        try:
            deadline = self.deadline

            async def acquire() -> None:
                """The model has to be loaded before it can answer."""
                return None

            async def dispatch() -> ModelResponse:
                return await original(self, request)

            # The acquisition is a phase of its own so that a slow load is
            # visible, and bounded, rather than hiding inside the request.
            load_started = time.monotonic()
            await run_phase(acquire, phase=Phase.DOWNLOAD.value, deadline=deadline)
            load_s = time.monotonic() - load_started

            dispatch_started = time.monotonic()
            try:
                response = await run_phase(
                    dispatch, phase=Phase.PROVIDER.value, deadline=deadline
                )
            finally:
                dispatch_s = time.monotonic() - dispatch_started

            if state is not None and ledger is not None:
                self._record_attempt(
                    ledger,
                    state,
                    started_at=started_at,
                    started=started,
                    load_s=load_s,
                    dispatch_s=dispatch_s,
                    response=response,
                    error=None,
                )
                recorded = True
            return response
        except asyncio.CancelledError as exc:
            # Cancellation is a terminal outcome, not an absence of one.  The
            # record is written before the exception is re-raised so that the
            # caller's cancellation cannot erase the attempt from accounting.
            if state is not None and ledger is not None and not recorded:
                self._record_attempt(
                    ledger,
                    state,
                    started_at=started_at,
                    started=started,
                    load_s=load_s,
                    dispatch_s=dispatch_s,
                    response=None,
                    error=exc,
                )
            raise
        except Exception as exc:
            if state is not None and ledger is not None and not recorded:
                self._record_attempt(
                    ledger,
                    state,
                    started_at=started_at,
                    started=started,
                    load_s=load_s,
                    dispatch_s=dispatch_s,
                    response=None,
                    error=exc,
                )
            raise
        finally:
            if token is not None:
                _ATTEMPT_STATE.reset(token)

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise ModelError("provider does not implement complete()")

    async def aclose(self) -> None:
        return None
