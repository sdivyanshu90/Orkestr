"""Isolation profiles.

A profile is a *promise* about the environment a run executes in, and the
runtime checks that the promise can be kept before it starts.  If a required
boundary cannot be established, the run is refused rather than degraded
silently — a hardened profile that quietly runs unhardened is worse than no
profile at all, because it tells the operator something untrue.

Two profiles ship:

``development``
    The default.  Best-effort containment: the worktree, the capability
    layer, the sandbox's resource limits and a stripped environment.  It does
    **not** promise network isolation, and it says so.

``hardened``
    For untrusted repositories.  It requires network isolation, an isolated
    home directory, and no ambient git configuration, and it refuses to run
    when any of those cannot be established.

The distinction is the point: the development profile's limits are stated in
the threat model; the hardened profile's limits are *enforced or refused*.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..config import SandboxConfig
from ..errors import InfrastructureError


class IsolationProfile(str, Enum):
    DEVELOPMENT = "development"
    HARDENED = "hardened"


@dataclass(frozen=True)
class BoundaryRequirement:
    """One thing the environment must guarantee."""

    name: str
    description: str
    available: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "available": self.available,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Profile:
    """What a profile promises."""

    name: IsolationProfile
    requires_network_isolation: bool = False
    requires_isolated_home: bool = False
    requires_no_ambient_git: bool = False
    requires_containment: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.value,
            "requires_network_isolation": self.requires_network_isolation,
            "requires_isolated_home": self.requires_isolated_home,
            "requires_no_ambient_git": self.requires_no_ambient_git,
            "requires_containment": self.requires_containment,
        }


DEVELOPMENT_PROFILE = Profile(
    name=IsolationProfile.DEVELOPMENT,
    requires_network_isolation=False,
    requires_isolated_home=False,
    requires_no_ambient_git=False,
)

HARDENED_PROFILE = Profile(
    name=IsolationProfile.HARDENED,
    requires_network_isolation=True,
    requires_isolated_home=True,
    requires_no_ambient_git=True,
    requires_containment=True,
)

PROFILES: dict[IsolationProfile, Profile] = {
    IsolationProfile.DEVELOPMENT: DEVELOPMENT_PROFILE,
    IsolationProfile.HARDENED: HARDENED_PROFILE,
}


def get_profile(name: str | IsolationProfile) -> Profile:
    if isinstance(name, Profile):
        return name
    try:
        return PROFILES[IsolationProfile(name)]
    except (KeyError, ValueError) as exc:
        raise InfrastructureError(
            f"unknown isolation profile {name!r}; "
            f"known profiles: {sorted(p.value for p in PROFILES)}",
            profile=str(name),
        ) from exc


@dataclass
class IsolationReport:
    """What the guard found."""

    profile: str
    boundaries: list[BoundaryRequirement] = field(default_factory=list)
    satisfied: bool = True

    def missing(self) -> list[str]:
        return [b.name for b in self.boundaries if not b.available]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "satisfied": self.satisfied,
            "missing": self.missing(),
            "boundaries": [b.to_dict() for b in self.boundaries],
        }


class IsolationGuard:
    """Checks that a profile's promises can be kept, before the run starts."""

    def __init__(
        self,
        profile: Profile,
        sandbox: SandboxConfig | None = None,
        *,
        workspace: Path | None = None,
        private_home: Path | None = None,
    ) -> None:
        self.profile = profile
        self.sandbox = sandbox or SandboxConfig()
        self.workspace = workspace
        self.private_home = private_home
        self._probe_report: Any = None

    # --------------------------------------------------------- the checks

    def _network_isolation_available(self) -> tuple[bool, str]:
        if self.sandbox.allow_network:
            return False, "the sandbox configuration allows network access"
        unshare = shutil.which("unshare")
        if not unshare:
            return False, "unshare is not installed"
        try:
            proc = subprocess.run(
                [unshare, "--net", "--", "true"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"unshare could not be run: {exc}"
        if proc.returncode != 0:
            return False, (
                "unshare --net is not permitted on this host "
                f"({proc.stderr.decode(errors='replace').strip()[:120]})"
            )
        return True, "unshare --net is available"

    def _isolated_home_available(self) -> tuple[bool, str]:
        """The private home must exist, and must not be the user's home.

        The previous implementation of this check returned ``True``
        unconditionally on the grounds that "the sandbox rewrites HOME for
        every command".  That was false: the environment builder kept the
        invoking user's ``HOME``, so the check asserted a boundary the
        runtime did not have.  It now inspects the actual configuration
        and the actual directory.
        """
        configured = (self.sandbox.private_home or "").strip()
        if not configured:
            return False, (
                "the run has no private home directory, so every command would "
                "inherit the invoking user's home"
            )
        home = Path(configured)
        if not home.is_dir():
            return False, f"the private home directory does not exist: {home}"
        user_home = os.environ.get("HOME", "")
        if user_home and home.resolve() == Path(user_home).resolve():
            return False, (
                "the private home is the invoking user's home, which provides "
                "no isolation at all"
            )
        return True, f"the private home is {home}"

    def _no_ambient_git_available(self) -> tuple[bool, str]:
        git = shutil.which("git")
        if not git:
            return False, "git is not installed"
        return True, "the sandbox disables ambient git configuration"

    def _containment_available(self) -> tuple[bool, str]:
        """The containment backend must be present *and* work.

        Presence is not capability.  The backend is exercised with a real
        command and the observations decide, so a host on which bubblewrap
        is installed but blocked by the kernel is refused rather than
        trusted.
        """
        from ..sandbox.containment import find_backend

        backend = find_backend()
        if backend is None:
            return False, (
                "neither bubblewrap nor unshare is installed, so no execution "
                "containment can be established"
            )
        if backend != "bubblewrap":
            return False, (
                f"the only available backend is {backend!r}, which isolates the "
                "network and the PID namespace but leaves the filesystem "
                "intact; the hardened profile requires a filesystem boundary"
            )
        return True, "bubblewrap is installed"

    # ------------------------------------------------------------- the check

    def inspect(self) -> IsolationReport:
        """Report on each required boundary without failing."""
        report = IsolationReport(profile=self.profile.name.value)
        checks = (
            ("network_isolation", self.profile.requires_network_isolation, self._network_isolation_available),
            ("isolated_home", self.profile.requires_isolated_home, self._isolated_home_available),
            ("no_ambient_git", self.profile.requires_no_ambient_git, self._no_ambient_git_available),
            ("execution_containment", self.profile.requires_containment, self._containment_available),
        )
        for name, required, check in checks:
            if not required:
                report.boundaries.append(
                    BoundaryRequirement(
                        name=name,
                        description="not required by this profile",
                        available=True,
                        detail="",
                    )
                )
                continue
            available, detail = check()
            report.boundaries.append(
                BoundaryRequirement(
                    name=name,
                    description=f"the {self.profile.name.value} profile requires {name}",
                    available=available,
                    detail=detail,
                )
            )
            if not available:
                report.satisfied = False
        return report

    async def probe(self) -> Any:
        """Execute a real command inside the boundary and report what it saw.

        This is the difference between *claiming* a boundary and
        *demonstrating* one.  The probe runs the same argument builder the
        worker's commands use, so a passing probe is evidence about the
        thing workers actually run.
        """
        from ..sandbox.containment import probe as run_probe

        if self.workspace is None or self.private_home is None:
            self._probe_report = None
            return None
        backend = self.sandbox.containment or "none"
        if backend == "auto":
            from ..sandbox.containment import find_backend

            backend = find_backend() or "none"
        if backend == "none":
            self._probe_report = None
            return None
        canary = self.private_home.parent / "host-canary.txt"
        try:
            canary.write_text("agent-squad-host-canary", encoding="utf-8")
        except OSError:
            canary = self.private_home
        from ..sandbox.limits import build_environment

        report = await run_probe(
            backend,
            workspace=self.workspace,
            private_home=self.private_home,
            canary_path=canary,
            env=build_environment(self.sandbox),
            extra_read_write=tuple(Path(p) for p in self.sandbox.extra_bind_paths),
        )
        self._probe_report = report
        return report

    def check(self) -> IsolationReport:
        """Enforce the profile, refusing to run if a boundary is unavailable."""
        report = self.inspect()
        if not report.satisfied:
            missing = ", ".join(report.missing())
            details = "; ".join(
                f"{b.name}: {b.detail}" for b in report.boundaries if not b.available
            )
            raise InfrastructureError(
                f"the {self.profile.name.value} isolation profile requires "
                f"{missing}, which this host cannot provide ({details}). "
                "Refusing to run rather than degrading silently; use the "
                "development profile if that is acceptable.",
                profile=self.profile.name.value,
                missing=report.missing(),
                details=details,
            )
        return report


def hardened_environment(sandbox: SandboxConfig) -> dict[str, str]:
    """The environment hardening the hardened profile relies on.

    These are *enforced* settings, not prompt instructions: a repository
    cannot opt out of them, because the orchestrator sets them for every
    command it runs.
    """
    env = {
        # No ambient git configuration: a repository's .gitconfig, and the
        # user's global one, cannot inject aliases, hooks or credential
        # helpers.
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        # No hooks: a repository's hooks are executable code that would run
        # with the orchestrator's privileges.
        "GIT_CONFIG_COUNT": "0",
        # No package manager reaching the network behind the sandbox's back.
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "npm_config_offline": "true",
        "CARGO_NET_OFFLINE": "true",
        # No proxy: a proxy would defeat the network boundary.
        "no_proxy": "*",
        "NO_PROXY": "*",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
    }
    return env
