"""End-to-end request accounting against a real recording endpoint.

A ledger that is only tested against a fake proves the ledger; these tests
prove the *request path*.  Every assertion reconciles the provider's terminal
records with the number of HTTP requests the endpoint actually served, so a
missing record (or a double record) fails exactly when the two counts
disagree.  Failures are injected on both sides of the usage boundary, and a
fallback is exercised through the real router.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_squad.config import ProviderConfig
from agent_squad.errors import ModelError, TransientModelError
from agent_squad.providers.anthropic import AnthropicProvider
from agent_squad.providers.base import ChatMessage, ModelRequest
from agent_squad.providers.ledger import RequestLedger, RequestPhase, UsageSource
from agent_squad.providers.openai_compat import OpenAICompatProvider
from agent_squad.providers.registry import ProviderRouter


class Recorder:
    """The recording endpoint's queue and request log."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[tuple[int, Any]] = []
        self.lock = threading.Lock()

    def push(self, status: int, body: Any) -> None:
        with self.lock:
            self.responses.append((status, body))

    def next_response(self) -> tuple[int, Any]:
        with self.lock:
            if self.responses:
                return self.responses.pop(0)
            return (500, {"error": "the recording endpoint has no response queued"})

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.requests)


class Handler(BaseHTTPRequestHandler):
    recorder: Recorder

    def log_message(self, *args: Any) -> None:
        return

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", "replace")}
        with self.recorder.lock:
            self.recorder.requests.append({"path": self.path, "body": body})
        status, payload = self.recorder.next_response()
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = _handle


@pytest.fixture()
def server():
    recorder = Recorder()
    handler = type("BoundHandler", (Handler,), {"recorder": recorder})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield recorder, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def make_request(*, phase: str | None = None, attempt: int | None = None) -> ModelRequest:
    metadata: dict[str, object] = {}
    if phase is not None:
        metadata["ledger_phase"] = phase
    if attempt is not None:
        metadata["ledger_attempt"] = attempt
    return ModelRequest(
        messages=[ChatMessage(role="user", content="write the helper")],
        metadata=metadata,
    )


def completion(prompt_tokens: int, output_tokens: int, *, text: str = "done") -> dict[str, Any]:
    return {
        "model": "recording-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": output_tokens},
    }


@pytest.mark.integration
async def test_records_reconcile_exactly_with_the_recording_endpoint(server) -> None:
    recorder, base_url = server
    recorder.push(200, completion(10, 5))
    recorder.push(200, completion(20, 7))
    recorder.push(503, {"error": "temporarily unavailable"})
    recorder.push(200, completion(30, 9))

    ledger = RequestLedger()
    provider = OpenAICompatProvider(
        model="recording-model", base_url=f"{base_url}/v1", ledger=ledger
    )

    phases = ("planner", "worker", "worker", "retry")
    try:
        for phase in phases:
            with contextlib.suppress(TransientModelError):
                await provider.complete(make_request(phase=phase))
    finally:
        await provider.aclose()

    # The endpoint served exactly one request per dispatched attempt, and
    # the ledger holds exactly one terminal record for each of them.
    assert recorder.count == len(phases) == len(ledger.records)
    counts = ledger.counts()
    assert counts["requests"] == recorder.count
    assert counts["terminal"] == recorder.count
    assert counts["ok"] == 3
    assert counts["failed"] == 1
    assert [record.index for record in ledger.records] == [0, 1, 2, 3]
    assert counts["phases"] == {
        "planner": 1,
        "worker": 2,
        "reviewer": 0,
        "retry": 1,
        "fallback": 0,
        "other": 0,
    }

    # Only the three successful attempts had reported usage.  The failed
    # attempt contributes no tokens at all, not a zero.
    totals = ledger.totals()
    assert totals["reported_requests"] == 3
    assert totals["unavailable_requests"] == 1
    assert totals["prompt_tokens"] == 60
    assert totals["output_tokens"] == 21
    assert records_with(ledger, ok=False)[0].prompt_tokens is None


def records_with(ledger: RequestLedger, *, ok: bool) -> list[Any]:
    return [record for record in ledger.records if record.ok is ok]


@pytest.mark.integration
async def test_a_failure_before_usage_is_terminal_and_unavailable(server) -> None:
    recorder, base_url = server
    recorder.push(500, {"error": "backend exploded"})

    ledger = RequestLedger()
    provider = OpenAICompatProvider(
        model="recording-model", base_url=f"{base_url}/v1", ledger=ledger
    )
    try:
        with pytest.raises(TransientModelError):
            await provider.complete(make_request(phase="worker"))
    finally:
        await provider.aclose()

    (record,) = ledger.records
    assert recorder.count == 1
    assert record.ok is False
    assert record.error_class == "model"
    assert record.usage_source is UsageSource.UNAVAILABLE
    assert record.prompt_tokens is None
    assert record.output_tokens is None
    assert record.cached_tokens is None


