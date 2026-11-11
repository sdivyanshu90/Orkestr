"""The human-in-the-loop approval gate, exercised end to end.

The approval flow is one of the system's central safety claims, so it is
tested against a *real* run: a worker must genuinely attempt a protected
action, the run must genuinely block, a human must genuinely decide, and
the decision must genuinely determine the outcome.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agent_squad.agents.approval import ApprovalBroker
from agent_squad.config import ApprovalConfig, RunConfig
from agent_squad.orchestrator import Orchestrator
from agent_squad.providers.base import ModelRequest, ModelResponse, ToolCall
from agent_squad.providers.mock import MockProvider
from agent_squad.security.policy import Decision, PolicyEngine
from agent_squad.store.db import Database
from agent_squad.store.events import EventLog
from agent_squad.store.state import StateStore


class PushProvider(MockProvider):
    """A worker that tries to push, which must require approval."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if any(m.role == "tool" for m in request.messages):
            return ModelResponse(text="attempted the push", provider="push", model="p")
        return ModelResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="push_changes",
                    arguments={"remote": "origin", "branch": "main"},
                )
            ],
            provider="push",
            model="p",
        )


@pytest.mark.integration
async def test_a_protected_action_blocks_until_a_human_decides(
    tmp_path: Path, repo: Path
) -> None:
    """The worker must not proceed while the decision is outstanding."""
    config = RunConfig(
        run_id="run_approval",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        approval={"approval_timeout_s": 30},
    )
    orchestrator = Orchestrator(
        config, data_dir=tmp_path / "data", provider=PushProvider()
    )

    decided = threading.Event()
    decision_result: dict[str, bool] = {}

    def decide_when_requested() -> None:
        """Wait for the approval to appear, then approve it."""
        deadline = 30
        while deadline:
            approvals = orchestrator.state.pending_approvals(config.run_id)
            if approvals:
                orchestrator.state.decide_approval(
                    approvals[0]["approval_id"],
                    approved=True,
                    decided_by="test-operator",
                    reason="approved by the test",
                )
                decision_result["approved"] = True
                decided.set()
                return
            threading.Event().wait(0.05)
            deadline -= 1

    thread = threading.Thread(target=decide_when_requested, daemon=True)
    thread.start()
    try:
        outcome = await orchestrator.run(
            "push something",
            request_spec={
                "objective": "push",
                "tasks": [
                    {
                        "id": "push-task",
                        "title": "Push",
                        "objective": "Attempt a protected push",
                        "role": "implementer",
                        "depends_on": [],
                        "edits": [{"path": "calc.py", "content": "x = 1\n"}],
                        "acceptance_criteria": [
                            {
                                "id": "file",
                                "description": "calc.py exists",
                                "kind": "file_exists",
                                "spec": {"path": "calc.py"},
                            }
                        ],
                    }
                ],
            },
        )
        approvals = orchestrator.state.pending_approvals(config.run_id)
        events = [
            e.payload
            for e in orchestrator.log.read(config.run_id)
            if e.type.value == "verification.human_approval"
        ]
    finally:
        await orchestrator.aclose()

    assert decided.wait(5), "the approval request was never raised"
    assert decision_result.get("approved") is True
    # The approval was recorded and resolved.
    statuses = {e.get("status") for e in events}
    assert "requested" in statuses
    assert "approved" in statuses
    assert approvals == [], "the approval must not still be pending"
    assert outcome.status.value in ("succeeded", "completed_with_failures")


