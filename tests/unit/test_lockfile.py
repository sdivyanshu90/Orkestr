"""The dependency lock is complete, pinned and hash-checked.

The lock is evidence only if pip would accept it under
``--require-hashes``: every requirement needs an exact version and at least
one sha256.  This test also re-resolves the installed closure, so a package
added to ``pyproject.toml`` without regenerating the lock fails here rather
than on a reviewer's clean machine.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "requirements.lock"
SCRIPT = ROOT / "scripts" / "lock_requirements.py"

REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^ ]+)"
    r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})+)"
)


def _load_lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lock_requirements", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lock_requirements"] = module
    spec.loader.exec_module(module)
    return module


def _lock_lines() -> list[tuple[str, str, list[str]]]:
    text = LOCK.read_text(encoding="utf-8").replace("\\\n", " ")
    parsed: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        match = REQUIREMENT.match(line)
        assert match is not None, f"not a hash-locked requirement: {line!r}"
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", match.group("hashes"))
        parsed.append(
            (canonicalize_name(match.group("name")), match.group("version"), hashes)
        )
    return parsed


def test_every_requirement_is_pinned_and_hashed() -> None:
    entries = _lock_lines()
    assert entries, "the lock must not be empty"
    names = [name for name, _version, _hashes in entries]
    assert len(names) == len(set(names)), "duplicate requirements in the lock"
    for name, version, hashes in entries:
        assert version and version not in {"*", "latest"}, name
        assert hashes, f"{name} has no hash"
        assert len(set(hashes)) == len(hashes), f"{name} repeats a hash"


def test_lock_covers_the_supported_interpreter_matrix() -> None:
    """Compiled packages must carry a hash per supported CPython."""
    entries = {name: hashes for name, _version, hashes in _lock_lines()}
    for compiled in ("pydantic-core", "mypy"):
        assert len(entries[compiled]) >= 3, (
            f"{compiled} should have a wheel hash for CPython 3.11/3.12/3.13"
        )


def test_header_documents_reproduction() -> None:
    header = "\n".join(
        line for line in LOCK.read_text(encoding="utf-8").splitlines()[:20]
    )
    assert "scripts/lock_requirements.py" in header
    assert "importlib.metadata" in header
    assert "sha256" in header
    assert "--require-hashes" in header


def test_lock_matches_the_installed_closure() -> None:
    module = _load_lock_module()
    module.check_lock(
        LOCK,
        pyproject=ROOT / "pyproject.toml",
        extras=("dev",),
    )
