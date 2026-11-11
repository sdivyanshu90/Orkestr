"""Request accounting.

Every model request is recorded: the planner's, the workers', the
retries', the fallbacks' and the failures'.  A request that failed still
consumed something, and a run that cannot account for its own requests
cannot report its own cost.

Three rules the ledger enforces:

* **Failures are recorded.**  A failed request is a record like any other,
  with the error class that caused it.
* **Missing values are null, not zero.**  If the provider did not report
  usage, the field is ``None``.  A zero would claim a measurement that
  was never taken.
* **Usage provenance is explicit.**  ``reported`` means the provider said
  so; ``estimated`` means the runtime inferred it; ``unavailable`` means
  nobody knows.

Timing is split into the phases an operator actually needs to distinguish:
the concurrency-slot wait (``queue_s``), model acquisition (``load_s``),
the whole attempt (``latency_s``), prompt processing (``prompt_s``), decode
(``decode_s``) and, where a caller can observe them, tool execution,
verification and teardown (``tool_s``, ``verification_s``, ``teardown_s``).
A phase that was not observed is ``None``: providers must not report a
zero for a clock nobody read.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ErrorClass
from ..models.task import utcnow


class UsageSource(str, Enum):
    REPORTED = "reported"
    """The provider reported the usage."""

    ESTIMATED = "estimated"
    """The runtime inferred it from the text."""

    UNAVAILABLE = "unavailable"
    """Nobody knows; the request failed before any usage existed."""


class RequestPhase(str, Enum):
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"
    RETRY = "retry"
    FALLBACK = "fallback"
    OTHER = "other"


class RequestRecord(BaseModel):
    """One terminal model attempt.

    Exactly one of these exists per dispatched attempt, whether the attempt
    succeeded, failed, timed out or was cancelled.  A retried request
    produces one record for the failed attempt and another for the retry;
    they are deliberately never merged, because the first attempt's cost and
    error are facts that the second attempt cannot replace.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    phase: RequestPhase = RequestPhase.OTHER
    task_id: str | None = None
    attempt: int | None = None
    provider: str = ""
    model: str = ""
    ok: bool = True
    error_class: str | None = None
    error: str | None = None

    queue_s: float | None = None
    """Time spent waiting for a provider slot before the attempt started.

    ``None`` when the provider had no slot to wait for (it was called
    directly rather than through the router), never ``0.0``.
    """

    load_s: float | None = None
    """Model acquisition time, when the provider has such a phase."""

    latency_s: float = 0.0
    """Total wall-clock time of the attempt, including load but not queue."""

    prompt_s: float | None = None
    """Prompt-processing time (request to first token), when observed."""

    decode_s: float | None = None
    """Decode time, when observed.  Derived as latency minus prompt otherwise."""

    tool_s: float | None = None
    """Tool execution time, when the caller observed it."""

    verification_s: float | None = None
    """Verification time, when the caller observed it."""

    teardown_s: float | None = None
    """Teardown time, when the caller observed it."""

    prompt_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    usage_source: UsageSource = UsageSource.UNAVAILABLE
    """Where the token numbers came from.

    ``UNAVAILABLE`` is paired with ``None`` token fields.  A failed request
    that never produced a response therefore has no usage at all.
    """

    tool_calls: int = 0
    salvaged_tool_calls: int = 0
    started_at: _dt.datetime = Field(default_factory=utcnow)

    @property
    def cancelled(self) -> bool:
        """Whether the attempt ended because it was cancelled."""
        return self.error_class == ErrorClass.CANCELLED.value

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RequestLedger:
    """The durable record of every model request a run made."""

    def __init__(self) -> None:
        self._records: list[RequestRecord] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- writing

    def record(self, record: RequestRecord) -> RequestRecord:
        """Append one terminal record, assigning its stable index."""
        with self._lock:
            record.index = len(self._records)
            self._records.append(record)
        return record

    def next_index(self) -> int:
        with self._lock:
            return len(self._records)

    # ------------------------------------------------------------- reading

    @property
    def records(self) -> list[RequestRecord]:
        with self._lock:
            return list(self._records)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def counts(self) -> dict[str, Any]:
        """Exact attempt counts, suitable for reconciliation.

        The counts are derived from the same snapshot as :meth:`records`, so
        ``counts()["requests"] == len(records)`` always holds.  Phase counts
        cover every phase, including the ones with no attempts, so a caller
        can compare the whole distribution rather than only the non-zero
        buckets.
        """
        records = self.records
        return {
            "requests": len(records),
            "terminal": len(records),
            "ok": sum(1 for record in records if record.ok),
            "failed": sum(1 for record in records if not record.ok),
            "cancelled": sum(1 for record in records if record.cancelled),
            "phases": {
                phase.value: sum(1 for record in records if record.phase is phase)
                for phase in RequestPhase
            },
            "usage_sources": {
                source.value: sum(
                    1 for record in records if record.usage_source is source
                )
                for source in UsageSource
            },
            "providers": {
                name: sum(1 for record in records if record.provider == name)
                for name in sorted({record.provider for record in records})
            },
        }

    # --------------------------------------------------------------- totals

    def totals(self) -> dict[str, Any]:
        """The aggregate accounting for the run."""
        records = self.records
        reported = [r for r in records if r.usage_source is UsageSource.REPORTED]
        estimated = [r for r in records if r.usage_source is UsageSource.ESTIMATED]
        unavailable = [r for r in records if r.usage_source is UsageSource.UNAVAILABLE]

        def total(records_: list[RequestRecord], field: str) -> int | None:
            values = [getattr(r, field) for r in records_]
            if not values or any(v is None for v in values):
                return None
            return sum(int(v) for v in values)

        def optional_total(field: str) -> float | None:
            values = [getattr(r, field) for r in records]
            if not values or any(v is None for v in values):
                return None
            return round(sum(float(v) for v in values), 6)

        return {
            "requests": len(records),
            "failed_requests": sum(1 for r in records if not r.ok),
            "cancelled_requests": sum(1 for r in records if r.cancelled),
            "reported_requests": len(reported),
            "estimated_requests": len(estimated),
            "unavailable_requests": len(unavailable),
            "prompt_tokens": total(reported, "prompt_tokens"),
            "output_tokens": total(reported, "output_tokens"),
            "cached_tokens": total(reported, "cached_tokens"),
            "total_latency_s": round(sum(r.latency_s for r in records), 3),
            "total_queue_s": optional_total("queue_s"),
            "total_load_s": optional_total("load_s"),
            "total_tool_calls": sum(r.tool_calls for r in records),
            "total_salvaged_tool_calls": sum(r.salvaged_tool_calls for r in records),
            "phases": {
                phase.value: sum(1 for r in records if r.phase is phase)
                for phase in RequestPhase
            },
        }

    def summary(self) -> dict[str, Any]:
        """A stable query surface for a run report.

        It is the same aggregate as :meth:`totals`; the alias exists because
        report code reads better asking for a summary than for totals.
        """
        return self.totals()

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "totals": self.totals(),
            "counts": self.counts(),
        }


