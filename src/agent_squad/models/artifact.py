"""Artifacts and their provenance.

Artifacts are the only channel through which large payloads move between
agents.  Chat-style hand-waving is explicitly *not* a valid transfer
mechanism: an agent that "knows" a file changed must point at an artifact id
whose content hash can be checked.

Every artifact records provenance (who produced it, from which task attempt,
against which base revision) so that a later reviewer can decide whether the
evidence applies to the current tree state.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .task import utcnow


class ArtifactKind(str, Enum):
    PATCH = "patch"
    FILE = "file"
    LOG = "log"
    TEST_REPORT = "test_report"
    REVIEW = "review"
    REPORT = "report"
    EVIDENCE = "evidence"
    CONTEXT = "context"
    MANIFEST = "manifest"
    BUNDLE = "bundle"


class Provenance(BaseModel):
    """Who produced an artifact, when, and against which source state."""

    model_config = ConfigDict(extra="forbid")

    producer: str
    run_id: str
    task_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    created_at: _dt.datetime = Field(default_factory=utcnow)
    model: str | None = None
    provider: str | None = None
    base_revision: str | None = None
    parent_artifact_ids: tuple[str, ...] = ()
    tool_calls: tuple[str, ...] = ()


class Artifact(BaseModel):
    """A content-addressed blob stored outside the chat transcript."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    kind: ArtifactKind
    uri: str
    sha256: str
    size: int = Field(default=0, ge=0)
    media_type: str = "application/octet-stream"
    provenance: Provenance
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_hash(self) -> Artifact:
        if len(self.sha256) != 64:
            raise ValueError("sha256 must be a 64 character hex digest")
        return self

    def verify(self, content: bytes) -> bool:
        """Recompute the digest of ``content`` and compare with the record."""
        return hashlib.sha256(content).hexdigest() == self.sha256


class FileManifestEntry(BaseModel):
    """One file touched by a patch."""

    model_config = ConfigDict(extra="forbid")

    path: str
    status: str = Field(description="added | modified | deleted | renamed")
    added_lines: int = 0
    removed_lines: int = 0
    sha256_before: str | None = None
    sha256_after: str | None = None


class PatchManifest(BaseModel):
    """Machine-readable description of the change a worker produced."""

    model_config = ConfigDict(extra="forbid")

    base_revision: str
    head_revision: str | None = None
    entries: tuple[FileManifestEntry, ...] = ()
    patch_artifact_id: str | None = None
    patch_sha256: str | None = None
    files: tuple[str, ...] = ()

    def touched_paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
