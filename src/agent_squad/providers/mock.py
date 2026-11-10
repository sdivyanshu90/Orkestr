"""Deterministic mock provider.

The mock provider exists so that the whole system -- planning, scheduling,
worktree isolation, patching, integration, verification, benchmarking -- can
be exercised end to end without any paid API access and with *identical*
results on every run.

It is not a stub that returns canned text.  It understands a structured
**task brief** that the worker places in its prompt, and it emits genuine tool
calls (``write_file``, ``run_command``, ``git_diff``) that a real worker would
execute.  That means the mock path produces real file changes, real diffs,
real test runs and real conflicts: the parts of the system most likely to be
wrong are actually exercised.

Failure injection is a first-class feature here rather than an accident:
``failures=`` lets the chaos harness make the provider raise on the Nth
call, and ``unavailable=True`` simulates an endpoint that is down.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import ModelError, TransientModelError
from .base import (
    BaseProvider,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ToolCall,
    Usage,
    estimate_tokens,
)

BRIEF_START = "<<<SQUAD_TASK_BRIEF>>>"
BRIEF_END = "<<<END_SQUAD_TASK_BRIEF>>>"


def extract_brief(text: str) -> dict[str, Any] | None:
    """Pull the structured task brief out of a prompt, if present."""
    if BRIEF_START not in text or BRIEF_END not in text:
        return None
    start = text.index(BRIEF_START) + len(BRIEF_START)
    end = text.index(BRIEF_END)
    blob = text[start:end].strip()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class MockProvider(BaseProvider):
    """A fully deterministic provider suitable for tests and demos."""

    name = "mock"
    capabilities = ProviderCapabilities(
        supports_tools=True,
        supports_system_prompt=True,
        supports_seed=True,
        supports_parallel_tool_calls=False,
        supports_json_schema=True,
    )

    def __init__(
        self,
        model: str = "mock-deterministic-v1",
        *,
        failures: int = 0,
        failure_error: type[Exception] = TransientModelError,
        unavailable: bool = False,
        latency_s: float = 0.0,
        scripted: list[ModelResponse | Exception] | None = None,
    ) -> None:
        super().__init__(model=model)
        self._remaining_failures = failures
        self._failure_error = failure_error
        self._unavailable = unavailable
        self._latency_s = latency_s
        self._scripted = list(scripted or [])
        self._calls = 0
        self.requests: list[ModelRequest] = []

    # ------------------------------------------------------------- controls

    @property
    def call_count(self) -> int:
        return self._calls

    def set_failures(self, count: int) -> None:
        self._remaining_failures = count

    def set_unavailable(self, unavailable: bool) -> None:
        self._unavailable = unavailable

    # -------------------------------------------------------------- request

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._calls += 1
        self.requests.append(request)
        if self._latency_s:
            await _sleep(self._latency_s)
        if self._unavailable:
            raise ModelError(
                "mock provider is unavailable", provider=self.name, model=self.model
            )
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self._failure_error(
                "mock provider injected failure", provider=self.name, call=self._calls
            )
        if self._scripted:
            item = self._scripted.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self._synthesise(request)

    # ------------------------------------------------------------ synthesis

    def _synthesise(self, request: ModelRequest) -> ModelResponse:
        brief = self._find_brief(request)
        role = (brief or {}).get("role", "implementer")
        if role == "supervisor":
            return self._supervisor_response(request, brief or {})
        if role == "reviewer":
            return self._reviewer_response(request, brief or {})
        return self._worker_response(request, brief or {})

    @staticmethod
    def _find_brief(request: ModelRequest) -> dict[str, Any] | None:
        for message in reversed(request.messages):
            if message.role not in ("user", "system"):
                continue
            brief = extract_brief(message.content)
            if brief is not None:
                return brief
        return None

    def _worker_response(self, request: ModelRequest, brief: dict[str, Any]) -> ModelResponse:
        calls: list[ToolCall] = []
        # The mock behaves like a well-mannered agent: it proposes its work
        # once, and once the results are back it reports completion instead of
        # proposing the same edits again.
        already_ran = any(m.role == "tool" for m in request.messages)
        if not already_ran:
            for edit in brief.get("edits", []) or []:
                if not isinstance(edit, dict):
                    continue
                path = edit.get("path")
                if not path:
                    continue
                args: dict[str, Any] = {"path": path}
                mode = edit.get("mode", "write")
                if mode == "append":
                    args["content"] = str(edit.get("content", ""))
                    args["mode"] = "append"
                else:
                    args["content"] = str(edit.get("content", ""))
                calls.append(ToolCall(name="write_file", arguments=args))
            for command in brief.get("commands", []) or []:
                if isinstance(command, (list, tuple)) and command:
                    calls.append(
                        ToolCall(name="run_command", arguments={"command": list(command)})
                    )
        text = self._summary_text(brief, calls)
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=self._usage(request, text),
            model=self.model,
            provider=self.name,
            latency_s=self._latency_s,
            finish_reason="tool_calls" if calls else "stop",
        )

    def _reviewer_response(self, request: ModelRequest, brief: dict[str, Any]) -> ModelResponse:
        calls: list[ToolCall] = []
        if not any(m.role == "tool" for m in request.messages):
            calls = [
                ToolCall(name="run_command", arguments={"command": ["git", "diff", "--stat"]}),
                ToolCall(name="run_command", arguments={"command": ["git", "status", "--porcelain"]}),
            ]
        text = (
            f"Review of task {brief.get('task_id', 'unknown')} completed. "
            "Findings are advisory; independent validation decides acceptance."
        )
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=self._usage(request, text),
            model=self.model,
            provider=self.name,
            latency_s=self._latency_s,
            finish_reason="tool_calls",
        )

    def _supervisor_response(self, request: ModelRequest, brief: dict[str, Any]) -> ModelResponse:
        plan = brief.get("plan") or {"tasks": []}
        text = json.dumps({"plan": plan}, indent=2, sort_keys=True)
        return ModelResponse(
            text=text,
            tool_calls=[],
            usage=self._usage(request, text),
            model=self.model,
            provider=self.name,
            latency_s=self._latency_s,
            finish_reason="stop",
        )

    @staticmethod
    def _summary_text(brief: dict[str, Any], calls: list[ToolCall]) -> str:
        files = [
            c.arguments.get("path")
            for c in calls
            if c.name == "write_file" and c.arguments.get("path")
        ]
        parts = [f"Task {brief.get('task_id', 'unknown')} work performed."]
        if files:
            parts.append("Files written: " + ", ".join(str(f) for f in files))
        else:
            parts.append("No files were modified by this attempt.")
        return " ".join(parts)

    @staticmethod
    def _usage(request: ModelRequest, text: str) -> Usage:
        prompt = sum(estimate_tokens(m.content) for m in request.messages)
        return Usage(
            input_tokens=prompt,
            output_tokens=estimate_tokens(text),
            estimated=True,
        )


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
