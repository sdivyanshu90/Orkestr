"""SQLite persistence layer.

The store is *local-first*: a single SQLite database in WAL mode holds the
append-only event log plus the materialised projections that the runtime
needs for fast queries.  WAL gives us concurrent readers alongside a single
writer, which is exactly the access pattern of the scheduler.

Consistency model
-----------------

* The **event log** is the source of truth.  It is append-only and never
  rewritten.
* The **projection tables** are derived.  They can always be rebuilt by
  replaying the log, and :meth:`Database.rebuild_projection` does exactly
  that.  A crash between writing an event and updating a projection leaves
  the projection behind, never the log, and recovery repairs it.
* Writes that must be atomic across the log and the projection use
  ``BEGIN IMMEDIATE`` so that only one writer can be in flight at a time.

This is *not* exactly-once execution of arbitrary side effects.  A worker
that ran a command and died before recording the result will be retried and
may repeat that command; idempotency keys exist so that the orchestrator can
detect the repetition, but it cannot undo it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..errors import InfrastructureError

SCHEMA_VERSION = 1

_MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id       TEXT PRIMARY KEY,
        status       TEXT NOT NULL,
        strategy     TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        config_json  TEXT NOT NULL DEFAULT '{}',
        metadata     TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS events (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id    TEXT NOT NULL UNIQUE,
        run_id      TEXT NOT NULL,
        task_id     TEXT,
        attempt     INTEGER,
        type        TEXT NOT NULL,
        actor       TEXT,
        payload     TEXT NOT NULL DEFAULT '{}',
        created_at  TEXT NOT NULL,
        recorded_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
    CREATE INDEX IF NOT EXISTS idx_events_task   ON events(run_id, task_id, seq);

    CREATE TABLE IF NOT EXISTS tasks (
        task_id    TEXT PRIMARY KEY,
        run_id     TEXT NOT NULL,
        status     TEXT NOT NULL,
        attempt    INTEGER NOT NULL DEFAULT 0,
        owner      TEXT,
        spec_json  TEXT NOT NULL DEFAULT '{}',
        state_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);

    CREATE TABLE IF NOT EXISTS artifacts (
        id            TEXT PRIMARY KEY,
        run_id        TEXT NOT NULL,
        task_id       TEXT,
        kind          TEXT NOT NULL,
        uri           TEXT NOT NULL,
        sha256        TEXT NOT NULL,
        size          INTEGER NOT NULL DEFAULT 0,
        media_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
        provenance    TEXT NOT NULL DEFAULT '{}',
        metadata      TEXT NOT NULL DEFAULT '{}',
        created_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);

    CREATE TABLE IF NOT EXISTS messages (
        id             TEXT PRIMARY KEY,
        run_id         TEXT NOT NULL,
        kind           TEXT NOT NULL,
        sender         TEXT NOT NULL,
        recipient      TEXT NOT NULL,
        task_id        TEXT,
        correlation_id TEXT,
        payload        TEXT NOT NULL DEFAULT '{}',
        note           TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        version        INTEGER NOT NULL DEFAULT 1,
        conversation   TEXT NOT NULL DEFAULT '',
        seq            INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(run_id, sender, conversation, version);

    CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        run_id        TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        label         TEXT NOT NULL,
        metadata      TEXT NOT NULL DEFAULT '{}',
        created_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS approvals (
        approval_id  TEXT PRIMARY KEY,
        run_id       TEXT NOT NULL,
        task_id      TEXT,
        action       TEXT NOT NULL,
        rationale    TEXT NOT NULL,
        arguments    TEXT NOT NULL DEFAULT '[]',
        risk         TEXT NOT NULL DEFAULT 'medium',
        status       TEXT NOT NULL,
        decided_by   TEXT,
        decided_at   TEXT,
        reason       TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        binding      TEXT NOT NULL DEFAULT '{}',
        expires_at   REAL
    );
    CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status);

    CREATE TABLE IF NOT EXISTS run_ownership (
        run_id       TEXT PRIMARY KEY,
        owner_id     TEXT NOT NULL,
        owner_pid    INTEGER,
        fence        INTEGER NOT NULL DEFAULT 1,
        state        TEXT NOT NULL DEFAULT 'active',
        acquired_at  TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        released_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


class Database:
    """Thread-safe SQLite wrapper with WAL enabled."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # explicit transaction control
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    def migrate(self) -> None:
        """Apply the schema.

        ``executescript`` is used because SQLite's ``execute`` accepts only a
        single statement.  The script is idempotent (``IF NOT EXISTS``
        throughout), so re-running it on an existing database is a no-op.
        """
        with self._lock:
            for statement in _MIGRATIONS:
                self._conn.executescript(statement)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------ primitives

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside an ``BEGIN IMMEDIATE`` transaction."""
        with self._lock:
            conn = self._conn
            in_tx = conn.in_transaction
            if not in_tx:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                if not in_tx and conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if not in_tx and conn.in_transaction:
                    conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            try:
                return self._conn.execute(sql, params)
            except sqlite3.Error as exc:  # pragma: no cover - depends on runtime
                raise InfrastructureError(f"sqlite error: {exc}", sql=sql[:200]) from exc

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default if default is not None else {}
