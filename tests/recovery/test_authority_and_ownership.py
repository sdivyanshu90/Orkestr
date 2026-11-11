"""R2: authority binding, ownership and durable deduplication.

Three properties:

* an **approval is bound to one action**.  A grant for ``git push`` does not
  authorise a different command, and it does not survive a change to the
  request it was granted against.
* **a run has exactly one owner**.  Two processes must not drive the same
  run, and a stale owner must be fenced out.
* **message deduplication survives a restart**.  The store, not the
  process's memory, is the authority on whether a message was delivered.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent_squad.agents.messaging import MessageBus
from agent_squad.config import ApprovalConfig
from agent_squad.errors import StaleAttemptError, ValidationError
from agent_squad.models.message import MessageKind, make_message
from agent_squad.security.approval import ApprovalBinding, ApprovalGrant
from agent_squad.store.db import Database
from agent_squad.store.events import EventLog
from agent_squad.store.state import StateStore

# ------------------------------------------------------------ approval binding


def test_a_binding_identifies_exactly_one_action() -> None:
    a = ApprovalBinding(action="git.push", arguments=("git", "push", "main"))
    b = ApprovalBinding(action="git.push", arguments=("git", "push", "other"))
    assert a.binding_hash != b.binding_hash


def test_a_binding_covers_the_workspace_and_the_request() -> None:
    base = ApprovalBinding(action="rm", arguments=("x",), workspace="/wt/1")
    other_workspace = ApprovalBinding(action="rm", arguments=("x",), workspace="/wt/2")
    other_request = ApprovalBinding(
        action="rm", arguments=("x",), workspace="/wt/1", contract_hash="deadbeef"
    )
    assert base.binding_hash != other_workspace.binding_hash
    assert base.binding_hash != other_request.binding_hash


def test_a_grant_authorises_only_the_bound_action() -> None:
    granted = ApprovalBinding(action="git.push", arguments=("git", "push", "main"))
    grant = ApprovalGrant(approval_id="a", binding=granted, approved=True)

    allowed, _ = grant.authorises(granted)
    assert allowed

    different_command = ApprovalBinding(
        action="git.push", arguments=("git", "push", "other-branch")
    )
    allowed, reason = grant.authorises(different_command)
    assert not allowed
    assert "different action" in reason

    different_action = ApprovalBinding(action="git.commit", arguments=("git", "push", "main"))
    allowed, _ = grant.authorises(different_action)
    assert not allowed


def test_a_denied_grant_authorises_nothing() -> None:
    binding = ApprovalBinding(action="git.push")
    grant = ApprovalGrant(approval_id="a", binding=binding, approved=False)
    allowed, reason = grant.authorises(binding)
    assert not allowed
    assert "not approved" in reason


def test_an_expired_grant_authorises_nothing() -> None:
    binding = ApprovalBinding(action="git.push")
    grant = ApprovalGrant(
        approval_id="a", binding=binding, approved=True, expires_at=time.time() - 1
    )
    allowed, reason = grant.authorises(binding)
    assert not allowed
    assert "expired" in reason


def test_the_policy_engine_refuses_a_mismatched_grant() -> None:
    from agent_squad.models.task import Role
    from agent_squad.security.capabilities import capabilities_for_role
    from agent_squad.security.policy import Decision, PolicyEngine

    engine = PolicyEngine(approval=ApprovalConfig())
    caps = capabilities_for_role(Role.IMPLEMENTER)
    granted = ApprovalBinding(
        action="git.push", arguments=("git", "push", "main"), workspace="/wt/1"
    )
    grant = ApprovalGrant(approval_id="a", binding=granted, approved=True)

    # The agent asks for something else.
    requested = ApprovalBinding(
        action="git.push", arguments=("git", "push", "other"), workspace="/wt/1"
    )
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=caps,
        tool="push_changes",
        arguments={"remote": "origin", "branch": "other"},
        grant=grant,
        binding=requested,
    )
    assert decision.decision is Decision.DENY
    assert "different action" in decision.reason

    # And the action it *was* granted for is allowed.
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=caps,
        tool="push_changes",
        arguments={"remote": "origin", "branch": "main"},
        grant=grant,
        binding=granted,
    )
    assert decision.decision is Decision.ALLOW


# ------------------------------------------------------------- run ownership


def test_only_one_process_may_own_a_run(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    state.create_run("r1")

    fence = state.acquire_run_ownership("r1", owner_id="process-a", owner_pid=1)
    assert fence == 1

    with pytest.raises(ValidationError):
        state.acquire_run_ownership("r1", owner_id="process-b", owner_pid=2)

    record = state.run_ownership("r1")
    assert record["owner_id"] == "process-a"
    db.close()


def test_a_released_run_can_be_taken_over(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    state.create_run("r1")

    fence = state.acquire_run_ownership("r1", owner_id="process-a")
    assert state.release_run_ownership("r1", owner_id="process-a", fence=fence)

    fence_b = state.acquire_run_ownership("r1", owner_id="process-b")
    assert fence_b == fence + 1, "the fence must advance so the old owner is stale"
    db.close()


def test_a_stale_owner_is_fenced_out(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    state.create_run("r1")

    fence_a = state.acquire_run_ownership("r1", owner_id="process-a")
    state.release_run_ownership("r1", owner_id="process-a", fence=fence_a)
    fence_b = state.acquire_run_ownership("r1", owner_id="process-b")

    # The old owner comes back and tries to act.
    with pytest.raises(StaleAttemptError):
        state.assert_run_ownership("r1", owner_id="process-a", fence=fence_a)
    # The new owner may.
    state.assert_run_ownership("r1", owner_id="process-b", fence=fence_b)
    db.close()


def test_a_heartbeat_fails_for_a_stale_fence(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    state.create_run("r1")
    fence = state.acquire_run_ownership("r1", owner_id="process-a")
    assert state.heartbeat_run_ownership("r1", owner_id="process-a", fence=fence)
    assert not state.heartbeat_run_ownership("r1", owner_id="process-a", fence=fence + 1)
    db.close()


def test_two_processes_cannot_both_own_a_run(tmp_path: Path) -> None:
    """The real race: two processes, one run, one winner."""
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    state.create_run("r1")

    winners: list[str] = []
    barrier = threading.Barrier(2)

    def contend(owner: str) -> None:
        barrier.wait()
        try:
            state.acquire_run_ownership("r1", owner_id=owner)
        except ValidationError:
            return
        winners.append(owner)

    threads = [threading.Thread(target=contend, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(winners) == 1, f"both processes acquired ownership: {winners}"
    db.close()


# -------------------------------------------------------- durable deduplication


@pytest.mark.integration
async def test_a_duplicate_message_is_refused_after_a_restart(tmp_path: Path) -> None:
    """Deduplication must survive the process that recorded the message."""
    db_path = tmp_path / "state.db"

    db = Database(db_path)
    state = StateStore(db, EventLog(db))
    bus = MessageBus(
        state=state,
        log=EventLog(db),
        known_recipients=frozenset({"a", "b"}),
    )
    message = make_message(
        MessageKind.PROGRESS,
        run_id="r1",
        sender="a",
        recipient="b",
        task_id="t",
        percent=10.0,
    )
    await bus.publish(message)
    db.close()

    # A brand new process, with no memory of the first.
    db2 = Database(db_path)
    state2 = StateStore(db2, EventLog(db2))
    bus2 = MessageBus(
        state=state2,
        log=EventLog(db2),
        known_recipients=frozenset({"a", "b"}),
    )
    with pytest.raises(ValidationError):
        await bus2.publish(message)
    db2.close()


@pytest.mark.integration
async def test_the_version_survives_a_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    db = Database(db_path)
    state = StateStore(db, EventLog(db))
    bus = MessageBus(
        state=state, log=EventLog(db), known_recipients=frozenset({"a", "b"})
    )
    first = make_message(
        MessageKind.PROGRESS,
        run_id="r1",
        sender="a",
        recipient="b",
        task_id="t",
        percent=10.0,
    )
    published_first = await bus.publish(first)
    db.close()

    db2 = Database(db_path)
    state2 = StateStore(db2, EventLog(db2))
    bus2 = MessageBus(
        state=state2, log=EventLog(db2), known_recipients=frozenset({"a", "b"})
    )
    second = make_message(
        MessageKind.PROGRESS,
        run_id="r1",
        sender="a",
        recipient="b",
        task_id="t",
        percent=20.0,
    )
    published_second = await bus2.publish(second)
    # The version continued across the restart, rather than restarting at 1.
    assert published_first.version == 1
    assert published_second.version == 2
    db2.close()


@pytest.mark.integration
async def test_a_message_with_an_explicit_stale_version_is_refused(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    bus = MessageBus(
        state=state, log=EventLog(db), known_recipients=frozenset({"a", "b"})
    )
    await bus.publish(
        make_message(
            MessageKind.PROGRESS,
            run_id="r1",
            sender="a",
            recipient="b",
            task_id="t",
            percent=10.0,
        )
    )
    stale = make_message(
        MessageKind.PROGRESS,
        run_id="r1",
        sender="a",
        recipient="b",
        task_id="t",
        percent=20.0,
    )
    stale = stale.model_copy(update={"version": 1})
    with pytest.raises(ValidationError):
        await bus.publish(stale)
    db.close()
