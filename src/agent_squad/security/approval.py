"""Approval binding.

An approval is a decision about *one specific action*: this command, in
this workspace, for this request version, expiring at this time.  A bare
boolean "approved: true" cannot express that, and it is exactly the kind of
loose grant that turns into a privilege problem the moment a plan changes.

The binding is a content hash over:

* the action name,
* the exact arguments,
* the workspace the action applies to,
* the contract (request) hash the action was approved against,
* the request version.

If any of those change, the grant no longer matches and the action is
refused again.  Replanning the request, or a model asking to push a
different branch, invalidates the previous approval automatically — not
because something remembered to check, but because the hash no longer
matches.

Three properties make the grant harder to misuse than a plain flag:

* **It expires.**  An approval without a stated expiry expires after
  :data:`DEFAULT_APPROVAL_TTL_S` rather than never; a decision made
  yesterday must not authorise today's effect.
* **It is consumed once.**  :meth:`ApprovalGrant.consume` marks a grant as
  used, so a captured grant cannot be replayed for a second effect.
* **It names the run.**  The binding carries the run it was issued in, so
  a grant cannot be carried from one run into another even when the
  workspace happens to look alike.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models.task import utcnow

#: How long an approval lives when the request did not state an expiry.
#:
#: The point is not the number; it is that "no expiry" is not a grant
#: that lives forever.  A decision is a statement about the moment it was
#: made, and the effect it authorised.
DEFAULT_APPROVAL_TTL_S = 3600.0


class ApprovalBinding(BaseModel):
    """What an approval is a decision about."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str
    arguments: tuple[str, ...] = ()
    workspace: str = ""
    contract_hash: str = ""
    request_version: int = 1
    run_id: str = ""
    """The run the approval was issued in.

    The workspace already binds an approval to a run -- every run has its
    own worktrees -- but the run id says so explicitly, so a grant cannot
    be carried into another run by accident and so the record says which
    run it belongs to.
    """

    @property
    def binding_hash(self) -> str:
        payload = {
            "action": self.action,
            "arguments": list(self.arguments),
            "workspace": self.workspace,
            "contract_hash": self.contract_hash,
            "request_version": self.request_version,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "arguments": list(self.arguments),
            "workspace": self.workspace,
            "contract_hash": self.contract_hash,
            "request_version": self.request_version,
            "run_id": self.run_id,
            "binding_hash": self.binding_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ApprovalBinding:
        if not data:
            return cls(action="")
        return cls(
            action=str(data.get("action") or ""),
            arguments=tuple(str(a) for a in (data.get("arguments") or ())),
            workspace=str(data.get("workspace") or ""),
            contract_hash=str(data.get("contract_hash") or ""),
            request_version=int(data.get("request_version") or 1),
            run_id=str(data.get("run_id") or ""),
        )


class ApprovalGrant(BaseModel):
    """A recorded human decision, bound to the action it authorises."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    binding: ApprovalBinding
    approved: bool
    decided_by: str = ""
    reason: str = ""
    decided_at: _dt.datetime = Field(default_factory=utcnow)
    expires_at: float | None = None
    consumed: bool = False
    """Whether the grant has already authorised an effect.

    A grant is a capability, and a capability that can be replayed is not
    a capability: it is a standing permission.  :meth:`consume` marks it,
    and a second attempt to use it is refused.
    """

    @property
    def expiry(self) -> float:
        """When the grant stops being valid."""
        if self.expires_at is not None:
            return self.expires_at
        decided = self.decided_at
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=_dt.UTC)
        return decided.timestamp() + DEFAULT_APPROVAL_TTL_S

    @property
    def expired(self) -> bool:
        return time.time() > self.expiry

    def authorises(self, candidate: ApprovalBinding) -> tuple[bool, str]:
        """Whether this grant authorises ``candidate``.

        Returns ``(allowed, reason)``.  The reason is always present so a
        refusal can be explained rather than merely enforced.
        """
        if not self.approved:
            return False, "the action was not approved"
        if self.expired:
            return False, "the approval has expired"
        if self.consumed:
            return False, "the approval has already been used for another effect"
        if (
            self.binding.run_id
            and candidate.run_id
            and self.binding.run_id != candidate.run_id
        ):
            return False, (
                f"the approval was granted in run {self.binding.run_id!r} and "
                f"cannot authorise an effect in run {candidate.run_id!r}"
            )
        if self.binding.binding_hash != candidate.binding_hash:
            return False, (
                "the approval was granted for a different action "
                f"(approved: {self.binding.action} {list(self.binding.arguments)}; "
                f"requested: {candidate.action} {list(candidate.arguments)})"
            )
        return True, "the approval matches the action"

    def consume(self, candidate: ApprovalBinding) -> tuple[bool, str]:
        """Authorise ``candidate`` and mark the grant as used.

        This is the single-use form of :meth:`authorises`: a grant
        authorises exactly one effect, once.  Replaying it afterwards is
        refused.
        """
        allowed, reason = self.authorises(candidate)
        if allowed:
            self.consumed = True
        return allowed, reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "binding": self.binding.to_dict(),
            "approved": self.approved,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat()
            if hasattr(self.decided_at, "isoformat")
            else str(self.decided_at),
            "expires_at": self.expires_at,
            "expiry": self.expiry,
            "expired": self.expired,
            "consumed": self.consumed,
        }