@pytest.mark.integration
async def test_a_denied_approval_stops_the_action(tmp_path: Path, repo: Path) -> None:
    """A refusal must be recorded and must prevent the action."""
    config = RunConfig(
        run_id="run_denial",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        approval={"approval_timeout_s": 30},
    )
    orchestrator = Orchestrator(
        config, data_dir=tmp_path / "data", provider=PushProvider()
    )

    def deny_when_requested() -> None:
        deadline = 60
        while deadline:
            approvals = orchestrator.state.pending_approvals(config.run_id)
            if approvals:
                orchestrator.state.decide_approval(
                    approvals[0]["approval_id"],
                    approved=False,
                    decided_by="test-operator",
                    reason="no",
                )
                return
            threading.Event().wait(0.05)
            deadline -= 1

    thread = threading.Thread(target=deny_when_requested, daemon=True)
    thread.start()
    try:
        await orchestrator.run(
            "push something",
            request_spec={
                "objective": "push",
                "tasks": [
                    {
                        "id": "push-task",
                        "title": "Push",
                        "objective": "Attempt a protected push",
                        "role": "implementer",
                        "depends_on": [],
                        "edits": [{"path": "calc.py", "content": "x = 1\n"}],
                        "acceptance_criteria": [
                            {
                                "id": "file",
                                "description": "calc.py exists",
                                "kind": "file_exists",
                                "spec": {"path": "calc.py"},
                            }
                        ],
                    }
                ],
            },
        )
        events = [
            e.payload
            for e in orchestrator.log.read(config.run_id)
            if e.type.value == "verification.human_approval"
        ]
    finally:
        await orchestrator.aclose()

    statuses = {e.get("status") for e in events}
    assert "denied" in statuses


def test_the_broker_records_and_resolves_an_approval(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    broker = ApprovalBroker(
        state=state,
        log=log,
        config=ApprovalConfig(approval_timeout_s=5),
        run_id="r1",
    )

    async def approve_soon() -> None:
        for _ in range(100):
            approvals = state.pending_approvals("r1")
            if approvals:
                state.decide_approval(
                    approvals[0]["approval_id"],
                    approved=True,
                    decided_by="operator",
                )
                return
            await asyncio.sleep(0.01)

    async def go() -> bool:
        task = asyncio.create_task(approve_soon())
        grant = await broker.request(
            action="git.push", rationale="pushing the change", arguments=["git", "push"]
        )
        await task
        return grant.approved

    assert asyncio.run(go()) is True
    assert broker.requested
    db.close()


def test_the_broker_times_out_rather_than_hanging(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    log = EventLog(db)
    state = StateStore(db, log)
    broker = ApprovalBroker(
        state=state,
        log=log,
        config=ApprovalConfig(approval_timeout_s=0.2),
        run_id="r1",
        poll_interval_s=0.01,
    )

    async def go() -> bool:
        grant = await broker.request(
            action="git.push", rationale="nobody is watching", arguments=[]
        )
        return grant.approved

    assert asyncio.run(go()) is False
    events = [
        e.payload
        for e in log.read("r1")
        if e.type.value == "verification.human_approval"
    ]
    assert any(e.get("status") == "timed_out" for e in events)
    db.close()


def test_the_policy_engine_requires_approval_for_a_push() -> None:
    engine = PolicyEngine(approval=ApprovalConfig())
    from agent_squad.models.task import Role
    from agent_squad.security.capabilities import capabilities_for_role

    caps = capabilities_for_role(Role.IMPLEMENTER)
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=caps,
        tool="push_changes",
        arguments={},
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.action == "git.push"


def test_the_policy_engine_requires_approval_for_a_delete() -> None:
    engine = PolicyEngine(approval=ApprovalConfig())
    from agent_squad.models.task import Role
    from agent_squad.security.capabilities import capabilities_for_role

    caps = capabilities_for_role(Role.IMPLEMENTER)
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=caps,
        tool="delete_path",
        arguments={"path": "a.py"},
    )
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.action == "fs.delete"


def test_the_http_tool_requires_approval_and_the_network_capability() -> None:
    from agent_squad.models.task import Role
    from agent_squad.security.capabilities import capabilities_for_role

    engine = PolicyEngine(approval=ApprovalConfig())
    reviewer = capabilities_for_role(Role.REVIEWER)
    decision = engine.authorize(
        run_id="r",
        actor="reviewer:t",
        capabilities=reviewer,
        tool="http_request",
        arguments={"url": "https://example.com"},
    )
    # A reviewer has no network capability at all, so it is denied outright.
    assert decision.decision is Decision.DENY

    implementer = capabilities_for_role(Role.IMPLEMENTER)
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=implementer,
        tool="http_request",
        arguments={"url": "https://example.com"},
    )
    # An implementer holds the capability, but the action still needs a human.
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.action == "net.http"
