"""Provider-agnostic model interface.

Every provider is normalised into the same small vocabulary:

* :class:`ChatMessage` -- system / user / assistant / tool turns;
* :class:`ToolCall`   -- a structured request to invoke a registered tool;
* :class:`Usage`      -- token accounting, distinguishing *reported* usage
  from anything the provider estimated;
* :class:`ModelResponse` -- text, tool calls, usage, latency and the raw
  provider payload (kept so that provider-specific features are not lost).

Normalisation deliberately does *not* discard provider-specific fields: the
raw response is preserved on :attr:`ModelResponse.raw` and providers expose a
``capabilities`` descriptor so the orchestrator can use provider-specific
features without hard-coding them into the runtime.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ModelError
from ..ids import new_id

MessageRole = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """A model's request to invoke a tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("tc"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    created_at: _dt.datetime | None = None


class ToolSpec(BaseModel):
    """A tool description handed to the provider."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    """Token accounting.

    ``reported`` distinguishes numbers the provider actually returned from
    numbers we estimated locally.  Estimates are useful for budgeting but
    must never be reported as measured usage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    estimated: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            estimated=self.estimated and other.estimated,
        )


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    messages: list[ChatMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_output_tokens: int = Field(default=4096, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int | None = None
    stop: list[str] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    latency_s: float = 0.0
    finish_reason: str = "stop"
    raw: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supports_tools: bool = True
    supports_system_prompt: bool = True
    supports_seed: bool = False
    supports_parallel_tool_calls: bool = False
    supports_json_schema: bool = False


@runtime_checkable
class ModelProvider(Protocol):
    """The contract every provider implements."""

    name: str
    model: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one completion.  Must raise :class:`ModelError` on failure."""
        ...

    async def aclose(self) -> None:
        """Release any resources held by the provider."""
        ...


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate.

    The estimate is intentionally crude (about four characters per token) and
    is always labelled as an estimate.  It exists so that the mock provider
    and offline paths can participate in budgeting without pretending to be
    exact.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class BaseProvider:
    """Shared plumbing for concrete providers."""

    name = "base"
    capabilities = ProviderCapabilities()

    def __init__(self, model: str = "default") -> None:
        self.model = model

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        raise ModelError("provider does not implement complete()")

    async def aclose(self) -> None:
        return None
