"""The request contract and the acceptance boundary.

The problem this solves
-----------------------

A plan is *proposed*; the request is *owned*.  If the acceptance criteria
come from the same model that does the work, then a model that produces the
wrong thing can also produce a check that the wrong thing satisfies.  Worse,
a check can be satisfied by state that was already there: a task that says
"make sure `tests/test_calc.py` exists" passes instantly in a repository
where that file has always existed, and the run reports success having done
nothing at all.

That is a **false success**, and it is the single most dangerous failure mode
in this system, because it is silent.  The defence has three parts:

1. **The contract is immutable.**  :class:`RequestContract` holds the
   owner's required outcomes.  A plan may *add* supplementary checks; it may
   not remove or weaken a required one.
2. **The verdict is computed against the final candidate tree**, not against
   a worker's own worktree, and it is bound to the contract hash, the base
   revision, the candidate tree hash, the verifier version and the commands
   that were executed.
3. **A pre-satisfied outcome is not evidence.**  Each required outcome is
   evaluated against *both* the base tree and the candidate tree.  An outcome
   that already held before the work started proves nothing about the work,
   unless the contract explicitly permits a no-op.

The outcome vocabulary is explicit -- accepted, rejected, inconclusive,
timed out, cancelled -- because "the run finished" and "the request was
satisfied" are different claims.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .task import AcceptanceCriterion, RepoScope, utcnow


class VerificationOutcome(str, Enum):
    """The result of judging a run against its contract.

    These are deliberately distinct states.  A run that produced no evidence
    is **inconclusive**, not accepted.
    """

    ACCEPTED = "accepted"
    """Every required outcome is established by the final candidate tree."""

    REJECTED = "rejected"
    """At least one required outcome is not satisfied by the candidate."""

    INCONCLUSIVE = "inconclusive"
    """The evidence is insufficient: pre-satisfied outcomes, an unverifiable
    criterion, or no independent check at all."""

    TIMED_OUT = "timed_out"
    """The run exceeded its deadline."""

    CANCELLED = "cancelled"
    """The run was cancelled."""


class OutcomeState(str, Enum):
    """How a single required outcome was judged."""

    ESTABLISHED = "established"
    """Failed on the base tree and passes on the candidate: the work caused it."""

    PRE_SATISFIED = "pre_satisfied"
    """Already true before the work started: proves nothing about the work."""

    NOT_SATISFIED = "not_satisfied"
    """Fails on the candidate tree."""

    UNVERIFIABLE = "unverifiable"
    """The check could not be executed."""

    NOT_ALLOWED = "not_allowed"
    """The contract does not permit this kind of pre-satisfied outcome."""


class RequestContract(BaseModel):
    """The owner's immutable statement of what must be true when done.

    The contract is hashed so that every piece of evidence can be bound to
    the exact request it was produced for.  Any change to the contract
    changes the hash and invalidates the evidence.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    version: int = Field(default=1, ge=1)
    objective: str
    required_outcomes: tuple[AcceptanceCriterion, ...] = ()
    scope: RepoScope = Field(default_factory=RepoScope)
    allow_pre_satisfied: bool = Field(
        default=False,
        description=(
            "Whether a required outcome that was already true before the work "
            "started may count as satisfied.  This is the explicit permission "
            "for a legitimate no-op, and it is off by default."
        ),
    )
    notes: str = ""

    @model_validator(mode="after")
    def _validate(self) -> RequestContract:
        if not self.objective.strip():
            raise ValueError("a contract must state an objective")
        ids = [c.id for c in self.required_outcomes]
        if len(set(ids)) != len(ids):
            raise ValueError("a contract cannot have duplicate outcome ids")
        return self

    @property
    def contract_hash(self) -> str:
        """A content hash of the contract, excluding the hash itself."""
        payload = {
            "version": self.version,
            "objective": self.objective,
            "required_outcomes": [c.model_dump(mode="json") for c in self.required_outcomes],
            "scope": self.scope.model_dump(mode="json"),
            "allow_pre_satisfied": self.allow_pre_satisfied,
            "notes": self.notes,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def with_supplementary(
        self, extra: tuple[AcceptanceCriterion, ...]
    ) -> RequestContract:
        """Return a contract with additional, *supplementary* outcomes.

        Supplementary outcomes strengthen a contract; they never replace a
        required one.  The required set is carried over unchanged.
        """
        existing = {c.id for c in self.required_outcomes}
        additions = tuple(c for c in extra if c.id not in existing)
        return self.model_copy(
            update={"required_outcomes": self.required_outcomes + additions}
        )

    def covers(self, criteria: tuple[AcceptanceCriterion, ...]) -> list[str]:
        """Which required outcomes are *not* represented in ``criteria``.

        A plan that omits a required outcome is trying to weaken the
        contract; the ids are returned so the caller can refuse the plan.
        """
        present = {c.id for c in criteria}
        return [c.id for c in self.required_outcomes if c.id not in present]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "objective": self.objective,
            "required_outcomes": [c.model_dump(mode="json") for c in self.required_outcomes],
            "scope": self.scope.model_dump(mode="json"),
            "allow_pre_satisfied": self.allow_pre_satisfied,
            "notes": self.notes,
            "contract_hash": self.contract_hash,
        }


class OutcomeVerdict(BaseModel):
    """How one required outcome was judged."""

    criterion_id: str
    state: OutcomeState
    detail: str = ""
    base_exit_code: int | None = None
    candidate_exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ContractVerdict(BaseModel):
    """The judgement of a run against its contract."""

    outcome: VerificationOutcome
    contract_hash: str
    base_revision: str = ""
    candidate_tree: str = ""
    verifier_version: str = ""
    verdicts: tuple[OutcomeVerdict, ...] = ()
    commands: tuple[str, ...] = ()
    detail: str = ""
    created_at: Any = Field(default_factory=utcnow)

    @property
    def accepted(self) -> bool:
        return self.outcome is VerificationOutcome.ACCEPTED

    @property
    def established(self) -> tuple[str, ...]:
        return tuple(v.criterion_id for v in self.verdicts if v.state is OutcomeState.ESTABLISHED)

    @property
    def pre_satisfied(self) -> tuple[str, ...]:
        return tuple(v.criterion_id for v in self.verdicts if v.state is OutcomeState.PRE_SATISFIED)

    @property
    def not_satisfied(self) -> tuple[str, ...]:
        return tuple(v.criterion_id for v in self.verdicts if v.state is OutcomeState.NOT_SATISFIED)

    @property
    def unverifiable(self) -> tuple[str, ...]:
        return tuple(v.criterion_id for v in self.verdicts if v.state is OutcomeState.UNVERIFIABLE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "contract_hash": self.contract_hash,
            "base_revision": self.base_revision,
            "candidate_tree": self.candidate_tree,
            "verifier_version": self.verifier_version,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "commands": list(self.commands),
            "established": list(self.established),
            "pre_satisfied": list(self.pre_satisfied),
            "not_satisfied": list(self.not_satisfied),
            "unverifiable": list(self.unverifiable),
            "detail": self.detail,
            "created_at": self.created_at.isoformat()
            if hasattr(self.created_at, "isoformat")
            else str(self.created_at),
        }
