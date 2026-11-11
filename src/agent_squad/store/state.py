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
from .events import EventLog, EventType


def _parse_timestamp(value: Any) -> float | None:
    """Parse an ISO timestamp into a POSIX timestamp."""
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


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
            existing = conn.execute(
                "SELECT run_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO runs(run_id, status, strategy, created_at, updated_at, "
                    "config_json, metadata) VALUES (?,?,?,?,?,?,?)",
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
            else:
                # An existing run is never silently replaced: its history
                # belongs to the run that created it.
                conn.execute(
                    "UPDATE runs SET updated_at = ?, config_json = ?, metadata = ? "
                    "WHERE run_id = ?",
                    (now, dumps(config or {}), dumps(metadata or {}), run_id),
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

    def set_strategy(self, run_id: str, strategy: str) -> None:
        """Record the execution strategy selected for a run."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE runs SET strategy = ?, updated_at = ? WHERE run_id = ?",
                (strategy, utcnow().isoformat(), run_id),
            )

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

    def rebuild_projection(self, run_id: str) -> dict[str, int]:
        """Rebuild the task projection by replaying the event log.

        The log is the source of truth.  A crash between the event append and
        the projection update leaves the projection behind (recoverable); the
        reverse cannot happen because both happen in one transaction.  This
        method repairs the former.

        Returns a summary of what was rebuilt.
        """
        events = self._log.read(run_id)
        states: dict[str, TaskState] = {}
        specs: dict[str, TaskSpec] = {}
        replayed = 0
        for event in events:
            if event.type is EventType.TASK_CREATED:
                spec_data = event.payload.get("spec") or {}
                try:
                    spec = TaskSpec.model_validate(spec_data)
                except Exception:
                    continue
                specs[spec.id] = spec
                if spec.id not in states:
                    # A task may be re-registered (a replan re-states it).
                    # The *first* registration defines the task; a later one
                    # must not reset the state the run has accumulated.
                    states[spec.id] = TaskState(
                        id=spec.id,
                        run_id=spec.run_id,
                        max_attempts=spec.budget.max_attempts,
                    )
                replayed += 1
            elif event.type is EventType.TASK_STATUS_CHANGED and event.task_id:
                state = states.get(event.task_id)
                if state is None:
                    continue
                target = event.payload.get("to")
                try:
                    new_status = TaskStatus(target)
                except ValueError:
                    continue
                if new_status != state.status:
                    state.status = new_status
                if event.payload.get("error_class"):
                    state.error_class = str(event.payload["error_class"])
                if event.payload.get("error_message"):
                    state.error_message = str(event.payload["error_message"])
                if new_status in (
                    TaskStatus.SUCCEEDED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.TIMED_OUT,
                ):
                    state.finished_at = event.created_at
                replayed += 1
            elif event.type is EventType.TASK_ATTEMPT_STARTED and event.task_id:
                state = states.get(event.task_id)
                if state is None:
                    continue
                state.attempt = int(event.attempt or state.attempt + 1)
                state.owner = event.actor
                state.started_at = event.created_at
                lease = event.payload.get("lease_expires_at")
                if isinstance(lease, str) and lease:
                    try:
                        state.lease_expires_at = _dt.datetime.fromisoformat(lease)
                    except ValueError:
                        state.lease_expires_at = None
                replayed += 1

        with self._db.transaction() as conn:
            for task_id, state in states.items():
                task_spec = specs.get(task_id)
                conn.execute(
                    "INSERT INTO tasks(task_id, run_id, status, attempt, owner, spec_json, "
                    "state_json, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, "
                    "attempt=excluded.attempt, owner=excluded.owner, "
                    "spec_json=excluded.spec_json, state_json=excluded.state_json, "
                    "updated_at=excluded.updated_at",
                    (
                        task_id,
                        state.run_id,
                        state.status.value,
                        state.attempt,
                        state.owner,
                        task_spec.model_dump_json() if task_spec else "{}",
                        state.model_dump_json(),
                        state.updated_at.isoformat(),
                    ),
                )
        return {"tasks": len(states), "events_replayed": replayed}

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

    def invalidate_stale_artifacts(
        self, run_id: str, *, current_revision: str, reason: str = "repository state changed"
    ) -> list[str]:
        """Invalidate artifacts whose base revision is no longer the current one.

        An artifact is bound to the tree it was produced against.  When the
        repository moves on, artifacts computed against the old tree are no
        longer safe to treat as describing the current state, so they are
        marked invalid rather than silently reused.

        Returns the ids of the artifacts that were invalidated.
        """
        invalidated: list[str] = []
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, provenance, metadata FROM artifacts WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            for row in rows:
                provenance = loads(row["provenance"], {})
                base = provenance.get("base_revision")
                if not base or base == current_revision:
                    continue
                metadata = loads(row["metadata"], {})
                metadata["invalidated"] = True
                metadata["invalidated_reason"] = reason
                metadata["invalidated_against"] = current_revision
                conn.execute(
                    "UPDATE artifacts SET metadata = ? WHERE id = ?",
                    (dumps(metadata), row["id"]),
                )
                invalidated.append(row["id"])
        return invalidated

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

    # ------------------------------------------------------- run ownership

    def acquire_run_ownership(
        self,
        run_id: str,
        *,
        owner_id: str,
        owner_pid: int | None = None,
        lease_s: float = 300.0,
    ) -> int:
        """Take exclusive ownership of a run, returning a fencing token.

        Two processes must not drive the same run at once: their state
        updates would interleave and the attempt fence would not save them,
        because each would hold a *current* attempt number at some moment.

        The acquisition is atomic (``BEGIN IMMEDIATE``): the loser of a
        race either sees no row, or sees a row whose lease has expired.  The
        returned token is a monotonically increasing **fence**: every later
        ownership-sensitive update carries it, and an update with a stale
        fence is rejected.
        """
        now = utcnow()
        now_ts = now.timestamp()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT owner_id, fence, state, heartbeat_at FROM run_ownership "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is not None and row["state"] == "active":
                last_beat = _parse_timestamp(row["heartbeat_at"])
                if last_beat is not None and now_ts - last_beat < lease_s:
                    raise ValidationError(
                        f"run {run_id} is already owned by {row['owner_id']}",
                        run_id=run_id,
                        owner=row["owner_id"],
                    )
            fence = (int(row["fence"]) + 1) if row is not None else 1
            conn.execute(
                "INSERT INTO run_ownership(run_id, owner_id, owner_pid, fence, state, "
                "acquired_at, heartbeat_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET owner_id=excluded.owner_id, "
                "owner_pid=excluded.owner_pid, fence=excluded.fence, state='active', "
                "acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at, "
                "released_at=NULL",
                (
                    run_id,
                    owner_id,
                    owner_pid,
                    fence,
                    "active",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return fence

    def heartbeat_run_ownership(
        self, run_id: str, *, owner_id: str, fence: int
    ) -> bool:
        """Refresh the lease.  Fails if the fence is stale."""
        now = utcnow().isoformat()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE run_ownership SET heartbeat_at = ? "
                "WHERE run_id = ? AND owner_id = ? AND fence = ? AND state = 'active'",
                (now, run_id, owner_id, fence),
            )
            return cur.rowcount > 0

    def release_run_ownership(self, run_id: str, *, owner_id: str, fence: int) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE run_ownership SET state = 'released', released_at = ? "
                "WHERE run_id = ? AND owner_id = ? AND fence = ?",
                (utcnow().isoformat(), run_id, owner_id, fence),
            )
            return cur.rowcount > 0

    def run_ownership(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM run_ownership WHERE run_id = ?", (run_id,)
        )
        return dict(row) if row else None

    def assert_run_ownership(self, run_id: str, *, owner_id: str, fence: int) -> None:
        """Raise unless the caller still holds the run with this fence."""
        record = self.run_ownership(run_id)
        if record is None:
            raise ValidationError(
                f"run {run_id} has no owner; ownership must be acquired first",
                run_id=run_id,
            )
        if int(record["fence"]) != fence or record["owner_id"] != owner_id:
            raise StaleAttemptError(
                f"run {run_id} is owned by {record['owner_id']} at fence "
                f"{record['fence']}; this caller holds {owner_id} at fence {fence}",
                run_id=run_id,
            )

    # ------------------------------------------------------- durable messages

    def record_message_versioned(
        self, message: Any, version: int, conversation: str = ""
    ) -> bool:
        """Record a message, refusing a duplicate id.

        The store is the authority on duplication, so the check survives a
        restart: a message that was already delivered cannot be delivered
        again by a process that has lost its in-memory state.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages(id, run_id, kind, sender, recipient, "
                "task_id, correlation_id, payload, note, created_at, version, "
                "conversation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message.id,
                    message.run_id,
                    message.kind,
                    message.sender,
                    message.recipient,
                    message.task_id,
                    message.correlation_id,
                    dumps(message.payload),
                    message.note,
                    message.created_at.isoformat(),
                    version,
                    conversation,
                ),
            )
            return cur.rowcount > 0

    def latest_message_version(
        self, run_id: str, sender: str, conversation: str | None
    ) -> int:
        """The highest version recorded for a conversation."""
        if conversation:
            row = self._db.query_one(
                "SELECT MAX(version) AS v FROM messages WHERE run_id = ? AND sender = ? "
                "AND conversation = ?",
                (run_id, sender, conversation),
            )
        else:
            row = self._db.query_one(
                "SELECT MAX(version) AS v FROM messages WHERE run_id = ? AND sender = ?",
                (run_id, sender),
            )
        return int(row["v"]) if row and row["v"] is not None else 0

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
        binding: dict[str, Any] | None = None,
        ttl_s: float | None = None,
    ) -> str:
        """Record an approval request, bound to the action it is about."""
        approval_id = new_id("apr")
        now = utcnow()
        expires_at = (now.timestamp() + ttl_s) if ttl_s else None
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO approvals(approval_id, run_id, task_id, action, rationale, "
                "arguments, risk, status, created_at, binding, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                    dumps(binding or {}),
                    expires_at,
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
                    "binding": binding or {},
                    "expires_at": expires_at,
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
            "binding": loads(row["binding"], {}),
            "expires_at": row["expires_at"],
        }
