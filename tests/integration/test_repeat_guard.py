"""Tests for the agent loop's repeat guard.

Small models frequently re-issue a tool call they have already made, because
they do not recognise that the work is finished.  Executing the repeat would
be a no-op at best and a corruption at worst; burning the whole iteration
budget on it wastes time and tokens.

The guard has three properties, all asserted here:

* a repeated call is **not executed again**;
* the model is told the action was already applied;
* after a bounded number of repeats the attempt ends *successfully*, because
  the work it asked for is already done.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.agents.base import AgentLoop
from agent_squad.config import RunConfig
from agent_squad.models.task import Role
from agent_squad.providers.base import ModelRequest, ModelResponse, ToolCall
from agent_squad.providers.mock import MockProvider
from agent_squad.security.capabilities import capabilities_for_role
from agent_squad.tools.base import ToolContext, ToolRegistry
from agent_squad.tools.fs import WriteFileTool


class RepeatingProvider(MockProvider):
    """Always asks for the same write, like a small model that cannot stop."""

    def __init__(self, times: int = 10) -> None:
        super().__init__()
        self.times = times
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls > self.times:
            return ModelResponse(text="all done", provider="rep", model="r")
        return ModelResponse(
            text="writing the file",
            tool_calls=[
                ToolCall(
                    name="write_file",
                    arguments={"path": "hello.py", "content": "print('hi')\n"},
                )
            ],
            provider="rep",
            model="r",
        )


@pytest.fixture()
def context(tmp_path: Path) -> ToolContext:
    workdir = tmp_path / "wt"
    workdir.mkdir()
    return ToolContext(
        run_id="r",
        task_id="t",
        actor="implementer:t",
        workdir=workdir,
        capabilities=capabilities_for_role(Role.IMPLEMENTER),
        sandbox=RunConfig(run_id="r", repo_path=str(tmp_path)).sandbox,
        scope_root=workdir,
    )


@pytest.mark.integration
async def test_a_repeated_call_is_not_executed_again(context: ToolContext) -> None:
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    provider = RepeatingProvider(times=6)
    loop = AgentLoop(
        provider=provider,
        tools=registry,
        config=RunConfig(run_id="r", repo_path=str(context.workdir)),
        run_id="r",
        task_id="t",
        actor="implementer:t",
        max_iterations=12,
    )
    result = await loop.run(
        prompt="create hello.py", tool_context=context, capabilities=context.capabilities
    )

    # The attempt succeeded rather than exhausting the iteration budget.
    assert result.ok is True
    assert "repeating the same action" in result.summary
    # The file was written exactly once.
    assert (context.workdir / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"
    # The loop stopped early rather than running all twelve iterations.
    assert provider.calls < 12
    assert result.model_calls < 12


@pytest.mark.integration
async def test_a_repeat_is_reported_to_the_model(context: ToolContext) -> None:
    """The model is told what happened rather than silently ignored."""
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    provider = RepeatingProvider(times=3)
    loop = AgentLoop(
        provider=provider,
        tools=registry,
        config=RunConfig(run_id="r", repo_path=str(context.workdir)),
        run_id="r",
        task_id="t",
        actor="implementer:t",
    )
    result = await loop.run(
        prompt="create hello.py", tool_context=context, capabilities=context.capabilities
    )
    assert result.ok is True
    assert result.tool_calls.count("write_file") >= 2


@pytest.mark.integration
async def test_a_model_that_does_not_repeat_is_unaffected(context: ToolContext) -> None:
    class WellBehaved(MockProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    text="writing",
                    tool_calls=[
                        ToolCall(
                            name="write_file",
                            arguments={"path": "a.py", "content": "x = 1\n"},
                        )
                    ],
                    provider="wb",
                    model="w",
                )
            return ModelResponse(text="done", provider="wb", model="w")

    registry = ToolRegistry()
    registry.register(WriteFileTool())
    loop = AgentLoop(
        provider=WellBehaved(),
        tools=registry,
        config=RunConfig(run_id="r", repo_path=str(context.workdir)),
        run_id="r",
        task_id="t",
        actor="implementer:t",
    )
    result = await loop.run(
        prompt="create a.py", tool_context=context, capabilities=context.capabilities
    )
    assert result.ok is True
    assert "repeating" not in result.summary
    assert result.model_calls == 2
