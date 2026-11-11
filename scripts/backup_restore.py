"""Consistent online backup and offline restore for the SQLite state store.

The store runs in WAL mode, so copying ``state.db`` with ``cp`` is not a
backup: uncheckpointed pages live in ``state.db-wal`` and a copy without it
is either stale or inconsistent.  SQLite's backup API takes a consistent
snapshot while the runtime is still writing, which is what this module uses.

A restored file is verified *before* it replaces the target, and the swap is
an atomic ``os.replace``.  A failed or corrupt backup therefore cannot
destroy the only good copy of a run's history.

Usage::

    python scripts/backup_restore.py backup  --database runs/r1/state.db --output /backups/r1.db
    python scripts/backup_restore.py verify  --database /backups/r1.db
    python scripts/backup_restore.py restore --backup /backups/r1.db --database runs/r1/state.db

The runtime must not be writing to the *target* during a restore.  Backups
are safe while it runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent_squad.store.db import SCHEMA_VERSION


class BackupError(RuntimeError):
    """A backup or restore could not be performed safely."""


def _readonly(path: Path) -> sqlite3.Connection:
    """Open ``path`` without giving SQLite any way to write to it."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _integrity(conn: sqlite3.Connection) -> tuple[str, list[str]]:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    integrity = str(row[0]) if row is not None else "no result"
    foreign_keys = [
        str(tuple(row)) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    ]
    return integrity, foreign_keys


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def verify_database(path: str | Path) -> dict[str, Any]:
    """Check that ``path`` is an intact, openable store of a known schema.

    ``PRAGMA integrity_check`` is the database's own page-level audit; a
    backup that fails it is not a backup.  The version check refuses a file
    written by a newer build, because running old code against a newer
    schema can silently write data the new code will misread.
    """
    database = Path(path)
    if not database.is_file():
        raise BackupError(f"database does not exist: {database}")
    try:
        conn = _readonly(database)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open {database}: {exc}") from exc
    try:
        integrity, foreign_keys = _integrity(conn)
        version = _schema_version(conn)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot inspect {database}: {exc}") from exc
    finally:
        conn.close()
    if integrity != "ok":
        raise BackupError(f"integrity check failed for {database}: {integrity}")
    if foreign_keys:
        raise BackupError(f"foreign-key violations in {database}: {foreign_keys[:5]}")
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"{database} has schema version {version}, newer than this build "
            f"({SCHEMA_VERSION}); upgrade agent-squad before restoring it"
        )
    return {
        "database": str(database),
        "schema_version": version,
        "integrity": integrity,
        "foreign_key_violations": len(foreign_keys),
        "size_bytes": database.stat().st_size,
    }


def _snapshot(source: Path, destination: Path) -> None:
    """Copy a consistent snapshot with SQLite's online backup API."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        src = _readonly(source)
        dst = sqlite3.connect(temporary)
        try:
            src.backup(dst)
            # A single-file result is portable; WAL is a property of the
            # live store, not of a backup archive.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            src.close()
            dst.close()
        verify_database(temporary)
        for suffix in ("-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def backup_database(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Take a consistent snapshot of ``source`` at ``destination``.

    The snapshot is verified before it replaces any previous backup with the
    same name, so a failure leaves the old backup intact.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise BackupError(f"database does not exist: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise BackupError("source and backup destination must differ")
    _snapshot(source_path, destination_path)
    result = verify_database(destination_path)
    result["source"] = str(source_path)
    return result


def restore_database(backup: str | Path, target: str | Path) -> dict[str, Any]:
    """Replace ``target`` with a verified copy of ``backup`` atomically.

    Sidecar ``-wal``/``-shm`` files belong to the *old* target and are
    removed before the swap: leaving them in place would make SQLite replay
    a stale write-ahead log against the restored file.
    """
    backup_path = Path(backup)
    target_path = Path(target)
    if backup_path.resolve() == target_path.resolve():
        raise BackupError("backup and restore target must differ")
    verify_database(backup_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(backup_path, target_path.with_name(f".{target_path.name}.restore"))
    restored = target_path.with_name(f".{target_path.name}.restore")
    for suffix in ("-wal", "-shm"):
        Path(f"{target_path}{suffix}").unlink(missing_ok=True)
    os.replace(restored, target_path)
    result = verify_database(target_path)
    result["backup"] = str(backup_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up, verify or restore an agent-squad SQLite store."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backup_parser = sub.add_parser("backup", help="snapshot a live database")
    backup_parser.add_argument("--database", required=True)
    backup_parser.add_argument("--output", required=True)

    verify_parser = sub.add_parser("verify", help="check integrity and schema version")
    verify_parser.add_argument("--database", required=True)

    restore_parser = sub.add_parser("restore", help="atomically replace a database")
    restore_parser.add_argument("--backup", required=True)
    restore_parser.add_argument("--database", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_database(args.database, args.output)
        elif args.command == "verify":
            result = verify_database(args.database)
        else:
            result = restore_database(args.backup, args.database)
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
