"""Capability vocabulary and role grants.

Capabilities are the *only* authority in this system.  A model can request a
tool call; it cannot grant itself the ability to make one.  The capability
set for a role is fixed in code (not in a prompt, not in model output), and
every tool invocation is checked against it before execution.

The vocabulary is intentionally small and coarse.  Fine-grained capabilities
would multiply the number of ways the policy could be wrong without
meaningfully improving security.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..models.task import Role


class Capability:
    """Namespace of capability identifiers."""

    FS_READ = "fs:read"
    FS_WRITE = "fs:write"
    FS_DELETE = "fs:delete"
    EXEC_RUN = "exec:run"
    EXEC_GIT = "exec:git"
    NET_HTTP = "net:http"
    GIT_COMMIT = "git:commit"
    GIT_PUSH = "git:push"
    ARTIFACT_WRITE = "artifact:write"
    APPROVAL_REQUEST = "approval:request"
    BROADCAST = "broadcast"


ALL_CAPABILITIES: frozenset[str] = frozenset(
    {
        Capability.FS_READ,
        Capability.FS_WRITE,
        Capability.FS_DELETE,
        Capability.EXEC_RUN,
        Capability.EXEC_GIT,
        Capability.NET_HTTP,
        Capability.GIT_COMMIT,
        Capability.GIT_PUSH,
        Capability.ARTIFACT_WRITE,
        Capability.APPROVAL_REQUEST,
        Capability.BROADCAST,
    }
)


@dataclass(frozen=True)
class CapabilitySet:
    """An immutable set of granted capabilities."""

    granted: frozenset[str] = frozenset()

    @classmethod
    def of(cls, capabilities: Iterable[str]) -> CapabilitySet:
        return cls(frozenset(capabilities))

    def allows(self, capability: str) -> bool:
        return capability in self.granted

    def allows_all(self, capabilities: Iterable[str]) -> bool:
        return all(self.allows(c) for c in capabilities)

    def missing(self, capabilities: Iterable[str]) -> list[str]:
        return sorted(c for c in capabilities if not self.allows(c))

    def union(self, other: CapabilitySet) -> CapabilitySet:
        return CapabilitySet(self.granted | other.granted)

    def intersect(self, other: CapabilitySet) -> CapabilitySet:
        return CapabilitySet(self.granted & other.granted)

    def without(self, *capabilities: str) -> CapabilitySet:
        return CapabilitySet(self.granted - frozenset(capabilities))

    def __contains__(self, capability: object) -> bool:
        return isinstance(capability, str) and capability in self.granted

    def __iter__(self):
        return iter(sorted(self.granted))

    def __len__(self) -> int:
        return len(self.granted)

    def sorted(self) -> list[str]:
        return sorted(self.granted)


#: Default grants per role.  These are the *minimum* a role needs; a
#: configuration may grant more, but never removes the ability of a worker to
#: report a failure or request approval.
DEFAULT_ROLE_CAPABILITIES: dict[Role, CapabilitySet] = {
    Role.SUPERVISOR: CapabilitySet.of(
        {
            Capability.FS_READ,
            Capability.EXEC_RUN,
            Capability.EXEC_GIT,
            Capability.ARTIFACT_WRITE,
            Capability.BROADCAST,
            Capability.APPROVAL_REQUEST,
        }
    ),
    Role.IMPLEMENTER: CapabilitySet.of(
        {
            Capability.FS_READ,
            Capability.FS_WRITE,
            Capability.EXEC_RUN,
            Capability.EXEC_GIT,
            Capability.ARTIFACT_WRITE,
            Capability.BROADCAST,
            Capability.APPROVAL_REQUEST,
            # Committing and pushing are *capability*-granted but
            # *approval*-gated: the human authorisation is the control that
            # matters, not the absence of the capability.
            Capability.GIT_COMMIT,
            Capability.GIT_PUSH,
        }
    ),
    Role.TEST_ENGINEER: CapabilitySet.of(
        {
            Capability.FS_READ,
            Capability.FS_WRITE,
            Capability.EXEC_RUN,
            Capability.EXEC_GIT,
            Capability.ARTIFACT_WRITE,
            Capability.BROADCAST,
            Capability.APPROVAL_REQUEST,
        }
    ),
    Role.REVIEWER: CapabilitySet.of(
        {
            Capability.FS_READ,
            Capability.EXEC_RUN,
            Capability.EXEC_GIT,
            Capability.ARTIFACT_WRITE,
            Capability.BROADCAST,
        }
    ),
    # The integration controller is an ordinary software component, not an
    # LLM agent; it holds no model-driven capabilities at all.
    Role.INTEGRATION: CapabilitySet.of({Capability.FS_READ, Capability.FS_WRITE}),
}


def capabilities_for_role(role: Role, extra: Iterable[str] = ()) -> CapabilitySet:
    """Capabilities for a role, optionally widened by configuration."""
    base = DEFAULT_ROLE_CAPABILITIES.get(role, CapabilitySet.of({Capability.FS_READ}))
    unknown = set(extra) - ALL_CAPABILITIES
    if unknown:
        raise ValueError(f"unknown capabilities requested: {sorted(unknown)}")
    return base.union(CapabilitySet.of(extra))
