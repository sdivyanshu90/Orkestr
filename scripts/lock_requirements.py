"""Generate a hash-locked requirements file from the installed environment.

The lock exists so an independent reviewer can install the exact environment
that produced a release and have pip *verify* every artifact, not merely pin
a version string.  Version numbers are read from the distributions actually
installed in ``.venv`` (via :mod:`importlib.metadata`); the hashes are
computed over real wheel archives fetched by pip for each supported CPython
version.  Nothing is hard-coded and no hash is invented.

Why download instead of hashing the installed files?  pip verifies hashes
against the *distribution archive* it downloads, not against the unpacked
files in ``site-packages``.  Hashing installed files would produce a file
that cannot be installed with ``--require-hashes``.

Supported targets default to the CI matrix (CPython 3.11, 3.12 and 3.13 on
manylinux x86-64).  Hashes for every wheel any of those interpreters can
use are included, so one lock serves the whole matrix.

Reproduce::

    .venv/bin/python scripts/lock_requirements.py --output requirements.lock

Verify an existing lock against the installed environment without network
access (downloads are skipped)::

    .venv/bin/python scripts/lock_requirements.py --check requirements.lock
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import tomllib
from collections import deque
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

DEFAULT_PYTHON_VERSIONS = ("3.11", "3.12", "3.13")
DEFAULT_PLATFORMS = (
    "manylinux2014_x86_64",
    "manylinux_2_17_x86_64",
    "manylinux_2_28_x86_64",
)
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Locked:
    """One distribution in the lock, with every archive hash we observed."""

    name: str
    version: str
    hashes: tuple[str, ...]


def _installed() -> dict[str, Distribution]:
    found: dict[str, Distribution] = {}
    for distribution in distributions():
        name = distribution.metadata["Name"]
        if not name:
            continue
        found[canonicalize_name(name)] = distribution
    return found


def _project_roots(pyproject: Path, extras: tuple[str, ...]) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    roots = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        roots.extend(optional.get(extra, []))
    return roots


def _marker_allows(marker: Marker | None, target: str) -> bool:
    """Evaluate a marker for one supported interpreter, ignoring extras."""
    if marker is None:
        return True
    major, minor = target.split(".")
    environment: dict[str, str] = {
        "extra": "",
        "python_version": f"{major}.{minor}",
        "python_full_version": f"{major}.{minor}.0",
        "implementation_name": "cp",
        "platform_python_implementation": "CPython",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "x86_64",
        "os_name": "posix",
    }
    return marker.evaluate(environment)


def _resolve_for_target(
    roots: list[str], installed: dict[str, Distribution], target: str
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    queue: deque[Requirement] = deque(Requirement(root) for root in roots)
    while queue:
        requirement = queue.popleft()
        name = canonicalize_name(requirement.name)
        distribution = installed.get(name)
        if distribution is None:
            raise SystemExit(
                f"{name!r} is required for Python {target} but is not installed in "
                f"{sys.executable}; install the dev extra first"
            )
        version = distribution.version
        if requirement.specifier and not requirement.specifier.contains(
            version, prereleases=True
        ):
            raise SystemExit(
                f"installed {name}=={version} does not satisfy {requirement}"
            )
        if name in resolved:
            continue
        resolved[name] = version
        for raw in distribution.requires or []:
            child = Requirement(raw)
            if _marker_allows(child.marker, target):
                queue.append(child)
    return resolved


def resolve_closure(
    pyproject: Path,
    *,
    extras: tuple[str, ...],
    python_versions: tuple[str, ...],
) -> dict[str, str]:
    """Resolve the installed dependency closure for every supported Python."""
    roots = _project_roots(pyproject, extras)
    installed = _installed()
    closure: dict[str, str] = {}
    for target in python_versions:
        resolved = _resolve_for_target(roots, installed, target)
        for name, version in resolved.items():
            previous = closure.setdefault(name, version)
            if previous != version:
                raise SystemExit(
                    f"{name} resolves to {previous} on one target and {version} on "
                    f"another; pin one version in pyproject.toml"
                )
    return closure


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_hashes(
    closure: dict[str, str],
    *,
    target: str,
    platforms: tuple[str, ...],
    workdir: Path,
) -> dict[str, set[str]]:
    """Fetch real wheels for one interpreter and hash them."""
    destination = workdir / f"py{target}"
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-deps",
        "--only-binary=:all:",
        "--dest",
        str(destination),
        "--python-version",
        target,
        "--implementation",
        "cp",
        "--abi",
        f"cp{target.replace('.', '')}",
    ]
    for platform in platforms:
        command.extend(["--platform", platform])
    command.extend(f"{name}=={version}" for name, version in sorted(closure.items()))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"pip download failed for Python {target}:\n{result.stderr[-4000:]}"
        )

    hashes: dict[str, set[str]] = {}
    for archive in destination.iterdir():
        if archive.suffix != ".whl":
            continue
        name, _version, _build, _tags = parse_wheel_filename(archive.name)
        hashes.setdefault(canonicalize_name(name), set()).add(_sha256(archive))
    missing = sorted(set(closure) - set(hashes))
    if missing:
        raise SystemExit(f"no wheel downloaded for Python {target}: {missing}")
    return hashes


def build_lock(
    pyproject: Path,
    *,
    extras: tuple[str, ...],
    python_versions: tuple[str, ...],
    platforms: tuple[str, ...],
) -> str:
    closure = resolve_closure(
        pyproject, extras=extras, python_versions=python_versions
    )
    hashes: dict[str, set[str]] = {name: set() for name in closure}
    with tempfile.TemporaryDirectory(prefix="agent-squad-lock-") as temporary:
        workdir = Path(temporary)
        for target in python_versions:
            for name, values in _download_hashes(
                closure, target=target, platforms=platforms, workdir=workdir
            ).items():
                hashes[name].update(values)

    host = f"CPython {sys.version.split()[0]} on {sys.platform}"
    lines = [
        "# requirements.lock -- generated file, do not edit by hand.",
        "#",
        "# How this file was produced:",
        "#   - Versions were read from the distributions installed in .venv via",
        f"#     importlib.metadata (generator host: {host}).",
        "#   - sha256 hashes were computed over the real wheel archives fetched by",
        "#     pip for each supported interpreter; no hash is hand-written.",
        f"#   - Interpreter targets: CPython {', '.join(python_versions)}.",
        f"#   - Platform tags: {', '.join(platforms)}.",
        "#   - The dependency closure is pyproject.toml's runtime dependencies plus",
        f"#     the [{', '.join(extras)}] extra.",
        "#",
        "# Reproduce it exactly with:",
        "#   .venv/bin/python scripts/lock_requirements.py --output requirements.lock",
        "#",
        "# Install it (wheel-only, every artifact hash-checked) with:",
        "#   python -m pip install --require-hashes -r requirements.lock",
        "#   python -m pip install --no-deps -e .",
        "",
        "--only-binary=:all:",
        "",
    ]
    for name in sorted(closure):
        values = sorted(hashes[name])
        if not values:
            raise SystemExit(f"no archives hashed for {name}")
        lines.append(f"{name}=={closure[name]} \\")
        for index, value in enumerate(values):
            suffix = "" if index == len(values) - 1 else " \\"
            lines.append(f"    --hash=sha256:{value}{suffix}")
    lines.append("")
    return "\n".join(lines)


def check_lock(path: Path, *, pyproject: Path, extras: tuple[str, ...]) -> None:
    """Verify the lock pins the installed closure and hashes every requirement."""
    closure = resolve_closure(
        pyproject,
        extras=extras,
        python_versions=DEFAULT_PYTHON_VERSIONS,
    )
    joined = path.read_text(encoding="utf-8").replace("\\\n", " ")
    pinned: dict[str, tuple[str, int]] = {}
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        requirement, *options = line.split()
        name, separator, version = requirement.partition("==")
        if not separator:
            raise SystemExit(f"unpinned requirement: {line}")
        hashes = [option for option in options if option.startswith("--hash=sha256:")]
        for option in hashes:
            digest = option.removeprefix("--hash=sha256:")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise SystemExit(f"malformed hash for {name}: {digest}")
        if not hashes:
            raise SystemExit(f"unhashed requirement: {name}")
        pinned[canonicalize_name(name)] = (version, len(hashes))

    if set(pinned) != set(closure):
        missing = sorted(set(closure) - set(pinned))
        extra = sorted(set(pinned) - set(closure))
        raise SystemExit(f"lock drift: missing={missing} extra={extra}")
    for name, version in closure.items():
        locked_version, _count = pinned[name]
        if locked_version != version:
            raise SystemExit(
                f"lock drift: {name} locks {locked_version}, installed {version}"
            )
    print(f"lock is current: {len(pinned)} hashed requirements")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="requirements.lock")
    parser.add_argument("--pyproject", default=str(ROOT / "pyproject.toml"))
    parser.add_argument(
        "--extras",
        default="dev",
        help="comma-separated project extras to include (default: dev)",
    )
    parser.add_argument(
        "--python-versions",
        default=",".join(DEFAULT_PYTHON_VERSIONS),
        help="comma-separated CPython versions to fetch wheels for",
    )
    parser.add_argument(
        "--platforms",
        default=",".join(DEFAULT_PLATFORMS),
        help="comma-separated manylinux platform tags",
    )
    parser.add_argument(
        "--check",
        metavar="LOCKFILE",
        default=None,
        help="verify an existing lock against the installed environment",
    )
    args = parser.parse_args(argv)

    extras = tuple(part.strip() for part in args.extras.split(",") if part.strip())
    if args.check:
        check_lock(Path(args.check), pyproject=Path(args.pyproject), extras=extras)
        return 0

    content = build_lock(
        Path(args.pyproject),
        extras=extras,
        python_versions=tuple(
            part.strip() for part in args.python_versions.split(",") if part.strip()
        ),
        platforms=tuple(
            part.strip() for part in args.platforms.split(",") if part.strip()
        ),
    )
    output = Path(args.output)
    output.write_text(content, encoding="utf-8")
    print(f"wrote {output} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
