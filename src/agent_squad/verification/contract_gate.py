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
from ..models.task import AcceptanceCriterion
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
            base_result = await self._run(criterion, base_tree)
            candidate_result = await self._run(criterion, candidate_tree)
            commands.extend(base_result[1])
            commands.extend(candidate_result[1])
            state, detail = self._classify(
                criterion,
                base_exit=base_result[0],
                candidate_exit=candidate_result[0],
                allow_pre_satisfied=contract.allow_pre_satisfied,
            )
            verdicts.append(
                OutcomeVerdict(
                    criterion_id=criterion.id,
                    state=state,
                    detail=detail,
                    base_exit_code=base_result[0],
                    candidate_exit_code=candidate_result[0],
                )
            )

        verdict = self._conclude(contract, verdicts, base_revision, candidate_tree_hash, commands)
        self._record(contract, verdict)
        return verdict

    # ------------------------------------------------------------- helpers

    async def _run(
        self, criterion: AcceptanceCriterion, tree: Path
    ) -> tuple[int | None, list[str]]:
        """Execute one criterion against one tree."""
        try:
            outcome = await run_criterion(
                criterion,
                workspace=tree,
                sandbox_config=self.config.sandbox,
                has_changes=False,
                timeout_s=criterion.timeout_s
                or self.config.verification.command_timeout_s,
            )
        except Exception as exc:
            return None, [f"error: {exc}"]
        commands = [" ".join(outcome.record.command)] if outcome.record.command else []
        return outcome.record.exit_code, commands

    @staticmethod
    def _classify(
        criterion: AcceptanceCriterion,
        *,
        base_exit: int | None,
        candidate_exit: int | None,
        allow_pre_satisfied: bool,
    ) -> tuple[OutcomeState, str]:
        """Decide what one outcome's two evaluations mean."""
        if candidate_exit is None:
            return (
                OutcomeState.UNVERIFIABLE,
                "the check could not be executed against the candidate tree",
            )
        candidate_passed = candidate_exit == 0
        if not candidate_passed:
            return (
                OutcomeState.NOT_SATISFIED,
                f"the check failed on the candidate tree (exit {candidate_exit})",
            )
        if base_exit is None:
            return (
                OutcomeState.UNVERIFIABLE,
                "the check could not be executed against the base tree",
            )
        if base_exit == 0:
            if allow_pre_satisfied:
                return (
                    OutcomeState.PRE_SATISFIED,
                    "the outcome already held before the work and the contract "
                    "permits a no-op",
                )
            return (
                OutcomeState.PRE_SATISFIED,
                "the outcome already held before the work, so it is not evidence "
                "that the request was satisfied; the contract does not permit a "
                "no-op",
            )
        return (
            OutcomeState.ESTABLISHED,
            "the check failed on the base tree and passes on the candidate tree",
        )

    @staticmethod
    def _conclude(
        contract: RequestContract,
        verdicts: list[OutcomeVerdict],
        base_revision: str,
        candidate_tree_hash: str,
        commands: list[str],
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
        elif not_allowed:
            outcome = VerificationOutcome.INCONCLUSIVE
            detail = (
                "the following outcomes were already true before the work and "
                "therefore establish nothing: "
                + ", ".join(not_allowed)
            )
        else:
            outcome = VerificationOutcome.ACCEPTED
            detail = "every required outcome is established by the candidate tree"

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
                "pre_satisfied": list(verdict.pre_satisfied),
                "not_satisfied": list(verdict.not_satisfied),
                "unverifiable": list(verdict.unverifiable),
                "detail": verdict.detail,
            },
        )
