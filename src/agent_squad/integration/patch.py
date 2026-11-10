"""Patch representation for the integration engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models.artifact import Artifact


@dataclass
class PatchArtifact:
    """A captured change, ready to be integrated."""

    artifact_id: str
    task_id: str
    base_revision: str
    sha256: str
    content: str
    files: tuple[str, ...] = ()
    producer: str = ""
    head_revision: str | None = None
    base_tree: str = ""
    head_tree: str = ""

    @classmethod
    def from_artifact(
        cls, artifact: Artifact, *, task_id: str, files: tuple[str, ...] = ()
    ) -> PatchArtifact:
        return cls(
            artifact_id=artifact.id,
            task_id=task_id,
            base_revision=artifact.provenance.base_revision or "",
            sha256=artifact.sha256,
            content="",
            files=files,
            producer=artifact.provenance.producer,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "base_revision": self.base_revision,
            "sha256": self.sha256,
            "files": list(self.files),
            "producer": self.producer,
            "head_revision": self.head_revision,
        }


@dataclass
class ConflictRecord:
    """A conflict detected while integrating patches."""

    task_id: str
    artifact_id: str
    kind: str
    files: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "files": list(self.files),
            "detail": self.detail,
        }


@dataclass
class IntegrationResult:
    """The outcome of an integration attempt."""

    passed: bool
    applied: list[str] = field(default_factory=list)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    workspace: str | None = None
    commit: str | None = None
    check_output: str = ""
    error: str | None = None

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "applied": list(self.applied),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "stale": list(self.stale),
            "rejected": list(self.rejected),
            "workspace": self.workspace,
            "commit": self.commit,
            "error": self.error,
        }
