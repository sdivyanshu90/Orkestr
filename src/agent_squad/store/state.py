"""Materialised runtime state.

``StateStore`` is the only component allowed to mutate a task's status.  It
enforces three invariants:

1. **Legal transitions only.**  Every move goes through
   :func:`~agent_squad.models.task.assert_transition`.
2. **Attempt fencing.**  A worker may only write state for the attempt it
   owns.  A late update from a superseded attempt is rejected with
   :class:`~agent_squad.errors.StaleAttemptError` rather than overwriting a
   newer result.
3. **Atomicity.**  The event append and the projection update happen in one
   SQLite transaction, so a crash can never leave the log and the projection
   disagreeing in the dangerous direction (log ahead is recoverable, projection
   ahead is not).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..errors import StaleAttemptError, ValidationError
from ..ids import new_id
from ..models.artifact import Artifact
from ..models.message import Message
from ..models.run import RunStatus
from ..models.task import TaskSpec, TaskState, TaskStatus, assert_transition, utcnow
from .db import Database, dumps, loads
from .events import Event, EventLog, EventType


class StateStore:
    """Transactional access to the run/task state projections."""

    def __init__(self, db: Database, log: EventLog | None = None) -> None:
        self._db = db
        self._log = log or EventLog(db)

    @property
    def log(self) -> EventLog:
        return self._log

    # ------------------------------------------------------------------ runs

    def create_run(
        self,
        run_id: str,
        *,
        config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        strategy: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, status, strategy, created_at, "
                "updated_at, config_json, metadata) VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    RunStatus.CREATED.value,
                    strategy,
                    now,
                    now,
                    dumps(config or {}),
                    dumps(metadata or {}),
                ),
            )
            event = self._log.build(
                EventType.RUN_CREATED,
                run_id=run_id,
                payload={"config": config or {}, "metadata": metadata or {}},
            )
            self._log.append_in_transaction(conn, event)

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = utcnow().isoformat()
        with self._db.transaction() as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            previous = row["status"] if row else None
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status.value, now, run_id),
            )
            event = self._log.build(
                EventType.RUN_STATUS_CHANGED,
                run_id=run_id,
                actor=actor,
                payload={"from": previous, "to": status.value, **(payload or {})},
            )
            self._log.append_in_transaction(conn, event)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "strategy": row["strategy"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "config": loads(row["config_json"], {}),
            "metadata": loads(row["metadata"], {}),
        }

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT run_id, status, strategy, created_at, updated_at FROM runs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "strategy": r["strategy"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    # ----------------------------------------------------------------- tasks

    def upsert_task(self, spec: TaskSpec) -> None:
        """Register a task (or replace its spec after a replan)."""
        now = utcnow().isoformat()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status, state_json FROM tasks WHERE task_id = ?", (spec.id,)
            ).fetchone()
            if row is None:
                state = TaskState(
                    id=spec.id,
                    run_id=spec.run_id,
                    max_attempts=spec.budget.max_attempts,
                )
            else:
                state = TaskState.model_validate(loads(row["state_json"], {}))
            conn.execute(
                "INSERT INTO tasks(task_id, run_id, status, attempt, owner, spec_json, "
                "state_json, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
                "attempt=excluded.attempt, owner=excluded.owner, "
                "spec_json=excluded.spec_json, state_json=excluded.state_json, "
                "updated_at=excluded.updated_at",
                (
                    spec.id,
                    spec.run_id,
                    state.status.value,
                    state.attempt,
                    state.owner,
                    spec.model_dump_json(),
                    state.model_dump_json(),
                    now,
                ),
            )
            event = self._log.build(
                EventType.TASK_CREATED,
                run_id=spec.run_id,
                task_id=spec.id,
                payload={"spec": spec.model_dump(mode="json")},
            )
            self._log.append_in_transaction(conn, event)

    def get_task_state(self, task_id: str) -> TaskState | None:
        row = self._db.query_one("SELECT state_json FROM tasks WHERE task_id = ?", (task_id,))
        if not row:
            return None
        return TaskState.model_validate(loads(row["state_json"], {}))

    def get_task_spec(self, task_id: str) -> TaskSpec | None:
        row = self._db.query_one("SELECT spec_json FROM tasks WHERE task_id = ?", (task_id,))
        if not row:
            return None
        return TaskSpec.model_validate(loads(row["spec_json"], {}))

    def list_task_states(self, run_id: str) -> list[TaskState]:
        rows = self._db.query(
            "SELECT state_json FROM tasks WHERE run_id = ? ORDER BY task_id", (run_id,)
        )
        return [TaskState.model_validate(loads(r["state_json"], {})) for r in rows]

    def list_task_specs(self, run_id: str) -> list[TaskSpec]:
        rows = self._db.query(
            "SELECT spec_json FROM tasks WHERE run_id = ? ORDER BY task_id", (run_id,)
        )
        return [TaskSpec.model_validate(loads(r["spec_json"], {})) for r in rows]

    def transition_task(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        attempt: int | None = None,
        actor: str | None = None,
        payload: dict[str, Any] | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        owner: str | None = None,
    ) -> TaskState:
        """Move a task to ``target``, enforcing the lifecycle table.

        ``attempt`` is the attempt the caller believes it owns.  When the
        stored attempt is newer, the update is rejected as stale.
        """
        now = utcnow()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(f"unknown task: {task_id}", task_id=task_id)
            state = TaskState.model_validate(loads(row["state_json"], {}))
            if attempt is not None and attempt != state.attempt:
                raise StaleAttemptError(
                    f"attempt {attempt} is stale for task {task_id} "
                    f"(current attempt is {state.attempt})",
                    task_id=task_id,
                    attempt=attempt,
                    current_attempt=state.attempt,
                )
            assert_transition(state.status, target)
            previous = state.status
            state.transition_to(target)
            if error_class is not None:
                state.error_class = error_class
            if error_message is not None:
                state.error_message = error_message
            if owner is not None:
                state.owner = owner
            if target in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED,
                          TaskStatus.TIMED_OUT):
                state.finished_at = now
            conn.execute(
                "UPDATE tasks SET status = ?, attempt = ?, owner = ?, state_json = ?, "
                "updated_at = ? WHERE task_id = ?",
                (
                    state.status.value,
                    state.attempt,
                    state.owner,
                    state.model_dump_json(),
                    now.isoformat(),
                    task_id,
                ),
            )
            event = self._log.build(
                EventType.TASK_STATUS_CHANGED,
                run_id=state.run_id,
                task_id=task_id,
                attempt=state.attempt,
                actor=actor,
                payload={
                    "from": previous.value,
                    "to": target.value,
                    "error_class": error_class,
                    "error_message": error_message,
                    **(payload or {}),
                },
            )
            self._log.append_in_transaction(conn, event)
            return state

    def start_attempt(self, task_id: str, *, owner: str, lease_ttl_s: float) -> TaskState:
        """Fence a new attempt and take an ownership lease."""
        now = utcnow()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(f"unknown task: {task_id}", task_id=task_id)
            state = TaskState.model_validate(loads(row["state_json"], {}))
            if state.attempt >= state.max_attempts:
                raise ValidationError(
                    f"task {task_id} exhausted its {state.max_attempts} attempts",
                    task_id=task_id,
                )
            state.attempt += 1
            state.owner = owner
            state.started_at = now
            state.finished_at = None
            state.error_class = None
            state.error_message = None
            state.lease_expires_at = now + _dt.timedelta(seconds=lease_ttl_s)
            conn.execute(
                "UPDATE tasks SET status = ?, attempt = ?, owner = ?, state_json = ?, "
                "updated_at = ? WHERE task_id = ?",
                (
                    state.status.value,
                    state.attempt,
                    state.owner,
                    state.model_dump_json(),
                    now.isoformat(),
                    task_id,
                ),
            )
            event = self._log.build(
                EventType.TASK_ATTEMPT_STARTED,
                run_id=state.run_id,
                task_id=task_id,
                attempt=state.attempt,
                actor=owner,
                payload={"owner": owner, "lease_expires_at": state.lease_expires_at.isoformat()},
            )
            self._log.append_in_transaction(conn, event)
            return state

    def update_usage(
        self,
        task_id: str,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> TaskState:
        """Accumulate token/cost usage for a task (never decreases)."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(f"unknown task: {task_id}", task_id=task_id)
            state = TaskState.model_validate(loads(row["state_json"], {}))
            state.tokens_used += tokens
            state.cost_used_usd = round(state.cost_used_usd + cost_usd, 8)
            state.updated_at = utcnow()
            conn.execute(
                "UPDATE tasks SET state_json = ?, updated_at = ? WHERE task_id = ?",
                (state.model_dump_json(), state.updated_at.isoformat(), task_id),
            )
            return state

    def set_verification(
        self, task_id: str, verification: dict[str, Any]
    ) -> TaskState:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(f"unknown task: {task_id}", task_id=task_id)
            state = TaskState.model_validate(loads(row["state_json"], {}))
            state.verification = verification
            state.updated_at = utcnow()
            conn.execute(
                "UPDATE tasks SET state_json = ?, updated_at = ? WHERE task_id = ?",
                (state.model_dump_json(), state.updated_at.isoformat(), task_id),
            )
            return state

    # ------------------------------------------------------------- artifacts

    def save_artifact(self, artifact: Artifact) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts(id, run_id, task_id, kind, uri, sha256, "
                "size, media_type, provenance, metadata, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact.id,
                    artifact.provenance.run_id,
                    artifact.provenance.task_id,
                    artifact.kind.value,
                    artifact.uri,
                    artifact.sha256,
                    artifact.size,
                    artifact.media_type,
                    artifact.provenance.model_dump_json(),
                    dumps(artifact.metadata),
                    artifact.provenance.created_at.isoformat(),
                ),
            )

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        row = self._db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not row:
            return None
        return Artifact(
            id=row["id"],
            kind=row["kind"],
            uri=row["uri"],
            sha256=row["sha256"],
            size=row["size"],
            media_type=row["media_type"],
            provenance=loads(row["provenance"], {}),
            metadata=loads(row["metadata"], {}),
        )

    def list_artifacts(self, run_id: str, task_id: str | None = None) -> list[Artifact]:
        if task_id:
            rows = self._db.query(
                "SELECT id FROM artifacts WHERE run_id = ? AND task_id = ? ORDER BY id",
                (run_id, task_id),
            )
        else:
            rows = self._db.query(
                "SELECT id FROM artifacts WHERE run_id = ? ORDER BY id", (run_id,)
            )
        out: list[Artifact] = []
        for row in rows:
            artifact = self.get_artifact(row["id"])
            if artifact:
                out.append(artifact)
        return out

    # -------------------------------------------------------------- messages

    def record_message(self, message: Message) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages(id, run_id, kind, sender, recipient, "
                "task_id, correlation_id, payload, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    message.id,
                    message.run_id,
                    message.kind.value,
                    message.sender,
                    message.recipient,
                    message.task_id,
                    message.correlation_id,
                    dumps(message.payload),
                    message.note,
                    message.created_at.isoformat(),
                ),
            )

    def list_messages(self, run_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id:
            rows = self._db.query(
                "SELECT * FROM messages WHERE run_id = ? AND task_id = ? ORDER BY created_at",
                (run_id, task_id),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM messages WHERE run_id = ? ORDER BY created_at", (run_id,)
            )
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "sender": r["sender"],
                "recipient": r["recipient"],
                "task_id": r["task_id"],
                "correlation_id": r["correlation_id"],
                "payload": loads(r["payload"], {}),
                "note": r["note"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ----------------------------------------------------------- checkpoints

    def create_checkpoint(
        self, run_id: str, *, label: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        seq = self._log.last_seq(run_id)
        checkpoint_id = new_id("ckp")
        now = utcnow()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO checkpoints(checkpoint_id, run_id, seq, label, metadata, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (checkpoint_id, run_id, seq, label, dumps(metadata or {}), now.isoformat()),
            )
            event = self._log.build(
                EventType.CHECKPOINT_CREATED,
                run_id=run_id,
                payload={"checkpoint_id": checkpoint_id, "label": label, "seq": seq},
            )
            self._log.append_in_transaction(conn, event)
        return {
            "checkpoint_id": checkpoint_id,
            "run_id": run_id,
            "seq": seq,
            "label": label,
            "metadata": metadata or {},
            "created_at": now.isoformat(),
        }

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY seq", (run_id,)
        )
        return [
            {
                "checkpoint_id": r["checkpoint_id"],
                "run_id": r["run_id"],
                "seq": r["seq"],
                "label": r["label"],
                "metadata": loads(r["metadata"], {}),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------- approvals

    def create_approval(
        self,
        run_id: str,
        *,
        action: str,
        rationale: str,
        arguments: list[str] | None = None,
        risk: str = "medium",
        task_id: str | None = None,
    ) -> str:
        approval_id = new_id("apr")
        now = utcnow()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO approvals(approval_id, run_id, task_id, action, rationale, "
                "arguments, risk, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    approval_id,
                    run_id,
                    task_id,
                    action,
                    rationale,
                    dumps(arguments or []),
                    risk,
                    "pending",
                    now.isoformat(),
                ),
            )
            event = self._log.build(
                EventType.HUMAN_APPROVAL,
                run_id=run_id,
                task_id=task_id,
                payload={
                    "approval_id": approval_id,
                    "action": action,
                    "status": "requested",
                    "rationale": rationale,
                    "arguments": arguments or [],
                    "risk": risk,
                },
            )
            self._log.append_in_transaction(conn, event)
        return approval_id

    def decide_approval(
        self, approval_id: str, *, approved: bool, decided_by: str, reason: str = ""
    ) -> dict[str, Any]:
        now = utcnow()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(f"unknown approval: {approval_id}")
            if row["status"] != "pending":
                raise ValidationError(
                    f"approval {approval_id} already {row['status']}",
                    approval_id=approval_id,
                    status=row["status"],
                )
            status = "approved" if approved else "denied"
            conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, "
                "reason = ? WHERE approval_id = ?",
                (status, decided_by, now.isoformat(), reason, approval_id),
            )
            event = self._log.build(
                EventType.HUMAN_APPROVAL,
                run_id=row["run_id"],
                task_id=row["task_id"],
                payload={
                    "approval_id": approval_id,
                    "action": row["action"],
                    "status": status,
                    "decided_by": decided_by,
                    "reason": reason,
                },
            )
            self._log.append_in_transaction(conn, event)
        return {
            "approval_id": approval_id,
            "run_id": row["run_id"],
            "action": row["action"],
            "status": status,
            "decided_by": decided_by,
            "reason": reason,
        }

    def pending_approvals(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM approvals WHERE run_id = ? AND status = 'pending'", (run_id,)
        )
        return [
            {
                "approval_id": r["approval_id"],
                "run_id": r["run_id"],
                "task_id": r["task_id"],
                "action": r["action"],
                "rationale": r["rationale"],
                "arguments": loads(r["arguments"], []),
                "risk": r["risk"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        if not row:
            return None
        return {
            "approval_id": row["approval_id"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "action": row["action"],
            "rationale": row["rationale"],
            "arguments": loads(row["arguments"], []),
            "risk": row["risk"],
            "status": row["status"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }
