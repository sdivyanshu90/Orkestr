"""Anthropic Messages API provider.

A second, genuinely different wire protocol exists so that the abstraction is
not accidentally shaped around one vendor's quirks: Anthropic puts the system
prompt in a top-level field, streams tool input as JSON fragments, and reports
usage differently.  All of that is normalised here, and the raw payload is
kept on :attr:`~agent_squad.providers.base.ModelResponse.raw`.
"""

from __future__ import annotations

import time
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


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    capabilities = ProviderCapabilities(
        supports_tools=True,
        supports_system_prompt=True,
        supports_seed=False,
        supports_parallel_tool_calls=False,
        supports_json_schema=False,
    )

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        *,
        base_url: str = "https://api.anthropic.com",
        api_key: str | None = None,
        timeout_s: float = 120.0,
        max_concurrency: int = 4,
        api_version: str = "2023-06-01",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(model=model)
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_s = timeout_s
        self._api_version = api_version
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_s,
            limits=httpx.Limits(
                max_connections=max_concurrency, max_keepalive_connections=max_concurrency
            ),
        )

    @staticmethod
    def _split_system(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        turns: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            if message.role == "tool":
                turns.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "",
                                "content": message.content,
                            }
                        ],
                    }
                )
                continue
            if message.role == "assistant" and message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                turns.append({"role": "assistant", "content": blocks})
                continue
            turns.append({"role": message.role, "content": message.content})
        return "\n\n".join(system_parts), turns

    async def complete(self, request: ModelRequest) -> ModelResponse:
        system, turns = self._split_system(request.messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": turns,
        }
        if system:
            body["system"] = system
        if request.stop:
            body["stop_sequences"] = request.stop
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters or {"type": "object", "properties": {}},
                }
                for tool in request.tools
            ]

        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self._api_version,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key

        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/messages", json=body, headers=headers
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
        blocks = data.get("content") or []
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        arguments=dict(block.get("input") or {}),
                    )
                )
        raw_usage = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens") or 0),
            output_tokens=int(raw_usage.get("output_tokens") or 0),
            cached_input_tokens=int(raw_usage.get("cache_read_input_tokens") or 0),
            estimated=False,
        )
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=calls,
            usage=usage,
            model=str(data.get("model") or self.model),
            provider=self.name,
            latency_s=elapsed,
            finish_reason=str(data.get("stop_reason") or "stop"),
            raw=data,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
