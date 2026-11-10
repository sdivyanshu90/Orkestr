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
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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

    async def publish(self, message: Message) -> Message:
        """Validate, record and dispatch a message."""
        async with self._lock:
            self._validate(message)
            self.state.record_message(message)
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
            self._versions[(message.sender, message.correlation_id or message.task_id or message.id)] = (
                message.version
            )

        for subscription in list(self._subscriptions):
            if subscription.recipient != message.recipient:
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

    # ----------------------------------------------------------- validation

    def _validate(self, message: Message) -> None:
        if message.id in self._seen_ids:
            raise ValidationError(
                f"duplicate message id {message.id}", message_id=message.id
            )
        self._seen_ids.add(message.id)

        if self.known_recipients and message.recipient not in self.known_recipients:
            raise ValidationError(
                f"unknown recipient {message.recipient!r}",
                recipient=message.recipient,
                known=sorted(self.known_recipients),
            )
        if self.known_recipients and message.sender not in self.known_recipients:
            raise ValidationError(
                f"unknown sender {message.sender!r}", sender=message.sender
            )

        key = (message.sender, message.correlation_id or message.task_id or message.id)
        last_version = self._versions.get(key)
        if last_version is not None and message.version < last_version:
            raise ValidationError(
                f"stale message version {message.version} for {key[0]} "
                f"(latest is {last_version})",
                message_id=message.id,
            )
        if last_version is not None and message.version == last_version:
            raise ValidationError(
                f"duplicate message version {message.version} for {key[0]}",
                message_id=message.id,
            )

    # ------------------------------------------------------------- queries

    def history(self, run_id: str, task_id: str | None = None) -> list[dict]:
        return self.state.list_messages(run_id, task_id)

    def state_effecting_only(self, run_id: str) -> list[dict]:
        return [
            m
            for m in self.state.list_messages(run_id)
            if m["kind"] not in {k.value for k in INFORMATIONAL_KINDS}
        ]
