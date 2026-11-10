"""Artifact provenance and invalidation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import two_independent_spec

from agent_squad.artifacts.store import ArtifactStore
from agent_squad.config import RunConfig
from agent_squad.errors import InfrastructureError
from agent_squad.models.artifact import ArtifactKind, Provenance
from agent_squad.orchestrator import Orchestrator
from agent_squad.store.db import Database
from agent_squad.store.events import EventLog
from agent_squad.store.state import StateStore


def provenance(run_id: str, base: str) -> Provenance:
    return Provenance(
        producer="implementer:t",
        run_id=run_id,
        task_id="t",
        base_revision=base,
    )


def test_an_artifact_is_stored_by_content_hash(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_text("hello", kind=ArtifactKind.FILE, provenance=provenance("r", "abc"))
    assert artifact.sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert store.verify(artifact)
    assert store.read_text(artifact) == "hello"


def test_identical_content_is_stored_once(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_text("same", kind=ArtifactKind.FILE, provenance=provenance("r", "a"))
    second = store.put_text("same", kind=ArtifactKind.FILE, provenance=provenance("r", "b"))
    assert first.sha256 == second.sha256
    assert first.id != second.id
    blobs = list((tmp_path / "artifacts" / "blobs").rglob("*"))
    assert len([b for b in blobs if b.is_file()]) == 1


def test_a_corrupted_blob_fails_its_integrity_check(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_text("hello", kind=ArtifactKind.FILE, provenance=provenance("r", "a"))
    Path(artifact.uri).write_text("tampered", encoding="utf-8")
    assert not store.verify(artifact)
    with pytest.raises(InfrastructureError):
        store.read(artifact)


def test_manifests_are_readable_after_a_restart(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_text("payload", kind=ArtifactKind.FILE, provenance=provenance("r", "a"))
    reopened = ArtifactStore(tmp_path / "artifacts")
    loaded = reopened.load_manifest(artifact.id)
    assert loaded is not None
    assert loaded.sha256 == artifact.sha256


def test_artifacts_from_another_base_revision_are_invalidated(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    state = StateStore(db, EventLog(db))
    state.create_run("r1")
    store = ArtifactStore(tmp_path / "artifacts")
    old = store.put_text("old", kind=ArtifactKind.PATCH, provenance=provenance("r1", "aaa"))
    new = store.put_text("new", kind=ArtifactKind.PATCH, provenance=provenance("r1", "bbb"))
    for artifact in (old, new):
        state.save_artifact(artifact)

    invalidated = state.invalidate_stale_artifacts("r1", current_revision="bbb")
    assert invalidated == [old.id]
    assert state.get_artifact(old.id).metadata["invalidated"] is True
    assert state.get_artifact(new.id).metadata.get("invalidated") is None
    db.close()


@pytest.mark.integration
async def test_run_artifacts_record_their_base_revision(
    tmp_path: Path, repo: Path
) -> None:
    config = RunConfig(
        run_id="run_artifacts",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        await orchestrator.run("artifacts", request_spec=two_independent_spec())
        artifacts = orchestrator.state.list_artifacts(config.run_id)
    finally:
        await orchestrator.aclose()

    patches = [a for a in artifacts if a.kind is ArtifactKind.PATCH]
    assert len(patches) == 2
    for patch in patches:
        assert patch.provenance.base_revision
        assert patch.provenance.task_id
        assert patch.provenance.attempt >= 1
        assert patch.metadata["base_tree"]
