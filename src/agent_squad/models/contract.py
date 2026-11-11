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

from ..config import SandboxConfig
from ..errors import ValidationError
from ..observe.fingerprint import capture as capture_environment
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

    PRESERVED = "preserved"
    """Held before the work and still holds: the work did not break it.

    This satisfies the requirement.  Rejecting every pre-satisfied outcome
    would be wrong: "the test suite must still pass" is an invariant, and an
    invariant that already held is exactly what was asked for.
    """

    PRE_SATISFIED = "pre_satisfied"
    """Already held before the work, and the run did not touch what the
    outcome is about, so it establishes nothing about the work."""

    NOT_SATISFIED = "not_satisfied"
    """Does not hold on the candidate tree."""

    UNVERIFIABLE = "unverifiable"
    """The check could not be executed."""


class RequestContract(BaseModel):
    """The owner's immutable statement of what must be true when done.

    The contract is hashed so that every piece of evidence can be bound to
    the exact request it was produced for.  Any change to the contract
    changes the hash and invalidates the evidence.

    Two things make the contract *immutable* rather than merely
    hash-addressed:

    * **Identity is bound to meaning.**  Every required outcome carries a
      :attr:`~agent_squad.models.task.AcceptanceCriterion.semantic_key` --
      a hash of what the criterion checks.  A plan that keeps an outcome's
      id but changes what it checks has not satisfied the requirement; it
      has renamed it.  :meth:`assert_criterion_semantics` refuses that.
    * **A revision is explicit.**  :meth:`revise` produces a new version
      with a new hash and records the hash it supersedes, which is what
      invalidates the evidence and the approvals bound to the old one.
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
    supersedes: str = Field(
        default="",
        description=(
            "The contract hash this revision replaces.  A revision records "
            "what it supersedes so that the evidence and the approvals bound "
            "to the old contract can be invalidated explicitly rather than "
            "by accident."
        ),
    )
    authorised_by: str = Field(
        default="",
        description=(
            "Who authorised the revision.  An unauthorised change to the "
            "contract is not a revision at all; it is a different request."
        ),
    )

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
            "supersedes": self.supersedes,
            "authorised_by": self.authorised_by,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def criterion_semantics(self) -> dict[str, str]:
        """The meaning of every required outcome, keyed by its id.

        This is the map a plan is compared against: an id that is present
        but means something else is *not* the requirement the owner asked
        for.
        """
        return {c.id: c.semantic_key for c in self.required_outcomes}

    def check_criterion_semantics(
        self, criteria: tuple[AcceptanceCriterion, ...] | list[AcceptanceCriterion]
    ) -> list[str]:
        """The ids whose *meaning* differs from the contract's.

        An id identifies a requirement.  Re-using it for a different check
        is not the same requirement spelled differently; it is a different
        requirement wearing the same name, and it is how a plan weakens a
        contract without ever dropping an id.
        """
        expected = self.criterion_semantics
        mismatched: list[str] = []
        for criterion in criteria:
            want = expected.get(criterion.id)
            if want is None:
                continue
            if want != criterion.semantic_key:
                mismatched.append(criterion.id)
        return mismatched

    def assert_criterion_semantics(
        self, criteria: tuple[AcceptanceCriterion, ...] | list[AcceptanceCriterion]
    ) -> None:
        """Refuse a plan that reuses a required outcome id with a new meaning."""
        mismatched = self.check_criterion_semantics(criteria)
        if mismatched:
            raise ValidationError(
                "the plan reuses required outcome ids with different meanings: "
                + ", ".join(mismatched)
                + ". An id identifies a requirement; reusing it for a "
                "different check weakens the contract and is refused.",
                mismatched=mismatched,
                contract_hash=self.contract_hash,
            )

    def assert_plan_preserves(
        self, criteria: tuple[AcceptanceCriterion, ...] | list[AcceptanceCriterion]
    ) -> None:
        """Refuse a plan that drops *or* weakens a required outcome."""
        missing = self.covers(tuple(criteria))
        if missing:
            raise ValidationError(
                "the plan drops required outcomes from the request contract: "
                + ", ".join(missing)
                + ". A plan may add checks but may not remove a required one.",
                missing=missing,
                contract_hash=self.contract_hash,
            )
        self.assert_criterion_semantics(criteria)

    def with_supplementary(
        self, extra: tuple[AcceptanceCriterion, ...]
    ) -> RequestContract:
        """Return a contract with additional, *supplementary* outcomes.

        Supplementary outcomes strengthen a contract; they never replace a
        required one.  The required set is carried over unchanged.

        A supplementary outcome that reuses a required id with a different
        meaning is refused rather than ignored: silently dropping it would
        hide the attempt to weaken the contract behind a successful call.
        """
        existing = {c.id for c in self.required_outcomes}
        additions: list[AcceptanceCriterion] = []
        for candidate in extra:
            if candidate.id not in existing:
                existing.add(candidate.id)
                additions.append(candidate)
                continue
            current = next(
                c for c in self.required_outcomes if c.id == candidate.id
            )
            if current.semantic_key != candidate.semantic_key:
                raise ValidationError(
                    f"the supplementary outcome {candidate.id!r} reuses a "
                    "required outcome id with a different meaning, which would "
                    "weaken the contract",
                    criterion_id=candidate.id,
                    contract_hash=self.contract_hash,
                )
        return self.model_copy(
            update={"required_outcomes": self.required_outcomes + tuple(additions)}
        )

    def revise(
        self,
        *,
        required_outcomes: tuple[AcceptanceCriterion, ...] | None = None,
        objective: str | None = None,
        notes: str | None = None,
        authorised_by: str = "",
        reason: str = "",
    ) -> RequestContract:
        """An authorised revision of the contract.

        A revision may *strengthen* the contract -- more outcomes, a
        sharper description -- but it may never weaken it: every existing
        required outcome has to survive with exactly the same meaning.  The
        revision receives a new version and a new hash and records the hash
        it supersedes.

        That last part is what makes the revision safe.  Evidence and
        approvals are bound to a contract hash; a revision changes the
        hash, so everything bound to the old one stops matching and must be
        produced again.  The invalidation is explicit rather than
        incidental: :meth:`invalidates` says so in words.
        """
        outcomes = self.required_outcomes
        if required_outcomes is not None:
            proposed = tuple(required_outcomes)
            missing = self.covers(proposed)
            if missing:
                raise ValidationError(
                    "the revision would drop required outcomes: "
                    + ", ".join(missing),
                    missing=missing,
                    contract_hash=self.contract_hash,
                )
            weakened = self.check_criterion_semantics(proposed)
            if weakened:
                raise ValidationError(
                    "the revision would change the meaning of required "
                    "outcomes: " + ", ".join(weakened),
                    weakened=weakened,
                    contract_hash=self.contract_hash,
                )
            outcomes = proposed
        revision_notes = self.notes if notes is None else notes
        if reason:
            revision_notes = (
                f"{revision_notes}\n[revision {self.version + 1}: {reason}]"
            ).strip()
        return RequestContract(
            version=self.version + 1,
            objective=self.objective if objective is None else objective,
            required_outcomes=outcomes,
            scope=self.scope,
            allow_pre_satisfied=self.allow_pre_satisfied,
            notes=revision_notes,
            supersedes=self.contract_hash,
            authorised_by=authorised_by,
        )

    def invalidates(self, other: RequestContract) -> tuple[bool, str]:
        """Whether this revision invalidates ``other``'s evidence and approvals."""
        if other.contract_hash == self.contract_hash:
            return False, "the contract was not revised"
        if self.supersedes and self.supersedes != other.contract_hash:
            return False, "this revision does not supersede that contract"
        return True, (
            "the contract was revised; evidence and approvals bound to the "
            "superseded contract are void and must be produced again"
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
            "supersedes": self.supersedes,
            "authorised_by": self.authorised_by,
            "contract_hash": self.contract_hash,
            "criterion_semantics": dict(self.criterion_semantics),
        }


