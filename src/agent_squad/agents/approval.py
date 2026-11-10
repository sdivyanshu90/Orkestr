"""The human-in-the-loop approval broker.

When the policy engine decides that an action requires human authorisation,
the worker does not proceed and does not fail silently: it raises a request,
waits for a decision, and either continues or reports that the action was
refused.

The broker is deliberately simple and blocking.  A decision is looked up in
the durable store, so the operator can answer from the CLI, the API or the
dashboard while the run is in flight -- the process does not need to be
co-located with the person making the decision.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import ApprovalConfig
from ..models.message import MessageKind, make_message
from ..store.events import EventLog, EventType
from ..store.state import StateStore


@dataclass
class ApprovalBroker:
    """Creates approval requests and waits for a human decision."""

    state: StateStore
    log: EventLog
    config: ApprovalConfig
    run_id: str
    bus: Any = None
    poll_interval_s: float = 0.2
    auto_approve: bool = False
    requested: list[str] = field(default_factory=list)
    _message_failures: int = 0
    _requesters: dict[str, str] = field(default_factory=dict)

    async def request(
        self,
        *,
        action: str,
        rationale: str,
        arguments: list[str] | None = None,
        task_id: str | None = None,
        risk: str = "medium",
        sender: str = "supervisor",
    ) -> bool:
        """Request approval and wait for the decision.  Returns the decision.

        ``sender`` is the identity of the agent asking for the approval.  It
        must be a registered actor, or the bus will refuse the message --
        which is why the worker registers its identity before it starts.
        """
        if self.auto_approve:
            self.log.append(
                EventType.HUMAN_APPROVAL,
                run_id=self.run_id,
                task_id=task_id,
                payload={
                    "action": action,
                    "status": "auto_approved",
                    "rationale": rationale,
                },
            )
            return True

        approval_id = self.state.create_approval(
            self.run_id,
            action=action,
            rationale=rationale,
            arguments=arguments or [],
            risk=risk,
            task_id=task_id,
        )
        self.requested.append(approval_id)
        # Remember who asked, so the decision can be addressed back to them.
        self._requesters[approval_id] = sender

        if self.bus is not None:
            register = getattr(self.bus, "register_actor", None)
            if register is not None:
                register(sender)
            try:
                await self.bus.publish(
                    make_message(
                        MessageKind.APPROVAL_REQUEST,
                        run_id=self.run_id,
                        sender=sender,
                        recipient="supervisor",
                        task_id=task_id,
                        action=action,
                        rationale=rationale,
                        arguments=tuple(arguments or []),
                        risk=risk,
                    )
                )
            except Exception:
                # A message failure must not prevent the approval request from
                # being recorded; the durable record is the authority.
                self._message_failures += 1

        deadline = time.monotonic() + self.config.approval_timeout_s
        while time.monotonic() < deadline:
            record = self.state.get_approval(approval_id)
            if record is not None and record["status"] in ("approved", "denied"):
                await self._publish_decision(record, task_id)
                return record["status"] == "approved"
            await asyncio.sleep(self.poll_interval_s)

        self.log.append(
            EventType.HUMAN_APPROVAL,
            run_id=self.run_id,
            task_id=task_id,
            payload={
                "approval_id": approval_id,
                "action": action,
                "status": "timed_out",
            },
        )
        return False

    async def _publish_decision(self, record: dict, task_id: str | None) -> None:
        """Publish the human's decision as a typed message.

        The decision is already durable in the ``approvals`` table; the
        message makes it visible to the protocol, so the task's inbox shows
        what was authorised and by whom.  It is addressed back to the agent
        that asked for the approval.
        """
        if self.bus is None:
            return
        approval_id = str(record.get("approval_id") or "")
        recipient = self._requesters.get(approval_id) or "supervisor"
        try:
            await self.bus.publish(
                make_message(
                    MessageKind.APPROVAL_DECISION,
                    run_id=self.run_id,
                    sender="supervisor",
                    recipient=recipient,
                    task_id=task_id,
                    action=record.get("action", ""),
                    approved=record.get("status") == "approved",
                    decided_by=record.get("decided_by") or "approver",
                    reason=record.get("reason") or "",
                )
            )
        except Exception:
            # The decision is already recorded; a message failure must not
            # undo or dispute it.
            self._message_failures += 1
