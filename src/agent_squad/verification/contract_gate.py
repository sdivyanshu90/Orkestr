"""The contract gate: an independent acceptance boundary.

This is the component that decides whether a run *satisfied its request*, as
opposed to whether the workers finished their tasks.  It differs from
:class:`~agent_squad.verification.gate.VerificationGate` in three ways:

* it judges the **final combined candidate tree**, not a worker's worktree;
* it evaluates every required outcome against **both** the base tree and the
  candidate tree, so that a pre-satisfied outcome is not mistaken for
  evidence;
* it produces an explicit :class:`~agent_squad.models.contract.VerificationOutcome`
  rather than a boolean.

The "both trees" evaluation is the important part.  A criterion that already
held before the work started tells us nothing about the work, and reporting
it as satisfied is exactly the false success that R0 exists to eliminate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..config import RunConfig
from ..models.contract import (
    ContractVerdict,
    OutcomeState,
    OutcomeVerdict,
    RequestContract,
    VerificationOutcome,
)
from ..models.task import AcceptanceCriterion, CheckKind
from ..store.events import EventLog, EventType
from ..store.state import StateStore
from .checks import run_criterion

#: Bumped whenever the judgement logic changes, so that evidence produced by
#: an older verifier can be told apart from evidence produced by a newer one.
VERIFIER_VERSION = "contract-gate/1"


@dataclass
class ContractGate:
    """Judges a candidate tree against the request contract."""

    config: RunConfig
    state: StateStore | None = None
    log: EventLog | None = None

    async def verify(
        self,
        contract: RequestContract,
        *,
        base_tree: Path,
        candidate_tree: Path,
        base_revision: str = "",
        candidate_tree_hash: str = "",
    ) -> ContractVerdict:
        """Evaluate every required outcome against both trees."""
        outcomes = list(contract.required_outcomes)
        verdicts: list[OutcomeVerdict] = []
        commands: list[str] = []
        # The tree digests are computed once.  They serve two purposes: they
        # decide whether the run changed *anything* at all (a no-op), and
        # they are the subject digest for every criterion that observes the
        # whole tree.
        base_digest = self._tree_digest(base_tree)
        candidate_digest = self._tree_digest(candidate_tree)
        no_op = candidate_digest == base_digest

        if not outcomes:
            # Nothing independent was required, so nothing can be established.
            verdict = ContractVerdict(
                outcome=VerificationOutcome.INCONCLUSIVE,
                contract_hash=contract.contract_hash,
                base_revision=base_revision,
                candidate_tree=candidate_tree_hash,
                verifier_version=VERIFIER_VERSION,
                detail=(
                    "the request contract states no required outcome, so the run "
                    "cannot be accepted: no independent check was established"
                ),
            )
            self._record(contract, verdict)
            return verdict

        for criterion in outcomes:
            base_subject = self._subject_digest(criterion, base_tree, base_digest)
            candidate_subject = self._subject_digest(
                criterion, candidate_tree, candidate_digest
            )
            base_result = await self._run(
                criterion, base_tree, has_changes=False, tree_digest=base_digest
            )
            candidate_result = await self._run(
                criterion,
                candidate_tree,
                has_changes=no_op is False,
                tree_digest=candidate_digest,
            )
            commands.extend(base_result[1])
            commands.extend(candidate_result[1])
            state, detail = self._classify(
                criterion,
                base_passed=base_result[0],
                candidate_passed=candidate_result[0],
                subject_changed=base_subject != candidate_subject,
                allow_pre_satisfied=contract.allow_pre_satisfied,
            )
            verdicts.append(
                OutcomeVerdict(
                    criterion_id=criterion.id,
                    state=state,
                    detail=detail,
                    base_passed=base_result[0],
                    candidate_passed=candidate_result[0],
                )
            )

        verdict = self._conclude(
            contract,
            verdicts,
            base_revision,
            candidate_tree_hash,
            commands,
            no_op=no_op,
        )
        self._record(contract, verdict)
        return verdict

    # ------------------------------------------------------------- helpers

    async def _run(
        self,
        criterion: AcceptanceCriterion,
        tree: Path,
        *,
        has_changes: bool,
        tree_digest: str,
    ) -> tuple[bool | None, list[str], str]:
        """Execute one criterion against one tree.

        Returns ``(passed, commands, detail)``.  ``passed`` is the check's
        *complete* verdict -- the exit code **and** every match predicate.
        ``None`` means the check could not be executed at all, which is
        different from failing.
        """
        try:
            outcome = await run_criterion(
                criterion,
                workspace=tree,
                sandbox_config=self.config.sandbox,
                has_changes=has_changes,
                timeout_s=criterion.timeout_s
                or self.config.verification.command_timeout_s,
            )
        except Exception as exc:
            return None, [f"error: {exc}"], str(exc)
        record = outcome.record
        commands = [" ".join(record.command)] if record.command else []
        detail = record.error or ""
        if not record.deterministic:
            return None, commands, detail or "the check is advisory"
        # The complete result, not the exit code alone.
        return record.passed, commands, detail

    @staticmethod
    def _tree_digest(tree: Path) -> str:
        """A content digest of a tree, ignoring git's own metadata."""
        digest = hashlib.sha256()
        try:
            for path in sorted(tree.rglob("*")):
                if not path.is_file() or ".git" in path.parts:
                    continue
                digest.update(str(path.relative_to(tree)).encode())
                try:
                    digest.update(path.read_bytes())
                except OSError:
                    continue
        except OSError:
            return "unreadable"
        return digest.hexdigest()

    @classmethod
    def _subject_digest(
        cls, criterion: AcceptanceCriterion, tree: Path, tree_digest: str
    ) -> str:
        """The digest of whatever the criterion actually observes.

        This is what makes "preserved" different from "pre-satisfied".  A
        criterion about one file observes that file; a criterion that runs a
        command observes the whole tree, because a command may read
        anything.  Comparing the subject across the two trees answers the
        only question that matters: *did the run touch the thing the
        requirement is about?*
        """
        path_spec = criterion.spec.get("path")
        if not isinstance(path_spec, str) or not path_spec:
            return tree_digest
        if criterion.kind not in (
            CheckKind.FILE_EXISTS,
            CheckKind.FILE_CONTAINS,
            CheckKind.FILE_NOT_CONTAINS,
        ):
            return tree_digest
        digest = hashlib.sha256()
        digest.update(path_spec.encode("utf-8"))
        target = tree / path_spec
        try:
            if target.is_file():
                digest.update(b"\x00file\x00")
                digest.update(target.read_bytes())
            elif target.is_dir():
                digest.update(b"\x00dir\x00")
            else:
                digest.update(b"\x00missing\x00")
        except OSError:
            digest.update(b"\x00unreadable\x00")
        return digest.hexdigest()

    @staticmethod
    def _classify(
        criterion: AcceptanceCriterion,
        *,
        base_passed: bool | None,
        candidate_passed: bool | None,
        subject_changed: bool,
        allow_pre_satisfied: bool,
    ) -> tuple[OutcomeState, str]:
        """Decide what one outcome's two evaluations mean.

        The distinction that matters is *preserving* an invariant against
        *establishing* a newly requested change:

        * false -> true: the work caused it (``ESTABLISHED``);
        * true -> true and the run touched the subject: the work preserved
          it (``PRESERVED``), which satisfies the requirement -- an
          invariant that already held is exactly what "must still hold"
          asked for;
        * true -> true and the run did not touch the subject: the outcome
          was already true and proves nothing about the work
          (``PRE_SATISFIED``);
        * true -> false: the work broke it (``NOT_SATISFIED``);
        * false -> false: it was never achieved (``NOT_SATISFIED``).

        Rejecting every pre-satisfied invariant would be wrong in the other
        direction: "the test suite must still pass" is satisfied by a run
        that leaves it passing, and the run-level no-op rule below is what
        prevents a run that did *nothing* from claiming success.
        """
        if candidate_passed is None:
            return (
                OutcomeState.UNVERIFIABLE,
                "the check could not be executed against the candidate tree",
            )
        if not candidate_passed:
            return (
                OutcomeState.NOT_SATISFIED,
                f"the check failed on the candidate tree: {criterion.id}",
            )
        if base_passed is None:
            return (
                OutcomeState.UNVERIFIABLE,
                "the check could not be executed against the base tree",
            )
        if not base_passed:
            return (
                OutcomeState.ESTABLISHED,
                "the check failed on the base tree and passes on the candidate tree",
            )
        if subject_changed:
            return (
                OutcomeState.PRESERVED,
                "the outcome held before the work and still holds after it",
            )
        if allow_pre_satisfied:
            return (
                OutcomeState.PRE_SATISFIED,
                "the outcome already held before the work and the contract "
                "permits a no-op",
            )
        return (
            OutcomeState.PRE_SATISFIED,
            "the outcome already held before the work and the run did not "
            "touch what it is about, so it establishes nothing",
        )

    @staticmethod
    def _conclude(
        contract: RequestContract,
        verdicts: list[OutcomeVerdict],
        base_revision: str,
        candidate_tree_hash: str,
        commands: list[str],
        *,
        no_op: bool,
    ) -> ContractVerdict:
        not_satisfied = [v.criterion_id for v in verdicts if v.state is OutcomeState.NOT_SATISFIED]
        unverifiable = [v.criterion_id for v in verdicts if v.state is OutcomeState.UNVERIFIABLE]
        not_allowed = [
            v.criterion_id
            for v in verdicts
            if v.state is OutcomeState.PRE_SATISFIED
            and not contract.allow_pre_satisfied
        ]

        if not_satisfied:
            outcome = VerificationOutcome.REJECTED
            detail = (
                "required outcomes are not satisfied by the candidate tree: "
                + ", ".join(not_satisfied)
            )
        elif unverifiable:
            outcome = VerificationOutcome.INCONCLUSIVE
            detail = (
                "some required outcomes could not be checked: "
                + ", ".join(unverifiable)
            )
        elif no_op and not contract.allow_pre_satisfied:
            # The candidate tree is byte-identical to the base tree: the
            # run changed nothing, so it produced no evidence about the
            # request.  This is the false success that R0 exists to catch,
            # and it is reported as *inconclusive* rather than *accepted*.
            outcome = VerificationOutcome.INCONCLUSIVE
            detail = (
                "the run changed nothing: the candidate tree is byte-identical "
                "to the base tree, so no outcome was established by the work"
            )
        elif not_allowed:
            outcome = VerificationOutcome.INCONCLUSIVE
            detail = (
                "the following outcomes were already true before the work and "
                "therefore establish nothing: "
                + ", ".join(not_allowed)
            )
        else:
            outcome = VerificationOutcome.ACCEPTED
            detail = (
                "every required outcome holds on the candidate tree; "
                + f"{sum(1 for v in verdicts if v.state is OutcomeState.ESTABLISHED)} "
                "established, "
                + f"{sum(1 for v in verdicts if v.state is OutcomeState.PRESERVED)} "
                "preserved"
            )

        return ContractVerdict(
            outcome=outcome,
            contract_hash=contract.contract_hash,
            base_revision=base_revision,
            candidate_tree=candidate_tree_hash,
            verifier_version=VERIFIER_VERSION,
            verdicts=tuple(verdicts),
            commands=tuple(commands),
            detail=detail,
        )

    def _record(self, contract: RequestContract, verdict: ContractVerdict) -> None:
        if self.log is None:
            return
        self.log.append(
            EventType.VERIFICATION_CHECK,
            run_id=self.config.run_id,
            payload={
                "kind": "contract",
                "outcome": verdict.outcome.value,
                "contract_hash": verdict.contract_hash,
                "candidate_tree": verdict.candidate_tree,
                "verifier_version": verdict.verifier_version,
                "established": list(verdict.established),
                "preserved": list(verdict.preserved),
                "pre_satisfied": list(verdict.pre_satisfied),
                "not_satisfied": list(verdict.not_satisfied),
                "unverifiable": list(verdict.unverifiable),
                "detail": verdict.detail,
            },
        )
