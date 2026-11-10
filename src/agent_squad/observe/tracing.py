"""Tracing.

OpenTelemetry is an optional dependency.  When it is not installed, the
tracer degrades to a no-op that still records span metadata in memory, so
that the observability *interface* is stable and testable regardless of
whether an exporter is configured.

The in-memory span recorder is not a toy: it is what the replay feature and
the benchmark harness read to reconstruct the shape of an execution.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from ..models.task import utcnow


@dataclass
class Span:
    """A single recorded unit of work."""

    name: str
    span_id: str
    parent_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "unset"
    error: str | None = None

    @property
    def duration_s(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.duration_s, 6),
            "attributes": dict(self.attributes),
            "events": list(self.events),
            "status": self.status,
            "error": self.error,
        }


class SpanRecorder:
    """Collects spans in memory for inspection, replay and benchmarking."""

    def __init__(self) -> None:
        self._spans: list[Span] = []
        self._stack: list[str] = []

    def start(self, name: str, **attributes: Any) -> Span:
        from ..ids import span_id

        span = Span(
            name=name,
            span_id=span_id(),
            parent_id=self._stack[-1] if self._stack else None,
            attributes=dict(attributes),
        )
        self._spans.append(span)
        return span

    def end(self, span: Span, *, status: str = "ok", error: str | None = None) -> None:
        span.ended_at = time.time()
        span.status = status
        span.error = error

    def push(self, span: Span) -> None:
        self._stack.append(span.span_id)

    def pop(self) -> None:
        if self._stack:
            self._stack.pop()

    @property
    def spans(self) -> list[Span]:
        return list(self._spans)

    def by_name(self, name: str) -> list[Span]:
        return [s for s in self._spans if s.name == name]

    def critical_path(self) -> list[Span]:
        """The longest chain of nested spans.

        The critical path is approximated by the deepest chain of spans that
        contain one another, which for this runtime corresponds to the
        scheduler's task -> agent -> model chain.
        """
        if not self._spans:
            return []
        children: dict[str | None, list[Span]] = {}
        for span in self._spans:
            children.setdefault(span.parent_id, []).append(span)

        def longest(parent: str | None) -> list[Span]:
            best: list[Span] = []
            for span in children.get(parent, []):
                candidate = [span, *longest(span.span_id)]
                if sum(s.duration_s for s in candidate) > sum(s.duration_s for s in best):
                    best = candidate
            return best

        return longest(None)


class Tracer:
    """A tracing facade that works with or without OpenTelemetry installed."""

    def __init__(
        self,
        *,
        service_name: str = "agent-squad",
        otlp_endpoint: str | None = None,
        sample_rate: float = 1.0,
    ) -> None:
        self.service_name = service_name
        self.otlp_endpoint = otlp_endpoint
        self.sample_rate = sample_rate
        self.recorder = SpanRecorder()
        self._otel_tracer: Any = None
        self._enabled = False
        if otlp_endpoint:
            self._enabled = self._init_otel()

    def _init_otel(self) -> bool:
        try:  # pragma: no cover - optional dependency
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(
                resource=Resource.create({"service.name": self.service_name})
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=self.otlp_endpoint))
            )
            trace.set_tracer_provider(provider)
            self._otel_tracer = trace.get_tracer(self.service_name)
            return True
        except Exception:
            # OpenTelemetry is optional: a missing exporter must never break a
            # run, only reduce observability.
            return False

    @contextmanager
    def span(
        self, name: str, *, run_id: str | None = None, task_id: str | None = None, **attributes: Any
    ) -> Iterator[Span]:
        """Record a span, and export it when OpenTelemetry is available."""
        span = self.recorder.start(
            name, run_id=run_id, task_id=task_id, **attributes
        )
        otel_ctx = None
        if self._enabled and self._otel_tracer is not None:  # pragma: no cover
            try:
                otel_ctx = self._otel_tracer.start_as_current_span(name)
                otel_ctx.__enter__()
            except Exception:
                otel_ctx = None
        self.recorder.push(span)
        try:
            yield span
        except Exception as exc:
            self.recorder.end(span, status="error", error=str(exc))
            raise
        else:
            self.recorder.end(span, status="ok")
        finally:
            self.recorder.pop()
            if otel_ctx is not None:  # pragma: no cover
                with suppress(Exception):
                    otel_ctx.__exit__(None, None, None)

    def event(self, span: Span, name: str, **attributes: Any) -> None:
        span.events.append({"name": name, "at": utcnow().isoformat(), **attributes})

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service_name,
            "otlp_enabled": self._enabled,
            "spans": [s.to_dict() for s in self.recorder.spans],
        }
