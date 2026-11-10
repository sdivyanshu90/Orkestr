"""Content-addressed artifact store.

Artifacts are stored by content hash, which gives three properties for free:

* **Integrity** -- a corrupted blob is detectable by rehashing it.
* **Deduplication** -- two agents that produce the same content share one
  blob.
* **Immutability** -- a blob is never rewritten; publishing new information
  means publishing a new artifact.

The artifact *record* (id, kind, provenance) is kept separately from the blob
so that a manifest can reference many blobs without copying them.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import InfrastructureError, ValidationError
from ..ids import artifact_id
from ..models.artifact import Artifact, ArtifactKind, Provenance, sha256_bytes


@dataclass
class ArtifactStore:
    """Blob store plus manifest directory."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "manifests").mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- paths

    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest[:2] / digest

    def _manifest_path(self, aid: str) -> Path:
        return self.root / "manifests" / f"{aid}.json"

    # ---------------------------------------------------------------- write

    def put(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        provenance: Provenance,
        metadata: dict[str, Any] | None = None,
        media_type: str = "application/octet-stream",
    ) -> Artifact:
        """Store a blob and return its artifact record."""
        digest = sha256_bytes(data)
        blob = self._blob_path(digest)
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists():
            # Write to a temporary file and rename so that a crash cannot
            # leave a partially written blob visible under its final name.
            handle, tmp_name = tempfile.mkstemp(dir=str(blob.parent))
            try:
                with os.fdopen(handle, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, blob)
            except OSError as exc:  # pragma: no cover - filesystem failure
                raise InfrastructureError(f"failed to write artifact blob: {exc}") from exc
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

        artifact = Artifact(
            id=artifact_id(),
            kind=kind,
            uri=str(blob),
            sha256=digest,
            size=len(data),
            media_type=media_type,
            provenance=provenance,
            metadata=metadata or {},
        )
        self._write_manifest(artifact)
        return artifact

    def put_text(
        self,
        text: str,
        *,
        kind: ArtifactKind,
        provenance: Provenance,
        metadata: dict[str, Any] | None = None,
        media_type: str = "text/plain; charset=utf-8",
    ) -> Artifact:
        return self.put(
            text.encode("utf-8"),
            kind=kind,
            provenance=provenance,
            metadata=metadata,
            media_type=media_type,
        )

    def put_json(
        self,
        payload: dict[str, Any] | list[Any],
        *,
        kind: ArtifactKind,
        provenance: Provenance,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        return self.put(
            json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8"),
            kind=kind,
            provenance=provenance,
            metadata=metadata,
            media_type="application/json",
        )

    def _write_manifest(self, artifact: Artifact) -> None:
        path = self._manifest_path(artifact.id)
        path.write_text(
            json.dumps(artifact.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # ----------------------------------------------------------------- read

    def read(self, artifact: Artifact) -> bytes:
        path = Path(artifact.uri)
        if not path.exists():
            raise InfrastructureError(f"artifact blob missing: {artifact.id}")
        data = path.read_bytes()
        if sha256_bytes(data) != artifact.sha256:
            raise InfrastructureError(
                f"artifact blob failed integrity check: {artifact.id}",
                artifact_id=artifact.id,
            )
        return data

    def read_text(self, artifact: Artifact) -> str:
        return self.read(artifact).decode("utf-8", errors="replace")

    def verify(self, artifact: Artifact) -> bool:
        path = Path(artifact.uri)
        if not path.exists():
            return False
        return sha256_bytes(path.read_bytes()) == artifact.sha256

    def load_manifest(self, artifact_id_value: str) -> Artifact | None:
        path = self._manifest_path(artifact_id_value)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return Artifact.model_validate(data)
        except Exception:
            return None

    def list_artifacts(self) -> list[Artifact]:
        out: list[Artifact] = []
        for path in sorted((self.root / "manifests").glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(Artifact.model_validate(data))
            except (OSError, json.JSONDecodeError, ValidationError):
                continue
        return out
