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
            raise TransientModelError(
                f"model request failed: {exc}", provider=self.name
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
        return self._decode(data, elapsed)

    def _decode(self, data: dict[str, Any], elapsed: float) -> ModelResponse:
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
        raw_usage = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
            cached_input_tokens=int(
                (raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
            estimated=False,
        )
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            model=str(data.get("model") or self.model),
            provider=self.name,
            latency_s=elapsed,
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            raw=data,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
