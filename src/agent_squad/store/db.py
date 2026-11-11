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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import InfrastructureError

#: The newest schema this build understands.  ``PRAGMA user_version`` holds
#: the version that is physically present in the file; the two must match
#: after :meth:`Database.migrate` has run.
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Migration:
    """One ordered, forward-only schema step.

    Migrations are identified by the *target* version so that a file at
    version ``N`` is upgraded by applying every step with ``version > N`` in
    ascending order.  Each step must be idempotent: SQLite's
    ``executescript`` commits before it runs, so a crash can leave a file
    with the schema applied but ``user_version`` not yet bumped.  Re-running
    the step is then the recovery path, and a non-idempotent step would
    corrupt an otherwise healthy file.
    """

    version: int
    description: str
    sql: str


_BASELINE_SCHEMA = """
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
    """

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline event log and projections", _BASELINE_SCHEMA),
    Migration(
        2,
        "index event types for recovery and replay scans",
        """
        CREATE INDEX IF NOT EXISTS idx_events_run_type
            ON events(run_id, type, seq);
        """,
    ),
)


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

    @property
    def schema_version(self) -> int:
        """The schema version physically stored in this database file."""
        with self._lock:
            return self._read_user_version()

    def migrate(self) -> None:
        """Upgrade the file to :data:`SCHEMA_VERSION`, in order.

        ``PRAGMA user_version`` is the source of truth.  A database created
        by a pre-migration build has ``user_version == 0`` but already
        contains the baseline tables (and may carry
        ``meta.schema_version``), so it is *adopted* at that version rather
        than re-created: re-running the baseline is harmless, but silently
        resetting a newer file's version would hide a downgrade.

        Running this twice is a no-op: applied steps are skipped, and a
        later reopen does not re-execute their SQL.  A file written by a
        newer build is refused instead of being used with an older schema.
        """
        with self._lock:
            current = self._read_user_version()
            if current == 0:
                current = self._legacy_version()
                if current:
                    self._write_version(current)
            if current > SCHEMA_VERSION:
                raise InfrastructureError(
                    f"database schema version {current} is newer than this build "
                    f"({SCHEMA_VERSION}); upgrade agent-squad before opening it",
                    path=str(self.path),
                    schema_version=current,
                )
            for migration in MIGRATIONS:
                if migration.version <= current:
                    continue
                self._conn.executescript(migration.sql)
                self._write_version(migration.version)
                current = migration.version

    def _read_user_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    def _write_version(self, version: int) -> None:
        # PRAGMA does not accept bound parameters; ``version`` is an int read
        # from a migration tuple, never user input.
        self._conn.execute(f"PRAGMA user_version = {version:d}")
        if self._table_exists("meta"):
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(version),),
            )

    def _legacy_version(self) -> int:
        """Best-effort version of a file written before ``user_version``.

        Only the baseline tables existed then.  A file with those tables but
        no recorded version is therefore at least version 1; the ``meta``
        marker may narrow it down further.  A file that does not even have
        the baseline is genuinely empty and starts at 0.
        """
        if not (self._table_exists("runs") and self._table_exists("events")):
            return 0
        if self._table_exists("meta"):
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None:
                try:
                    recorded = int(str(row[0]))
                except (TypeError, ValueError):
                    recorded = 0
                if recorded > 0:
                    return recorded
        return 1

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

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
