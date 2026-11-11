"""OpenAI-compatible chat-completions provider.

Works against any endpoint that speaks the ``/v1/chat/completions`` shape:
OpenAI itself, vLLM, llama.cpp's server, Ollama's compatibility layer,
LM Studio, or a local gateway.  The same class therefore covers both the
"remote LLM API" and the "local / OpenAI-compatible endpoint" requirement.

Error handling distinguishes transient conditions (429, 5xx, connection
errors) from permanent ones (400, 401, 404): only transient failures are
marked retryable, because retrying a malformed request forever is a bug, not
resilience.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..errors import ModelError, TransientModelError
from .base import (
    BaseProvider,
    ChatMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ToolCall,
    ToolSpec,
    Usage,
)


class OpenAICompatProvider(BaseProvider):
    name = "openai_compat"
    capabilities = ProviderCapabilities(
        supports_tools=True,
        supports_system_prompt=True,
        supports_seed=True,
        supports_parallel_tool_calls=False,
        supports_json_schema=True,
    )

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout_s: float = 120.0,
        max_concurrency: int = 4,
        extra_headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        salvage_tool_calls: bool = True,
    ) -> None:
        super().__init__(model=model)
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_s,
            headers=extra_headers or {},
            limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=max_concurrency),
        )
        self.salvage_tool_calls = salvage_tool_calls
        #: How many responses needed the salvage path.  A non-zero count is
        #: a fact about the model, not a defect, and the benchmark reports it.
        self.salvaged_calls = 0

    # ------------------------------------------------------- tool salvage

    @staticmethod
    def _extract_json_objects(text: str) -> list[dict]:
        """Pull candidate JSON objects out of a model's free text.

        Two shapes are considered: a fenced code block containing JSON, and
        the whole text if it is itself a JSON object.  Nothing else is
        considered -- in particular, prose that merely *mentions* JSON is not
        a candidate.
        """
        candidates: list[dict] = []
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                candidates.append(parsed)
        for block in re.findall(r"```(?:json|tool_call)?\s*(.*?)```", text, re.DOTALL):
            block = block.strip()
            if not block.startswith("{"):
                continue
            try:
                parsed = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                candidates.append(parsed)
        return candidates

    def _salvage(
        self, text: str, tools: list[ToolSpec]
    ) -> list[ToolCall]:
        """Recover tool calls that a model emitted as plain text.

        Small models frequently render a tool call as a JSON code block in
        the assistant's content instead of using the structured channel.  The
        recovery is deliberately strict:

        * the JSON must name a tool that was actually offered;
        * the arguments must be a JSON object;
        * nothing is inferred -- a missing field stays missing.

        Anything that fails those checks is left alone, so the model sees its
        own text and can correct it.  The count of salvaged calls is
        recorded so that a caller can tell how much of the model's apparent
        competence came from this path.
        """
        if not self.salvage_tool_calls:
            return []
        if not tools or not text:
            return []
        allowed = {tool.name for tool in tools}
        salvaged: list[ToolCall] = []
        for candidate in self._extract_json_objects(text):
            name = candidate.get("name") or candidate.get("tool") or candidate.get("function")
            if isinstance(name, dict):
                name = name.get("name")
            arguments = candidate.get("arguments")
            if arguments is None:
                arguments = candidate.get("parameters")
            if arguments is None:
                arguments = candidate.get("args")
            if not isinstance(name, str) or name not in allowed:
                continue
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                continue
            salvaged.append(ToolCall(name=name, arguments=arguments))
        return salvaged

    # ------------------------------------------------------------- encoding

    @staticmethod
    def _encode_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        encoded: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                encoded.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": message.content,
                    }
                )
                continue
            payload: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, separators=(",", ":")),
                        },
                    }
                    for call in message.tool_calls
                ]
            encoded.append(payload)
        return encoded

    @staticmethod
    def _encode_tools(tools: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    # ---------------------------------------------------------------- call

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._encode_messages(request.messages),
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.stop:
            body["stop"] = request.stop
        if request.tools:
            body["tools"] = self._encode_tools(request.tools)
            body["tool_choice"] = "auto"
        if request.response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.metadata.get("schema_name", "response"),
                    "schema": request.response_schema,
                    "strict": False,
                },
            }

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        import time

        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions", json=body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise TransientModelError(
                f"model request timed out after {self.timeout_s}s", provider=self.name
            ) from exc
        except httpx.HTTPError as exc:
            # httpx exceptions often have an empty string representation, so
            # the type is included: "model request failed: " on its own tells
            # an operator nothing.
            detail = str(exc) or type(exc).__name__
            raise TransientModelError(
                f"model request failed ({type(exc).__name__}): {detail}",
                provider=self.name,
            ) from exc

        elapsed = time.monotonic() - started
        if response.status_code >= 400:
            detail = response.text[:500]
            if response.status_code in (408, 409, 425, 429) or response.status_code >= 500:
                raise TransientModelError(
                    f"model endpoint returned {response.status_code}: {detail}",
                    provider=self.name,
                    status_code=response.status_code,
                )
            raise ModelError(
                f"model endpoint returned {response.status_code}: {detail}",
                provider=self.name,
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ModelError(
                "model endpoint returned a non-JSON body", provider=self.name
            ) from exc
        return self._decode(data, elapsed, request.tools)

    def _decode(
        self,
        data: dict[str, Any],
        elapsed: float,
        tools: list[ToolSpec] | None = None,
    ) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ModelError("model response contained no choices", provider=self.name)
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            args_raw = function.get("arguments") or "{}"
            try:
                arguments = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except json.JSONDecodeError:
                arguments = {"_raw": args_raw}
            calls.append(
                ToolCall(id=raw.get("id") or "", name=function.get("name") or "", arguments=arguments)
            )
        salvaged = False
        if not calls:
            recovered = self._salvage(text, tools or [])
            if recovered:
                calls = recovered
                salvaged = True
                self.salvaged_calls += 1
        raw_usage = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
            cached_input_tokens=int(
                (raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
            estimated=False,
        )
        raw = dict(data)
        raw["salvaged_tool_call"] = salvaged
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            model=str(data.get("model") or self.model),
            provider=self.name,
            latency_s=elapsed,
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            raw=raw,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
