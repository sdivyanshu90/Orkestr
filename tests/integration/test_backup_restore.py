"""Backup and restore evidence for the SQLite state store.

A backup is only evidence if it can be restored into a fresh process and the
restored file passes SQLite's own integrity audit.  The round-trip here
writes a real run through ``StateStore``, backs up while the store is live,
mutates the original, restores, and then reopens the restored file to prove
the post-backup mutation is gone and the backed-up history survived.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from agent_squad.store.db import SCHEMA_VERSION, Database
from agent_squad.store.state import StateStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup_restore.py"


def _load_backup_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backup_restore", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["backup_restore"] = module
    spec.loader.exec_module(module)
    return module


backup_restore = _load_backup_module()


def test_backup_restore_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "state.db"
    backup = tmp_path / "backups" / "state.db"

    database = Database(source)
    store = StateStore(database)
    store.create_run("run-before-backup", metadata={"label": "original"})
    # A live backup must include WAL contents, not only the main file.
    report = backup_restore.backup_database(source, backup)
    assert report["integrity"] == "ok"
    assert report["schema_version"] == SCHEMA_VERSION
    assert backup.is_file()

    store.create_run("run-after-backup", metadata={"label": "later"})
    database.close()

    restored = backup_restore.restore_database(backup, source)
    assert restored["integrity"] == "ok"

    reopened = Database(source)
    try:
        store = StateStore(reopened)
        assert store.get_run("run-before-backup") is not None
        assert store.get_run("run-after-backup") is None
        assert reopened.schema_version == SCHEMA_VERSION
    finally:
        reopened.close()


def test_verify_rejects_a_non_database(tmp_path: Path) -> None:
    garbage = tmp_path / "not-a-database.db"
    garbage.write_bytes(b"this is not sqlite")

    with pytest.raises(backup_restore.BackupError):
        backup_restore.verify_database(garbage)


def test_restore_refuses_to_overwrite_its_source(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()

    with pytest.raises(backup_restore.BackupError, match="must differ"):
        backup_restore.restore_database(path, path)
