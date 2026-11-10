"""Append-only event log.

Every state change in the orchestrator is recorded here first.  The log is
the source of truth for recovery and replay; projections are derived.

Events are immutable.  There is no update or delete API on purpose: an
append-only log is what makes "what happened?" answerable after a crash, and
it removes a whole class of bugs where two code paths disagree about the
current state because one of them mutated a row in place.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..ids import new_id
from ..models.task import utcnow
from .db import Database, dumps, loads


class EventType(str, Enum):
    """Every event the orchestrator can record.

    Values are part of the on-disk contract; they are grouped by prefix for
    readability but the prefix carries no semantics.
    """

    # -- run lifecycle -------------------------------------------------
    RUN_CREATED = "run.created"
    RUN_STATUS_CHANGED = "run.status_changed"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_CANCELLED = "run.cancelled"
    RUN_FAILED = "run.failed"
    STRATEGY_SELECTED = "run.strategy_selected"
    PLAN_CREATED = "run.plan_created"
    PLAN_REVISED = "run.plan_revised"
    BUDGET_EXHAUSTED = "run.budget_exhausted"

    # -- task lifecycle ------------------------------------------------
    TASK_CREATED = "task.created"
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_ATTEMPT_STARTED = "task.attempt_started"
    TASK_ATTEMPT_FINISHED = "task.attempt_finished"
    TASK_ASSIGNED = "task.assigned"
    TASK_BLOCKED = "task.blocked"
    TASK_RETRY_SCHEDULED = "task.retry_scheduled"
    TASK_REPLANNED = "task.replanned"

    # -- agent / model / tool -------------------------------------------
    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"
    MODEL_CALL = "model.call"
    MODEL_ERROR = "model.error"
    TOOL_INVOKED = "tool.invoked"
    TOOL_DENIED = "tool.denied"
    POLICY_DENIED = "policy.denied"

    # -- messaging / artifacts ------------------------------------------
    MESSAGE_SENT = "message.sent"
    MESSAGE_RECEIVED = "message.received"
    ARTIFACT_PUBLISHED = "artifact.published"

    # -- integration / verification ------------------------------------
    PATCH_CAPTURED = "patch.captured"
    INTEGRATION_ATTEMPTED = "integration.attempted"
    INTEGRATION_CONFLICT = "integration.conflict"
    INTEGRATION_VERIFIED = "integration.verified"
    VERIFICATION_CHECK = "verification.check"
    VERIFICATION_PASSED = "verification.passed"
    VERIFICATION_FAILED = "verification.failed"
    HUMAN_APPROVAL = "verification.human_approval"

    # -- durability ------------------------------------------------------
    CHECKPOINT_CREATED = "checkpoint.created"
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_COMPLETED = "recovery.completed"
    CANCELLATION_REQUESTED = "control.cancellation_requested"


class Event(BaseModel):
    """A single immutable record in the log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = 0
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    run_id: str
    type: EventType
    task_id: str | None = None
    attempt: int | None = None
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: _dt.datetime = Field(default_factory=utcnow)
    recorded_at: float = Field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class EventLog:
    """Append-only log over a :class:`~agent_squad.store.db.Database`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def append(
        self,
        event_type: EventType,
        *,
        run_id: str,
        task_id: str | None = None,
        attempt: int | None = None,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: _dt.datetime | None = None,
    ) -> Event:
        event = Event(
            run_id=run_id,
            type=event_type,
            task_id=task_id,
            attempt=attempt,
            actor=actor,
            payload=payload or {},
            created_at=created_at or utcnow(),
        )
        self.append_event(event)
        return event

    def append_event(self, event: Event) -> Event:
        with self._db.transaction() as conn:
            self.append_in_transaction(conn, event)
        return event

    def append_in_transaction(self, conn: Any, event: Event) -> Event:
        """Append an event using an existing transaction.

        Callers that need the event write and a projection update to be
        atomic use this method inside ``db.transaction()``.
        """
        cur = conn.execute(
            "INSERT INTO events(event_id, run_id, task_id, attempt, type, actor, "
            "payload, created_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.run_id,
                event.task_id,
                event.attempt,
                event.type.value,
                event.actor,
                dumps(event.payload),
                event.created_at.isoformat(),
                event.recorded_at,
            ),
        )
        return event.model_copy(update={"seq": int(cur.lastrowid or 0)})

    def build(
        self,
        event_type: EventType,
        *,
        run_id: str,
        task_id: str | None = None,
        attempt: int | None = None,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: _dt.datetime | None = None,
    ) -> Event:
        """Create an event without writing it (for transactional batching)."""
        return Event(
            run_id=run_id,
            type=event_type,
            task_id=task_id,
            attempt=attempt,
            actor=actor,
            payload=payload or {},
            created_at=created_at or utcnow(),
        )

    # ------------------------------------------------------------------ read

    def read(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        task_id: str | None = None,
        types: list[EventType] | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        clauses = ["run_id = ?", "seq > ?"]
        params: list[Any] = [run_id, after_seq]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"type IN ({placeholders})")
            params.extend(t.value for t in types)
        sql = (
            "SELECT seq, event_id, run_id, task_id, attempt, type, actor, payload, "
            f"created_at, recorded_at FROM events WHERE {' AND '.join(clauses)} "
            "ORDER BY seq ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._db.query(sql, tuple(params))
        return [self._row_to_event(r) for r in rows]

    def last_seq(self, run_id: str) -> int:
        row = self._db.query_one("SELECT MAX(seq) AS s FROM events WHERE run_id = ?", (run_id,))
        return int(row["s"]) if row and row["s"] is not None else 0

    def count(self, run_id: str) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS c FROM events WHERE run_id = ?", (run_id,))
        return int(row["c"]) if row else 0

    @staticmethod
    def _row_to_event(row: Any) -> Event:
        return Event(
            seq=int(row["seq"]),
            event_id=row["event_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt=row["attempt"],
            type=EventType(row["type"]),
            actor=row["actor"],
            payload=loads(row["payload"], {}),
            created_at=_dt.datetime.fromisoformat(row["created_at"]),
            recorded_at=float(row["recorded_at"]),
        )

    def export(self, run_id: str) -> str:
        """Return the log as newline-delimited JSON (portable export format)."""
        lines = [json.dumps(e.to_dict(), separators=(",", ":")) for e in self.read(run_id)]
        return "\n".join(lines)
