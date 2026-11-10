"""Recording of model exchanges for replay and audit.

Every model call is recorded as an *exchange*: the request that was sent
(normalised) and the response that came back, together with the usage the
provider reported.  Exchanges are stored as artifacts, so they are
content-addressed, immutable and linked to the run that produced them.

The recording exists so that a run can be replayed -- its control-plane
decisions reconstructed -- without spending a single token on a model call.
It is emphatically *not* a way to re-run the work: replaying a recorded
exchange reproduces what the model said, not what the model would say again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..artifacts.store import ArtifactStore
from ..models.artifact import ArtifactKind, Provenance
from ..providers.base import (
    ChatMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)


@dataclass
class Exchange:
    """One recorded model call."""

    index: int
    run_id: str
    task_id: str | None
    request: dict[str, Any]
    response: dict[str, Any]
    artifact_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "request": self.request,
            "response": self.response,
            "artifact_id": self.artifact_id,
        }


def _encode_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [
        {
            "role": m.role,
            "content": m.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in m.tool_calls
            ],
            "tool_call_id": m.tool_call_id,
        }
        for m in messages
    ]


def _decode_messages(payload: list[dict[str, Any]]) -> list[ChatMessage]:
    return [
        ChatMessage(
            role=m.get("role", "user"),
            content=m.get("content", ""),
            tool_calls=tuple(
                ToolCall(id=c.get("id", ""), name=c.get("name", ""), arguments=c.get("arguments", {}))
                for c in m.get("tool_calls", [])
            ),
            tool_call_id=m.get("tool_call_id"),
        )
        for m in payload
    ]


@dataclass
class RecordingProvider:
    """Wraps a provider and records every exchange."""

    inner: ModelProvider
    store: ArtifactStore
    run_id: str
    exchanges: list[Exchange] = field(default_factory=list)
    record_artifacts: bool = True

    @property
    def name(self) -> str:
        """The name of the provider that actually served the request.

        The recorder is a transparent wrapper: it must not change how the
        provider identifies itself in traces and logs.
        """
        return getattr(self.inner, "name", "recording")

    @property
    def model(self) -> str:
        return self.inner.model

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.inner.complete(request)
        index = len(self.exchanges)
        exchange = Exchange(
            index=index,
            run_id=self.run_id,
            task_id=str(request.metadata.get("task_id") or "") or None,
            request={
                "messages": _encode_messages(request.messages),
                "tools": [t.model_dump(mode="json") for t in request.tools],
                "max_output_tokens": request.max_output_tokens,
                "temperature": request.temperature,
                "seed": request.seed,
                "metadata": request.metadata,
            },
            response={
                "text": response.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in response.tool_calls
                ],
                "usage": response.usage.model_dump(mode="json"),
                "model": response.model,
                "provider": response.provider,
                "finish_reason": response.finish_reason,
            },
        )
        if self.record_artifacts:
            provenance = Provenance(
                producer="recorder",
                run_id=self.run_id,
                task_id=exchange.task_id,
                model=response.model,
                provider=response.provider,
            )
            artifact = self.store.put_json(
                exchange.to_dict(),
                kind=ArtifactKind.CONTEXT,
                provenance=provenance,
                metadata={"kind": "model_exchange", "index": index},
            )
            exchange.artifact_id = artifact.id
        self.exchanges.append(exchange)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class RecordedProvider:
    """A provider that serves previously recorded responses.

    It makes **no** network calls.  Each request is matched against the next
    recorded exchange; if the recorded exchange does not fit, the provider
    refuses rather than improvising, because an improvised answer would turn
    a replay into an evaluation.
    """

    name = "recorded"

    def __init__(self, exchanges: list[Exchange], model: str = "recorded") -> None:
        self._exchanges = list(exchanges)
        self._cursor = 0
        self.model = model

    @property
    def calls_served(self) -> int:
        return self._cursor

    async def complete(self, request: ModelRequest) -> ModelResponse:
        from ..errors import ModelError

        if self._cursor >= len(self._exchanges):
            raise ModelError(
                "the recorded transcript is exhausted; replay cannot continue",
                recorded=len(self._exchanges),
                served=self._cursor,
            )
        exchange = self._exchanges[self._cursor]
        self._cursor += 1
        payload = exchange.response
        usage = Usage.model_validate(payload.get("usage") or {})
        return ModelResponse(
            text=payload.get("text", ""),
            tool_calls=[
                ToolCall(
                    id=c.get("id", ""),
                    name=c.get("name", ""),
                    arguments=c.get("arguments", {}),
                )
                for c in payload.get("tool_calls", [])
            ],
            usage=usage,
            model=payload.get("model", self.model),
            provider=payload.get("provider", "recorded"),
            finish_reason=payload.get("finish_reason", "stop"),
            raw={"replayed": True, "exchange_index": exchange.index},
        )

    async def aclose(self) -> None:
        return None


def load_exchanges(store: ArtifactStore, run_id: str) -> list[Exchange]:
    """Load the recorded exchanges of a run from the artifact store."""
    exchanges: list[Exchange] = []
    for artifact in store.list_artifacts():
        if artifact.metadata.get("kind") != "model_exchange":
            continue
        if artifact.provenance.run_id != run_id:
            continue
        try:
            data = json.loads(store.read_text(artifact))
        except Exception:
            continue
        exchanges.append(
            Exchange(
                index=int(data.get("index", len(exchanges))),
                run_id=data.get("run_id", run_id),
                task_id=data.get("task_id"),
                request=data.get("request", {}),
                response=data.get("response", {}),
                artifact_id=artifact.id,
            )
        )
    exchanges.sort(key=lambda e: e.index)
    return exchanges