@pytest.mark.integration
async def test_a_connection_error_is_terminal_and_unavailable() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    ledger = RequestLedger()
    provider = OpenAICompatProvider(
        model="recording-model",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout_s=1.0,
        ledger=ledger,
    )
    try:
        with pytest.raises(TransientModelError):
            await provider.complete(make_request(phase="worker"))
    finally:
        await provider.aclose()

    (record,) = ledger.records
    assert record.ok is False
    assert record.usage_source is UsageSource.UNAVAILABLE
    assert record.prompt_tokens is None
    assert record.output_tokens is None


@pytest.mark.integration
async def test_a_failure_after_usage_is_terminal_and_keeps_the_reported_numbers(server) -> None:
    """The endpoint reports usage, then the body fails to decode."""
    recorder, base_url = server
    recorder.push(
        200,
        {
            "choices": [{"message": "this is not a message object"}],
            "usage": {"prompt_tokens": 41, "completion_tokens": 13},
        },
    )

    ledger = RequestLedger()
    provider = OpenAICompatProvider(
        model="recording-model", base_url=f"{base_url}/v1", ledger=ledger
    )
    try:
        with pytest.raises(ModelError):
            await provider.complete(make_request(phase="worker"))
    finally:
        await provider.aclose()

    (record,) = ledger.records
    assert recorder.count == 1
    assert record.ok is False
    assert record.usage_source is UsageSource.REPORTED
    assert record.prompt_tokens == 41
    assert record.output_tokens == 13


@pytest.mark.integration
async def test_a_successful_response_without_usage_is_null_not_measured_zero(server) -> None:
    recorder, base_url = server
    recorder.push(
        200,
        {
            "model": "recording-model",
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        },
    )

    ledger = RequestLedger()
    provider = OpenAICompatProvider(
        model="recording-model", base_url=f"{base_url}/v1", ledger=ledger
    )
    try:
        await provider.complete(make_request())
    finally:
        await provider.aclose()

    (record,) = ledger.records
    assert recorder.count == 1
    assert record.ok is True
    assert record.usage_source is UsageSource.UNAVAILABLE
    assert record.prompt_tokens is None
    assert record.output_tokens is None


@pytest.mark.integration
async def test_anthropic_failure_is_terminal_and_reconciles(server) -> None:
    recorder, base_url = server
    recorder.push(429, {"error": "rate limited"})

    ledger = RequestLedger()
    provider = AnthropicProvider(
        model="recording-model", base_url=base_url, ledger=ledger
    )
    try:
        with pytest.raises(TransientModelError):
            await provider.complete(make_request(phase="reviewer"))
    finally:
        await provider.aclose()

    (record,) = ledger.records
    assert recorder.count == 1
    assert record.phase is RequestPhase.REVIEWER
    assert record.ok is False
    assert record.usage_source is UsageSource.UNAVAILABLE
    assert record.prompt_tokens is None


@pytest.mark.integration
async def test_a_fallback_reconciles_as_its_own_record(server) -> None:
    recorder, base_url = server
    recorder.push(503, {"error": "primary is down"})
    recorder.push(200, completion(8, 4, text="from the fallback"))

    ledger = RequestLedger()
    primary = OpenAICompatProvider(model="primary", base_url=f"{base_url}/v1")
    primary.name = "primary"
    fallback = OpenAICompatProvider(model="fallback", base_url=f"{base_url}/v1")
    fallback.name = "fallback"
    router = ProviderRouter(
        [primary, fallback],
        [
            ProviderConfig(name="openai_compat", model="primary"),
            ProviderConfig(name="openai_compat", model="fallback"),
        ],
        ledger=ledger,
    )

    try:
        response = await router.complete(make_request(phase="worker"))
    finally:
        await router.aclose()

    assert response.text == "from the fallback"
    assert recorder.count == 2 == len(ledger.records)
    assert ledger.counts()["requests"] == recorder.count
    assert ledger.counts()["phases"]["fallback"] == 1
    assert ledger.counts()["phases"]["worker"] == 1
    first, second = ledger.records
    assert first.ok is False
    assert first.provider == "primary"
    assert first.usage_source is UsageSource.UNAVAILABLE
    assert second.ok is True
    assert second.provider == "fallback"
    assert second.phase is RequestPhase.FALLBACK
    assert second.usage_source is UsageSource.REPORTED
    assert second.prompt_tokens == 8
    assert second.output_tokens == 4
