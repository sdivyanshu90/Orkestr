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
    request_key: str = ""
    """The identity of the request, independent of arrival order.

    A recorded transcript that is matched purely by arrival order is only
    replayable when one worker runs at a time.  With two workers the order
    in which requests reach the provider is not deterministic, so the same
    recording would hand task B the answer the model produced for task A.
    The key is ``<task_id>:<ordinal within that task>``: a task's calls
    are sequential, so the ordinal is deterministic even when tasks run
    concurrently.
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "request": self.request,
            "response": self.response,
            "artifact_id": self.artifact_id,
            "request_key": self.request_key,
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


def request_key(request: ModelRequest, counters: dict[str, int]) -> str:
    """The identity of one request within a recording.

    ``task_id`` separates concurrent tasks; the per-task ordinal separates
    the successive calls a single task makes (propose, observe a tool
    result, answer).  Neither depends on which worker happened to reach
    the provider first, which is what makes a concurrent recording
    replayable.
    """
    task_id = str(request.metadata.get("task_id") or "")
    ordinal = counters.get(task_id, 0)
    counters[task_id] = ordinal + 1
    return f"{task_id}:{ordinal}"


@dataclass
class RecordingProvider:
    """Wraps a provider and records every exchange."""

    inner: ModelProvider
    store: ArtifactStore
    run_id: str
    exchanges: list[Exchange] = field(default_factory=list)
    record_artifacts: bool = True
    _counters: dict[str, int] = field(default_factory=dict)

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
        key = request_key(request, self._counters)
        response = await self.inner.complete(request)
        index = len(self.exchanges)
        exchange = Exchange(
            index=index,
            run_id=self.run_id,
            task_id=str(request.metadata.get("task_id") or "") or None,
            request_key=key,
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

    It makes **no** network calls.  A request is matched to its recorded
    exchange by **request identity** (task and per-task ordinal), not by
    arrival order, because a replay of a concurrent run does not deliver
    requests in the recorded order.  When the recording predates request
    keys, the provider falls back to serving the next unserved exchange in
    order and records that it did so.

    If no exchange fits, the provider refuses rather than improvising: an
    improvised answer would turn a replay into an evaluation.
    """

    name = "recorded"

    def __init__(self, exchanges: list[Exchange], model: str = "recorded") -> None:
        self._exchanges = list(exchanges)
        self._cursor = 0
        self._counters: dict[str, int] = {}
        self._by_key: dict[str, Exchange] = {
            e.request_key: e for e in self._exchanges if e.request_key
        }
        self._served: set[int] = set()
        self.ordered_fallbacks = 0
        self.model = model

    @property
    def calls_served(self) -> int:
        return len(self._served)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        from ..errors import ModelError

        key = request_key(request, self._counters)
        exchange = self._by_key.get(key)
        if exchange is None or exchange.index in self._served:
            # A recording that predates request keys: serve the next
            # unserved exchange in order, and record that the match was
            # positional rather than logical.
            exchange = next(
                (e for e in self._exchanges if e.index not in self._served),
                None,
            )
            if exchange is not None:
                self.ordered_fallbacks += 1
        if exchange is None or exchange.index in self._served:
            raise ModelError(
                "the recorded transcript is exhausted; replay cannot continue",
                recorded=len(self._exchanges),
                served=len(self._served),
                request_key=key,
            )
        self._served.add(exchange.index)
        self._cursor = len(self._served)
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
                request_key=str(data.get("request_key") or ""),
            )
        )
    exchanges.sort(key=lambda e: e.index)
    return exchanges
