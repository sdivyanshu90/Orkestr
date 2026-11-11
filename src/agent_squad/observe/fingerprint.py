"""The environment fingerprint.

A measurement is only comparable with another measurement if the
environment was the same.  The fingerprint records what the environment
*was* — the interpreter, the platform, the dependency set, the model, the
configuration — so that a comparison across two runs can be checked rather
than assumed.

It is a content hash of a small, explicit set of facts, not a snapshot of
the whole machine: the point is reproducibility, and the fingerprint has to
be small enough to record with every result.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EnvironmentFingerprint:
    """What the environment was when a measurement was taken."""

    python_version: str = ""
    platform: str = ""
    machine: str = ""
    processor: str = ""
    provider: str = ""
    model: str = ""
    config_hash: str = ""
    dependency_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """A content hash over the fingerprint's fields."""
        payload = self._fields()
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _fields(self) -> dict[str, Any]:
        return {
            "python_version": self.python_version,
            "platform": self.platform,
            "machine": self.machine,
            "processor": self.processor,
            "provider": self.provider,
            "model": self.model,
            "config_hash": self.config_hash,
            "dependency_hash": self.dependency_hash,
            **self.extra,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._fields(), "fingerprint": self.fingerprint}


def _dependency_hash() -> str:
    """A hash of the installed distribution set."""
    try:
        from importlib.metadata import distributions

        names = sorted(
            f"{d.metadata['Name']}=={d.version}"
            for d in distributions()
            if d.metadata.get("Name")
        )
    except Exception:  # pragma: no cover - defensive
        return "unknown"
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


def capture(
    *,
    provider: str = "",
    model: str = "",
    config: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> EnvironmentFingerprint:
    """Capture the current environment."""
    config_blob = json.dumps(config or {}, sort_keys=True, default=str)
    return EnvironmentFingerprint(
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        machine=platform.machine(),
        processor=platform.processor(),
        provider=provider,
        model=model,
        config_hash=hashlib.sha256(config_blob.encode("utf-8")).hexdigest(),
        dependency_hash=_dependency_hash(),
        extra=dict(extra or {}),
    )