class EvidenceBinding(BaseModel):
    """The exact inputs a verdict is bound to.

    A verdict is only meaningful for the tree it judged, the base it
    started from, the contract it was asked about, the verifier that
    produced it and the environment it ran in.  Changing any of them
    invalidates it, so the binding is *recorded* rather than assumed.

    The binding is what turns "the run passed" into "the run passed, for
    this tree, this base, this request, this verifier and this
    environment".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_tree: str = ""
    base_revision: str = ""
    contract_hash: str = ""
    verifier_version: str = ""
    environment_fingerprint: str = ""

    @property
    def binding_hash(self) -> str:
        """A content hash over every bound input."""
        payload = {
            "candidate_tree": self.candidate_tree,
            "base_revision": self.base_revision,
            "contract_hash": self.contract_hash,
            "verifier_version": self.verifier_version,
            "environment_fingerprint": self.environment_fingerprint,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_tree": self.candidate_tree,
            "base_revision": self.base_revision,
            "contract_hash": self.contract_hash,
            "verifier_version": self.verifier_version,
            "environment_fingerprint": self.environment_fingerprint,
            "binding_hash": self.binding_hash,
        }


def environment_fingerprint(
    sandbox: SandboxConfig | None = None,
    *,
    isolation_profile: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    """The fingerprint of the environment a verdict is produced in.

    It combines the runtime environment -- interpreter, platform,
    dependency set -- with the *effective* isolation configuration,
    because the sandbox and the isolation profile decide what the worker
    was allowed to do, and therefore decide what the evidence means.

    The fingerprint is deliberately not a snapshot of the whole machine:
    it is the small set of facts that can change the meaning of a
    verdict.  The dependency set and the interpreter version are in it
    because a different pytest or a different Python can turn a passing
    check into a failing one, and the isolation configuration is in it
    because a run without containment is not the same run as one with
    it.
    """
    details: dict[str, Any] = dict(extra or {})
    if sandbox is not None:
        details["sandbox"] = sandbox.model_dump(mode="json")
    if isolation_profile:
        details["isolation_profile"] = isolation_profile
    return capture_environment(extra=details).fingerprint


class OutcomeVerdict(BaseModel):
    """How one required outcome was judged."""

    criterion_id: str
    state: OutcomeState
    detail: str = ""
    base_passed: bool | None = None
    candidate_passed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ContractVerdict(BaseModel):
    """The judgement of a run against its contract.

    The verdict is bound to the tree it judged, the base it started from,
    the contract it answered, the verifier that produced it and the
    environment it ran in.  :attr:`binding` records that binding, and
    :meth:`binds_to` says whether a verdict still applies to a different
    set of those inputs -- which is the question "is this evidence still
    valid?" asked precisely.
    """

    outcome: VerificationOutcome
    contract_hash: str
    base_revision: str = ""
    candidate_tree: str = ""
    verifier_version: str = ""
    environment_fingerprint: str = ""
    verdicts: tuple[OutcomeVerdict, ...] = ()
    commands: tuple[str, ...] = ()
    detail: str = ""
    created_at: Any = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _bind_environment(self) -> ContractVerdict:
        """Record the environment the verdict was produced in.

        A verdict produced in one environment is not evidence about
        another, so the environment is part of the binding.  It is filled
        in automatically when the caller does not supply one, because an
        unbound verdict is a verdict nobody can check.
        """
        if not self.environment_fingerprint:
            self.environment_fingerprint = environment_fingerprint()
        return self

    @property
    def binding(self) -> EvidenceBinding:
        """The inputs this verdict is bound to."""
        return EvidenceBinding(
            candidate_tree=self.candidate_tree,
            base_revision=self.base_revision,
            contract_hash=self.contract_hash,
            verifier_version=self.verifier_version,
            environment_fingerprint=self.environment_fingerprint,
        )

    @property
    def binding_hash(self) -> str:
        """A content hash over every bound input."""
        return self.binding.binding_hash

    def binds_to(
        self,
        *,
        candidate_tree: str | None = None,
        base_revision: str | None = None,
        contract_hash: str | None = None,
        verifier_version: str | None = None,
        environment_fingerprint: str | None = None,
    ) -> tuple[bool, str]:
        """Whether the verdict still applies to these inputs.

        Returns ``(bound, reason)``.  A changed input invalidates the
        verdict: the evidence was produced for a different tree, a
        different request, a different verifier or a different
        environment, and it says nothing about this one.
        """
        checks = (
            ("candidate tree", self.candidate_tree, candidate_tree),
            ("base revision", self.base_revision, base_revision),
            ("contract", self.contract_hash, contract_hash),
            ("verifier", self.verifier_version, verifier_version),
            ("environment", self.environment_fingerprint, environment_fingerprint),
        )
        for label, bound, current in checks:
            if current is None:
                continue
            if bound != current:
                return False, (
                    f"the verdict is bound to a different {label} "
                    f"({bound or 'unset'} != {current})"
                )
        return True, "the verdict is bound to these inputs"

    @property
    def accepted(self) -> bool:
        return self.outcome is VerificationOutcome.ACCEPTED

    @property
    def established(self) -> tuple[str, ...]:
        return tuple(v.criterion_id for v in self.verdicts if v.state is OutcomeState.ESTABLISHED)

    @property
    def preserved(self) -> tuple[str, ...]:
        """Outcomes that held before the work and still hold after it."""
        return tuple(v.criterion_id for v in self.verdicts if v.state is OutcomeState.PRESERVED)

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
            "environment_fingerprint": self.environment_fingerprint,
            "binding_hash": self.binding_hash,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "commands": list(self.commands),
            "established": list(self.established),
            "preserved": list(self.preserved),
            "pre_satisfied": list(self.pre_satisfied),
            "not_satisfied": list(self.not_satisfied),
            "unverifiable": list(self.unverifiable),
            "detail": self.detail,
            "created_at": self.created_at.isoformat()
            if hasattr(self.created_at, "isoformat")
            else str(self.created_at),
        }
