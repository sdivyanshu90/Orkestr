"""The inter-agent message bus.

Messages are the only way agents influence each other, and they are typed,
validated and recorded.  The bus enforces the rules that keep a message from
being a back door around the control plane:

* A message must name a known recipient.  Broadcast to unknown agents is
  rejected.
* A message payload must validate against the schema for its kind.  There is
  no free-form "instructions" payload.
* A message that references a stale version of a conversation is rejected,
  so a delayed packet cannot overwrite a newer result.
* Informational messages are recorded but are *not* allowed to change task
  state.  Only state-effecting kinds are dispatched to state handlers, and
  even those go through the normal guarded transition API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationError
from ..models.message import INFORMATIONAL_KINDS, Message, MessageKind
from ..store.events import EventLog, EventType
from ..store.state import StateStore


@dataclass
class Subscription:
    recipient: str
    handler: Callable[[Message], Awaitable[None]]
    kinds: frozenset[MessageKind] = frozenset()


@dataclass
class MessageBus:
    """Validating, recording message router."""

    state: StateStore
    log: EventLog
    known_recipients: frozenset[str] = frozenset()
    _subscriptions: list[Subscription] = field(default_factory=list)
    _versions: dict[tuple[str, str], int] = field(default_factory=dict)
    _seen_ids: set[str] = field(default_factory=set)
    _actors: set[str] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------- actors

    def register_actor(self, name: str) -> None:
        """Register an agent identity that may send or receive messages.

        Worker identities are derived from the task they are executing
        (``implementer:task-7``), so they are registered when the worker
        starts rather than enumerated up front.
        """
        self._actors.add(name)

    def _is_known(self, name: str) -> bool:
        return name in self.known_recipients or name in self._actors

    # ------------------------------------------------------------- routing

    def subscribe(
        self,
        recipient: str,
        handler: Callable[[Message], Awaitable[None]],
        kinds: frozenset[MessageKind] | None = None,
    ) -> None:
        self._subscriptions.append(
            Subscription(recipient=recipient, handler=handler, kinds=kinds or frozenset())
        )

    def subscribe_all(
        self,
        handler: Callable[[Message], Awaitable[None]],
        kinds: frozenset[MessageKind] | None = None,
    ) -> None:
        """Subscribe to every message, whatever its recipient.

        The supervisor uses this to keep a complete structured view of the
        run: it issues messages as well as receiving them, and the record it
        keeps must show both.
        """
        self._subscriptions.append(
            Subscription(recipient="*", handler=handler, kinds=kinds or frozenset())
        )

    async def publish(self, message: Message) -> Message:
        """Validate, record and dispatch a message.

        The version is assigned by the bus, not by the sender.  Each
        conversation (``sender`` + ``correlation_id``) has a monotonically
        increasing version counter, so:

        * several distinct messages may be exchanged in one conversation;
        * a sender that explicitly quotes an *older* version is rejected,
          because it is a delayed packet that would overwrite a newer
          result.
        The store is the authority on both duplication and the current
        version, so a message cannot be delivered twice merely because the
        process restarted and lost its in-memory state.
        """
        async with self._lock:
            self._validate(message)
            key = (
                message.sender,
                message.correlation_id or message.task_id or message.id,
            )
            last_version = self._latest_version(message, key)
            # An explicit version is a claim to supersede that version;
            # an unversioned message simply continues the conversation.
            effective_version = (
                message.version if message.version > 0 else last_version + 1
            )
            if effective_version != message.version:
                message = message.model_copy(update={"version": effective_version})
            if not self.state.record_message_versioned(
                message, message.version, key[1]
            ):
                raise ValidationError(
                    f"message {message.id} has already been recorded",
                    message_id=message.id,
                )
            self.log.append(
                EventType.MESSAGE_SENT,
                run_id=message.run_id,
                task_id=message.task_id,
                actor=message.sender,
                payload={
                    "message_id": message.id,
                    "kind": message.kind.value,
                    "recipient": message.recipient,
                    "version": message.version,
                    "state_effecting": message.is_state_effecting,
                },
            )
            self._versions[key] = message.version

        for subscription in list(self._subscriptions):
            if subscription.recipient not in ("*", message.recipient):
                continue
            if subscription.kinds and message.kind not in subscription.kinds:
                continue
            await subscription.handler(message)
            self.log.append(
                EventType.MESSAGE_RECEIVED,
                run_id=message.run_id,
                task_id=message.task_id,
                actor=message.recipient,
                payload={"message_id": message.id, "kind": message.kind.value},
            )
        return message

    def _latest_version(self, message: Message, key: tuple[str, str]) -> int:
        """The current version of a conversation, from the durable store.

        The store is consulted so that the version survives a restart; the
        in-memory map is only a cache for conversations the store cannot
        resolve.
        """
        sender, correlation = key
        try:
            return self.state.latest_message_version(
                message.run_id, sender, correlation or None
            )
        except Exception:
            return self._versions.get(key, 0)

    # ----------------------------------------------------------- validation

    def _validate(self, message: Message) -> None:
        """Check a message before it is recorded.

        The duplicate-id and stale-version checks are advisory here; the
        *authoritative* duplicate check happens at insert time, because only
        the store survives a restart.
        """
        if message.id in self._seen_ids:
            raise ValidationError(
                f"duplicate message id {message.id}", message_id=message.id
            )
        self._seen_ids.add(message.id)

        if self.known_recipients or self._actors:
            if not self._is_known(message.recipient):
                raise ValidationError(
                    f"unknown recipient {message.recipient!r}",
                    recipient=message.recipient,
                    known=sorted(self.known_recipients | self._actors),
                )
            if not self._is_known(message.sender):
                raise ValidationError(
                    f"unknown sender {message.sender!r}", sender=message.sender
                )

        key = (message.sender, message.correlation_id or message.task_id or message.id)
        last_version = self._latest_version(message, key)
        if message.version > 0 and message.version <= last_version:
            raise ValidationError(
                f"stale message version {message.version} for {key[0]} "
                f"(latest is {last_version})",
                message_id=message.id,
            )

    # ------------------------------------------------------------- queries

    def history(
        self, run_id: str, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self.state.list_messages(run_id, task_id)

    def state_effecting_only(self, run_id: str) -> list[dict[str, Any]]:
        return [
            m
            for m in self.state.list_messages(run_id)
            if m["kind"] not in {k.value for k in INFORMATIONAL_KINDS}
        ]


@dataclass
class TaskInbox:
    """The messages a single task has received.

    An inbox is deliberately *not* a transcript that is fed back into a
    model: it is a structured record of what was said about the task, so
    that the orchestrator (and a human) can reconstruct the reasoning
    without replaying a conversation.
    """

    task_id: str
    assignment: dict[str, Any] | None = None
    progress: list[dict[str, Any]] = field(default_factory=list)
    handoff: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    revisions: list[dict[str, Any]] = field(default_factory=list)
    verification_requests: list[dict[str, Any]] = field(default_factory=list)
    cancellations: list[dict[str, Any]] = field(default_factory=list)
    approval_decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "assignment": self.assignment,
            "progress": list(self.progress),
            "handoff": self.handoff,
            "failures": list(self.failures),
            "escalations": list(self.escalations),
            "revisions": list(self.revisions),
            "verification_requests": list(self.verification_requests),
            "cancellations": list(self.cancellations),
            "approval_decisions": list(self.approval_decisions),
        }

    @property
    def handoff_summary(self) -> str:
        """The structured handoff summary for the task, if one was published."""
        if not self.handoff:
            return ""
        return str(self.handoff.get("summary") or "")


@dataclass
class SupervisorChannel:
    """The supervisor's view of every task.

    This is the receiving end of the typed protocol.  It is subscribed to
    the bus, so every message that reaches the supervisor is routed into
    the corresponding task's inbox.  The supervisor never reads a raw
    transcript; it reads structured records.
    """

    bus: MessageBus
    supervisor: str = "supervisor"
    inboxes: dict[str, TaskInbox] = field(default_factory=dict)
    escalations: list[dict[str, Any]] = field(default_factory=list)

    def inbox(self, task_id: str) -> TaskInbox:
        box = self.inboxes.get(task_id)
        if box is None:
            box = TaskInbox(task_id=task_id)
            self.inboxes[task_id] = box
        return box

    async def handle(self, message: Message) -> None:
        """Route a delivered message into the right inbox."""
        task_id = message.task_id or ""
        box = self.inbox(task_id)
        # The payload is re-validated through its typed model, so the inbox
        # holds the canonical shape rather than whatever the JSON happened
        # to look like on the way through the store.
        try:
            payload = message.typed_payload().model_dump()
        except Exception:
            payload = dict(message.payload)
        kind = message.kind

        if kind is MessageKind.TASK_ASSIGNMENT:
            box.assignment = dict(payload)
        elif kind is MessageKind.PROGRESS:
            box.progress.append(dict(payload))
        elif kind is MessageKind.HANDOFF:
            box.handoff = dict(payload)
        elif kind is MessageKind.FAILURE_REPORT:
            box.failures.append(dict(payload))
        elif kind is MessageKind.ESCALATION:
            box.escalations.append(dict(payload))
            self.escalations.append(
                {"task_id": task_id, **dict(payload), "sender": message.sender}
            )
        elif kind is MessageKind.REVISION_REQUEST:
            box.revisions.append(dict(payload))
        elif kind is MessageKind.VERIFICATION_REQUEST:
            box.verification_requests.append(dict(payload))
        elif kind is MessageKind.CANCELLATION:
            box.cancellations.append(dict(payload))
        elif kind is MessageKind.APPROVAL_DECISION:
            box.approval_decisions.append(dict(payload))

    def attach(self) -> None:
        """Subscribe this channel to every message in the run."""
        self.bus.subscribe_all(self.handle)

    def report(self) -> dict[str, Any]:
        return {
            "tasks": {tid: box.to_dict() for tid, box in sorted(self.inboxes.items())},
            "escalations": list(self.escalations),
        }