def make_record(
    *,
    phase: RequestPhase,
    provider: str,
    model: str,
    ok: bool,
    latency_s: float,
    usage_source: UsageSource,
    queue_s: float | None = None,
    load_s: float | None = None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    prompt_s: float | None = None,
    decode_s: float | None = None,
    tool_s: float | None = None,
    verification_s: float | None = None,
    teardown_s: float | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
    tool_calls: int = 0,
    salvaged_tool_calls: int = 0,
    error_class: str | None = None,
    error: str | None = None,
    started_at: _dt.datetime | None = None,
) -> RequestRecord:
    """Build one terminal record.

    The optional timing fields default to ``None`` rather than ``0.0``: a
    caller that did not measure a phase must leave it unobserved, because a
    zero is indistinguishable from a measurement that happened to be fast.
    """

    def optional_round(value: float | None) -> float | None:
        return None if value is None else round(value, 6)

    return RequestRecord(
        index=0,
        phase=phase,
        provider=provider,
        model=model,
        ok=ok,
        queue_s=optional_round(queue_s),
        load_s=optional_round(load_s),
        latency_s=round(latency_s, 6),
        usage_source=usage_source,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        prompt_s=optional_round(prompt_s),
        decode_s=optional_round(decode_s),
        tool_s=optional_round(tool_s),
        verification_s=optional_round(verification_s),
        teardown_s=optional_round(teardown_s),
        task_id=task_id,
        attempt=attempt,
        tool_calls=tool_calls,
        salvaged_tool_calls=salvaged_tool_calls,
        error_class=error_class,
        error=error,
        started_at=started_at or _dt.datetime.now(_dt.UTC),
    )


class Timer:
    """A monotonic timer for one phase of a request."""

    def __init__(self) -> None:
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started


# --------------------------------------------------------------- ambient ledger

_CURRENT_LEDGER: ContextVar[RequestLedger | None] = ContextVar(
    "agent_squad_request_ledger", default=None
)


def current_ledger() -> RequestLedger | None:
    """The ledger bound to the current dispatch, if any.

    Providers prefer an explicitly injected ledger.  The ambient binding is
    what lets a router account for a provider that was wrapped by another
    layer (a replay recorder, for example) without that wrapper knowing
    anything about accounting.
    """
    return _CURRENT_LEDGER.get()


@contextlib.contextmanager
def bind_ledger(ledger: RequestLedger) -> Iterator[RequestLedger]:
    """Bind ``ledger`` to the current task for the duration of a dispatch."""
    token = _CURRENT_LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _CURRENT_LEDGER.reset(token)


__all__ = [
    "RequestLedger",
    "RequestPhase",
    "RequestRecord",
    "Timer",
    "UsageSource",
    "bind_ledger",
    "current_ledger",
    "make_record",
]
