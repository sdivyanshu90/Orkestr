"""Tool-call salvage tests.

Small models frequently render a tool call as a JSON code block in the
assistant's content instead of using the structured channel.  The provider
recovers those calls, but only under strict conditions, and the number of
recoveries is reported so that a caller can tell how much of a model's
apparent competence came from the salvage path.

These tests pin both halves: what must be recovered, and what must be
refused.
"""

from __future__ import annotations

import json

import pytest

from agent_squad.providers.base import ModelRequest, ToolSpec
from agent_squad.providers.openai_compat import OpenAICompatProvider

TOOLS = [
    ToolSpec(
        name="write_file",
        description="Write a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        },
    ),
    ToolSpec(name="run_command", description="Run a command", parameters={}),
]


@pytest.fixture()
def provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(salvage_tool_calls=True)


def test_a_plain_json_tool_call_is_recovered(provider) -> None:
    calls = provider._salvage(
        json.dumps({"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}),
        TOOLS,
    )
    assert len(calls) == 1
    assert calls[0].name == "write_file"
    assert calls[0].arguments == {"path": "a.py", "content": "x"}


def test_a_fenced_json_tool_call_is_recovered(provider) -> None:
    text = (
        "I will create the file.\n\n"
        "```json\n"
        '{"name": "write_file", "arguments": {"path": "b.py"}}\n'
        "```\n"
    )
    calls = provider._salvage(text, TOOLS)
    assert len(calls) == 1
    assert calls[0].arguments == {"path": "b.py"}


def test_an_unknown_tool_is_refused(provider) -> None:
    calls = provider._salvage('{"name": "rm_rf", "arguments": {}}', TOOLS)
    assert calls == []


def test_prose_is_not_a_tool_call(provider) -> None:
    text = (
        "I will write the file hello.py containing print(1). "
        'The tool call would look like {"name": "write_file", "arguments": {}} '
        "but I am only describing it."
    )
    # The JSON is present but embedded in prose that does not parse as a
    # whole-object, and there is no code fence, so nothing is recovered.
    assert provider._salvage(text, TOOLS) == []


def test_arguments_that_are_not_an_object_are_refused(provider) -> None:
    calls = provider._salvage('{"name": "write_file", "arguments": "path=a.py"}', TOOLS)
    assert calls == []


def test_missing_arguments_become_an_empty_object(provider) -> None:
    """The tool's own argument validation is what rejects a bad call."""
    calls = provider._salvage('{"name": "write_file"}', TOOLS)
    assert len(calls) == 1
    assert calls[0].arguments == {}


def test_nothing_is_recovered_without_tools(provider) -> None:
    assert provider._salvage('{"name": "write_file", "arguments": {}}', []) == []


def test_nothing_is_recovered_from_empty_text(provider) -> None:
    assert provider._salvage("", TOOLS) == []


def test_the_alternative_argument_spellings_are_accepted(provider) -> None:
    for key in ("arguments", "parameters", "args"):
        calls = provider._salvage(
            json.dumps({"name": "write_file", key: {"path": "c.py"}}), TOOLS
        )
        assert len(calls) == 1, key
        assert calls[0].arguments == {"path": "c.py"}


def test_the_salvage_can_be_disabled() -> None:
    disabled = OpenAICompatProvider(salvage_tool_calls=False)
    assert disabled._salvage('{"name": "write_file", "arguments": {}}', TOOLS) == []


@pytest.mark.integration
async def test_a_text_only_response_is_decoded_with_the_salvaged_call() -> None:
    """The whole path: HTTP response -> decoded response -> tool call."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '```json\n{"name": "write_file", "arguments": {"path": "x.py"}}\n```',
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OpenAICompatProvider(
            model="m", base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        )
        request = ModelRequest(messages=[], tools=TOOLS)
        try:
            response = await provider.complete(request)
        finally:
            await provider.aclose()
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "write_file"
    assert response.raw["salvaged_tool_call"] is True
    assert provider.salvaged_calls == 1


@pytest.mark.integration
async def test_a_structured_tool_call_is_not_double_counted() -> None:
    """A provider that *does* use the tool channel must not trigger salvage."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path": "y.py"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OpenAICompatProvider(
            model="m", base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        )
        try:
            response = await provider.complete(ModelRequest(messages=[], tools=TOOLS))
        finally:
            await provider.aclose()
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert len(response.tool_calls) == 1
    assert response.raw["salvaged_tool_call"] is False
    assert provider.salvaged_calls == 0
