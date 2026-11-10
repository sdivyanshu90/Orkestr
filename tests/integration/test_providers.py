"""Provider tests against a real HTTP server.

The OpenAI-compatible and Anthropic adapters are the only components that
talk to a third party, and they are the only ones that had no test until
now.  These tests run a real HTTP server on localhost and assert on the
actual bytes on the wire: the request shape, the header handling, the
response decoding and — most importantly — the distinction between a
transient failure (which must be retryable) and a permanent one (which
must not be).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_squad.config import ProviderConfig
from agent_squad.errors import ModelError, TransientModelError
from agent_squad.providers.anthropic import AnthropicProvider
from agent_squad.providers.base import ChatMessage, ModelRequest, ToolCall, ToolSpec
from agent_squad.providers.openai_compat import OpenAICompatProvider
from agent_squad.providers.registry import ProviderRouter, build_provider, compute_cost


class Recorder:
    """Captures the requests a provider makes."""

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
            return (200, {})


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
        self.recorder.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        status, payload = self.recorder.next_response()
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = _handle
    do_GET = _handle


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


def sample_request() -> ModelRequest:
    return ModelRequest(
        messages=[
            ChatMessage(role="system", content="be terse"),
            ChatMessage(role="user", content="add a helper"),
        ],
        tools=[
            ToolSpec(
                name="write_file",
                description="write a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        max_output_tokens=256,
    )


# ------------------------------------------------------- openai compatible


@pytest.mark.integration
async def test_openai_compatible_request_shape(server) -> None:
    recorder, base_url = server
    recorder.push(
        200,
        {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path": "a.py"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 45,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
        },
    )
    provider = OpenAICompatProvider(
        model="test-model", base_url=f"{base_url}/v1", api_key="secret-key"
    )
    try:
        response = await provider.complete(sample_request())
    finally:
        await provider.aclose()

    request = recorder.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer secret-key"
    body = request["body"]
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 256
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["tools"][0]["function"]["name"] == "write_file"
    assert body["tool_choice"] == "auto"

    assert response.text == "done"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "write_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 45
    assert response.usage.cached_input_tokens == 20
    assert response.usage.estimated is False
    assert response.raw["model"] == "test-model"


@pytest.mark.integration
async def test_openai_compatible_reports_usage_as_not_estimated(server) -> None:
    recorder, base_url = server
    recorder.push(
        200,
        {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )
    provider = OpenAICompatProvider(model="m", base_url=f"{base_url}/v1")
    try:
        response = await provider.complete(sample_request())
    finally:
        await provider.aclose()
    assert response.usage.estimated is False
    assert response.usage.total_tokens == 10


@pytest.mark.integration
async def test_openai_compatible_treats_429_as_transient(server) -> None:
    recorder, base_url = server
    recorder.push(429, {"error": "rate limited"})
    provider = OpenAICompatProvider(model="m", base_url=f"{base_url}/v1")
    try:
        with pytest.raises(TransientModelError):
            await provider.complete(sample_request())
    finally:
        await provider.aclose()


@pytest.mark.integration
async def test_openai_compatible_treats_500_as_transient(server) -> None:
    recorder, base_url = server
    recorder.push(503, {"error": "unavailable"})
    provider = OpenAICompatProvider(model="m", base_url=f"{base_url}/v1")
    try:
        with pytest.raises(TransientModelError):
            await provider.complete(sample_request())
    finally:
        await provider.aclose()


@pytest.mark.integration
async def test_openai_compatible_treats_401_as_permanent(server) -> None:
    """A permanent failure must not be retried forever."""
    recorder, base_url = server
    recorder.push(401, {"error": "invalid api key"})
    provider = OpenAICompatProvider(model="m", base_url=f"{base_url}/v1")
    try:
        with pytest.raises(ModelError) as exc:
            await provider.complete(sample_request())
    finally:
        await provider.aclose()
    assert not isinstance(exc.value, TransientModelError)
    assert "401" in exc.value.message


@pytest.mark.integration
async def test_openai_compatible_rejects_a_non_json_body(server) -> None:
    recorder, base_url = server
    recorder.push(200, {"not": "a chat completion"})
    provider = OpenAICompatProvider(model="m", base_url=f"{base_url}/v1")
    try:
        with pytest.raises(ModelError):
            await provider.complete(sample_request())
    finally:
        await provider.aclose()


@pytest.mark.integration
async def test_openai_compatible_handles_malformed_tool_arguments(server) -> None:
    """Broken tool arguments must not crash the decoder."""
    recorder, base_url = server
    recorder.push(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": "{not json"},
                            }
                        ],
                    }
                }
            ]
        },
    )
    provider = OpenAICompatProvider(model="m", base_url=f"{base_url}/v1")
    try:
        response = await provider.complete(sample_request())
    finally:
        await provider.aclose()
    assert response.tool_calls[0].arguments == {"_raw": "{not json"}


# ----------------------------------------------------------------- anthropic


@pytest.mark.integration
async def test_anthropic_request_shape(server) -> None:
    recorder, base_url = server
    recorder.push(
        200,
        {
            "model": "claude-test",
            "content": [
                {"type": "text", "text": "hello"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "write_file",
                    "input": {"path": "a.py"},
                },
            ],
            "usage": {"input_tokens": 30, "output_tokens": 12},
            "stop_reason": "tool_use",
        },
    )
    provider = AnthropicProvider(
        model="claude-test", base_url=base_url, api_key="ant-key"
    )
    try:
        response = await provider.complete(sample_request())
    finally:
        await provider.aclose()

    request = recorder.requests[0]
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == "ant-key"
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    body = request["body"]
    # Anthropic puts the system prompt in a top-level field, not a message.
    assert body["system"] == "be terse"
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["messages"][0]["role"] == "user"
    assert body["tools"][0]["name"] == "write_file"
    assert body["tools"][0]["input_schema"]["type"] == "object"

    assert response.text == "hello"
    assert response.tool_calls[0].name == "write_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.usage.input_tokens == 30
    assert response.usage.output_tokens == 12
    assert response.finish_reason == "tool_use"


@pytest.mark.integration
async def test_anthropic_encodes_tool_results(server) -> None:
    recorder, base_url = server
    recorder.push(200, {"content": [{"type": "text", "text": "ok"}], "usage": {}})
    provider = AnthropicProvider(model="m", base_url=base_url, api_key="k")
    request = ModelRequest(
        messages=[
            ChatMessage(role="user", content="go"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="tc1", name="write_file", arguments={"path": "a"}),),
            ),
            ChatMessage(role="tool", content="written", tool_call_id="tc1"),
        ]
    )
    try:
        await provider.complete(request)
    finally:
        await provider.aclose()

    body = recorder.requests[0]["body"]
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["content"][0]["type"] == "tool_use"
    assert assistant["content"][0]["id"] == "tc1"
    tool_turn = body["messages"][2]
    assert tool_turn["role"] == "user"
    assert tool_turn["content"][0]["type"] == "tool_result"
    assert tool_turn["content"][0]["tool_use_id"] == "tc1"


@pytest.mark.integration
async def test_anthropic_treats_429_as_transient(server) -> None:
    recorder, base_url = server
    recorder.push(429, {"error": "rate limited"})
    provider = AnthropicProvider(model="m", base_url=base_url, api_key="k")
    try:
        with pytest.raises(TransientModelError):
            await provider.complete(sample_request())
    finally:
        await provider.aclose()


# ------------------------------------------------------------------ routing


@pytest.mark.integration
async def test_the_router_falls_back_only_on_transient_failures(server) -> None:
    """A transient failure falls through; a permanent one does not."""
    _recorder, _ = server

    class AlwaysTransient:
        name = "primary"
        model = "primary"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            raise TransientModelError("primary is down")

        async def aclose(self):
            return None

    class Working:
        name = "fallback"
        model = "fallback"
        calls = 0

        async def complete(self, request):
            from agent_squad.providers.base import ModelResponse

            self.calls += 1
            return ModelResponse(text="from the fallback", provider="fallback", model="fallback")

        async def aclose(self):
            return None

    primary = AlwaysTransient()
    fallback = Working()
    router = ProviderRouter(
        [primary, fallback],
        [
            ProviderConfig(name="openai_compat", model="primary"),
            ProviderConfig(name="openai_compat", model="fallback"),
        ],
    )
    response = await router.complete(sample_request())
    assert response.text == "from the fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.integration
async def test_the_router_does_not_fall_back_on_a_permanent_failure() -> None:
    class AlwaysPermanent:
        name = "primary"
        model = "primary"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            raise ModelError("invalid api key")

        async def aclose(self):
            return None

    class Fallback:
        name = "fallback"
        model = "fallback"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            raise AssertionError("the fallback must not be used")

        async def aclose(self):
            return None

    primary = AlwaysPermanent()
    fallback = Fallback()
    router = ProviderRouter(
        [primary, fallback],
        [
            ProviderConfig(name="openai_compat", model="primary"),
            ProviderConfig(name="openai_compat", model="fallback"),
        ],
    )
    with pytest.raises(ModelError):
        await router.complete(sample_request())
    assert primary.calls == 1
    assert fallback.calls == 0


# --------------------------------------------------------------- registry


def test_the_registry_builds_each_supported_provider() -> None:
    from agent_squad.providers.anthropic import AnthropicProvider as A
    from agent_squad.providers.mock import MockProvider as M
    from agent_squad.providers.openai_compat import OpenAICompatProvider as O

    assert isinstance(
        build_provider(ProviderConfig(name="mock", model="m")), M
    )
    assert isinstance(
        build_provider(ProviderConfig(name="openai_compat", model="m")), O
    )
    assert isinstance(
        build_provider(ProviderConfig(name="anthropic", model="m")), A
    )


def test_the_registry_rejects_an_unknown_provider() -> None:
    from agent_squad.errors import ValidationError

    with pytest.raises(ValidationError):
        build_provider(ProviderConfig(name="telepathy", model="m"))


def test_the_api_key_is_read_from_the_environment_by_name(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SQUAD_TEST_KEY", "sk-from-env")
    config = ProviderConfig(name="openai_compat", model="m", api_key_env="AGENT_SQUAD_TEST_KEY")
    assert config.api_key() == "sk-from-env"


def test_a_missing_api_key_environment_variable_yields_none(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SQUAD_ABSENT_KEY", raising=False)
    config = ProviderConfig(name="openai_compat", model="m", api_key_env="AGENT_SQUAD_ABSENT_KEY")
    assert config.api_key() is None


def test_cost_is_computed_from_the_configured_prices() -> None:
    from agent_squad.providers.base import Usage

    config = ProviderConfig(
        name="openai_compat", model="m", price_input_per_1k=1.0, price_output_per_1k=2.0
    )
    usage = Usage(input_tokens=1000, output_tokens=1000, estimated=False)
    assert compute_cost(usage, config) == pytest.approx(3.0)
