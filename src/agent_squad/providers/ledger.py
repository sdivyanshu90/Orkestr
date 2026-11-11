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
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    """One model request."""

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

    queue_s: float = 0.0
    """Time spent waiting for a provider slot before the request was sent."""

    latency_s: float = 0.0
    """Total wall-clock time of the request."""

    prompt_s: float | None = None
    """Prompt-processing time, when the provider reports it."""

    decode_s: float | None = None
    """Decode time, when the provider reports it."""

    prompt_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    usage_source: UsageSource = UsageSource.UNAVAILABLE

    tool_calls: int = 0
    salvaged_tool_calls: int = 0
    started_at: _dt.datetime = Field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RequestLedger:
    """The durable record of every model request a run made."""

    def __init__(self) -> None:
        self._records: list[RequestRecord] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- writing

    def record(self, record: RequestRecord) -> RequestRecord:
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

        return {
            "requests": len(records),
            "failed_requests": sum(1 for r in records if not r.ok),
            "reported_requests": len(reported),
            "estimated_requests": len(estimated),
            "unavailable_requests": len(unavailable),
            "prompt_tokens": total(reported, "prompt_tokens"),
            "output_tokens": total(reported, "output_tokens"),
            "cached_tokens": total(reported, "cached_tokens"),
            "total_latency_s": round(sum(r.latency_s for r in records), 3),
            "total_queue_s": round(sum(r.queue_s for r in records), 3),
            "total_tool_calls": sum(r.tool_calls for r in records),
            "total_salvaged_tool_calls": sum(r.salvaged_tool_calls for r in records),
            "phases": {
                phase.value: sum(1 for r in records if r.phase is phase)
                for phase in RequestPhase
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "totals": self.totals(),
        }


def make_record(
    *,
    phase: RequestPhase,
    provider: str,
    model: str,
    ok: bool,
    queue_s: float,
    latency_s: float,
    usage_source: UsageSource,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    prompt_s: float | None = None,
    decode_s: float | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
    tool_calls: int = 0,
    salvaged_tool_calls: int = 0,
    error_class: str | None = None,
    error: str | None = None,
) -> RequestRecord:
    """Build a record, with the clock stamping the start time."""
    return RequestRecord(
        index=0,
        phase=phase,
        provider=provider,
        model=model,
        ok=ok,
        queue_s=round(queue_s, 6),
        latency_s=round(latency_s, 6),
        usage_source=usage_source,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        prompt_s=prompt_s,
        decode_s=decode_s,
        task_id=task_id,
        attempt=attempt,
        tool_calls=tool_calls,
        salvaged_tool_calls=salvaged_tool_calls,
        error_class=error_class,
        error=error,
        started_at=_dt.datetime.now(_dt.UTC),
    )


class Timer:
    """A monotonic timer for one phase of a request."""

    def __init__(self) -> None:
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started
