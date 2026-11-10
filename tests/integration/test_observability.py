"""Observability tests.

The observability layer must describe work that *actually happened*.  These
tests assert that a real run produces spans whose durations and attributes
correspond to the recorded execution, rather than merely asserting that a
span object exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import two_independent_spec

from agent_squad.config import RunConfig
from agent_squad.observe.events import StructuredLogger
from agent_squad.observe.metrics import RunMetrics
from agent_squad.observe.tracing import SpanRecorder, Tracer
from agent_squad.orchestrator import Orchestrator


@pytest.fixture()
def traced_run(tmp_path: Path, repo: Path, caplog):
    config = RunConfig(
        run_id="run_traced",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        observability={"log_level": "DEBUG", "log_json": True},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    import asyncio

    asyncio.run(orchestrator.run("traced", request_spec=two_independent_spec()))
    return orchestrator


@pytest.mark.integration
def test_a_run_produces_spans_for_every_phase(traced_run) -> None:
    spans = traced_run.tracer.recorder.spans
    names = {s.name for s in spans}
    assert "run" in names
    assert "inspect" in names
    assert "plan" in names
    assert "strategy" in names
    assert "task" in names
    assert "model.call" in names
    assert "integration" in names


@pytest.mark.integration
def test_the_spans_have_real_durations(traced_run) -> None:
    spans = traced_run.tracer.recorder.spans
    assert spans
    for span in spans:
        assert span.ended_at is not None, span.name
        assert span.duration_s >= 0.0
    run_span = next(s for s in spans if s.name == "run")
    assert run_span.duration_s > 0.0
    # The task spans must fit inside the run span.
    task_spans = [s for s in spans if s.name == "task"]
    assert task_spans
    for task_span in task_spans:
        assert task_span.duration_s <= run_span.duration_s + 0.001


@pytest.mark.integration
def test_the_model_spans_record_the_real_usage(traced_run) -> None:
    spans = traced_run.tracer.recorder.by_name("model.call")
    assert spans
    for span in spans:
        assert span.attributes["provider"] == "mock"
        assert span.attributes["input_tokens"] > 0
        assert span.attributes["output_tokens"] > 0
        assert "estimated" in span.attributes


@pytest.mark.integration
def test_the_task_spans_nest_inside_the_run_span(traced_run) -> None:
    """The span tree must reflect the actual call structure."""
    spans = {s.span_id: s for s in traced_run.tracer.recorder.spans}
    run_span = next(s for s in spans.values() if s.name == "run")
    task_spans = [s for s in spans.values() if s.name == "task"]
    assert task_spans
    for task_span in task_spans:
        parent = spans[task_span.parent_id] if task_span.parent_id else None
        assert parent is not None, "a task span must have a parent"
        assert parent.name in ("run", "task")


@pytest.mark.integration
def test_the_critical_path_can_be_extracted(traced_run) -> None:
    path = traced_run.tracer.recorder.critical_path()
    assert path
    assert path[0].name == "run"


@pytest.mark.integration
def test_the_tracer_degrades_gracefully_without_opentelemetry() -> None:
    """A missing exporter must never break a run."""
    tracer = Tracer(service_name="test", otlp_endpoint=None)
    assert tracer._enabled is False
    with tracer.span("unit", run_id="r") as span:
        span.attributes["x"] = 1
    assert tracer.recorder.spans[0].attributes["x"] == 1


def test_a_span_records_an_error_without_swallowing_it() -> None:
    tracer = Tracer(service_name="test")
    with pytest.raises(ValueError), tracer.span("failing"):
        raise ValueError("boom")
    span = tracer.recorder.spans[0]
    assert span.status == "error"
    assert "boom" in (span.error or "")


def test_the_span_recorder_keeps_parent_links() -> None:
    recorder = SpanRecorder()
    outer = recorder.start("outer")
    recorder.push(outer)
    inner = recorder.start("inner")
    recorder.push(inner)
    recorder.end(inner)
    recorder.pop()
    recorder.end(outer)
    recorder.pop()
    assert inner.parent_id == outer.span_id
    assert outer.parent_id is None


def test_the_structured_logger_writes_json(tmp_path: Path) -> None:
    import json

    path = tmp_path / "log.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        logger = StructuredLogger(level="INFO", json_output=True, stream=handle)
        logger.info("hello", run_id="r1", count=2)
        logger.debug("not written")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "hello"
    assert payload["run_id"] == "r1"
    assert payload["count"] == 2
    assert payload["level"] == "INFO"


def test_the_structured_logger_respects_the_level(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    with path.open("w", encoding="utf-8") as handle:
        logger = StructuredLogger(level="WARNING", stream=handle)
        logger.info("suppressed")
        logger.warning("kept")
    text = path.read_text(encoding="utf-8")
    assert "suppressed" not in text
    assert "kept" in text


def test_the_logger_context_is_inherited_by_children() -> None:
    import io

    buffer = io.StringIO()
    parent = StructuredLogger(level="DEBUG", json_output=True, stream=buffer)
    child = parent.bind(task_id="t1")
    child.info("scoped")
    payload = buffer.getvalue()
    assert "t1" in payload


def test_the_metrics_model_reports_per_task_costs() -> None:
    metrics = RunMetrics(
        run_id="r",
        total_tasks=4,
        verified_tasks=2,
        tokens_used=1000,
        cost_usd=0.25,
    )
    assert metrics.verified_completion_rate == pytest.approx(0.5)
    assert metrics.tokens_per_verified_task == pytest.approx(500)
    assert metrics.cost_per_verified_task == pytest.approx(0.125)
