"""Approval binding.

An approval is a decision about *one specific action*: this command, in
this workspace, for this request version, expiring at this time.  A bare
boolean "approved: true" cannot express that, and it is exactly the kind of
loose grant that turns into a privilege problem the moment a plan changes.

The binding is a content hash over:

* the action name,
* the exact arguments,
* the workspace the action applies to,
* the contract (request) hash the action was approved against.

If any of those change, the grant no longer matches and the action is
refused again.  Replanning the request, or a model asking to push a
different branch, invalidates the previous approval automatically — not
because something remembered to check, but because the hash no longer
matches.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models.task import utcnow


class ApprovalBinding(BaseModel):
    """What an approval is a decision about."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str
    arguments: tuple[str, ...] = ()
    workspace: str = ""
    contract_hash: str = ""
    request_version: int = 1

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

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def authorises(self, candidate: ApprovalBinding) -> tuple[bool, str]:
        """Whether this grant authorises ``candidate``.

        Returns ``(allowed, reason)``.  The reason is always present so a
        refusal can be explained rather than merely enforced.
        """
        if not self.approved:
            return False, "the action was not approved"
        if self.expired:
            return False, "the approval has expired"
        if self.binding.binding_hash != candidate.binding_hash:
            return False, (
                "the approval was granted for a different action "
                f"(approved: {self.binding.action} {list(self.binding.arguments)}; "
                f"requested: {candidate.action} {list(candidate.arguments)})"
            )
        return True, "the approval matches the action"

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
            "expired": self.expired,
        }
