"""Schema migration evidence for the SQLite store.

The tests build a database exactly as an older release left it -- baseline
tables present, ``PRAGMA user_version`` still 0 -- and prove that opening it
upgrades in place without losing data.  They also prove the two properties
that make migrations safe to run on every open: they are ordered and
idempotent, and a file from a newer build is refused instead of downgraded.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_squad.errors import InfrastructureError
from agent_squad.store.db import MIGRATIONS, SCHEMA_VERSION, Database


def _baseline_sql() -> str:
    return next(migration.sql for migration in MIGRATIONS if migration.version == 1)


def _make_legacy_database(path: Path) -> None:
    """Create a pre-migration file: baseline schema, version still unset."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_baseline_sql())
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
        )
        conn.execute(
            "INSERT INTO runs(run_id, status, created_at, updated_at) "
            "VALUES('legacy-run', 'created', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == 0
        )  # the pre-migration state
    finally:
        conn.close()


def _index_names(database: Database) -> set[str]:
    return {
        str(row["name"])
        for row in database.query(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }


def test_fresh_database_is_created_at_current_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.db")
    try:
        assert database.schema_version == SCHEMA_VERSION
        assert SCHEMA_VERSION >= 2
        assert "idx_events_run_type" in _index_names(database)
    finally:
        database.close()


def test_upgrade_from_older_version_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _make_legacy_database(path)

    database = Database(path)
    try:
        assert database.schema_version == SCHEMA_VERSION
        assert "idx_events_run_type" in _index_names(database)
        run = database.query_one("SELECT run_id FROM runs WHERE run_id = 'legacy-run'")
        assert run is not None, "migration must not drop existing rows"
    finally:
        database.close()


def test_migrate_twice_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"
    database = Database(path)
    try:
        first = database.schema_version
        database.migrate()
        database.migrate()
        assert database.schema_version == first
        indexes = [
            row
            for row in database.query(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_events_run_type'"
            )
        ]
        assert len(indexes) == 1
    finally:
        database.close()

    reopened = Database(path)
    try:
        reopened.migrate()
        assert reopened.schema_version == first
    finally:
        reopened.close()


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    database = Database(path)
    database.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(InfrastructureError, match="newer than this build"):
        Database(path)
