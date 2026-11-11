"""C2: the contract is immutable, versioned and bound.

The contract is the owner's statement of what must be true.  These tests
cover the three ways that statement can be undermined:

* an **id reused with different semantics** -- the plan keeps the id and
  swaps the check underneath it;
* an **unauthorised mutation** -- the required outcomes are changed
  without the owner saying so;
* a **revision** -- which is allowed, but must invalidate the evidence and
  the approvals bound to the contract it replaces.

They also cover the binding: a verdict is bound to the tree, the base,
the contract, the verifier and the environment, and changing any of them
invalidates it.
"""

from __future__ import annotations

import pytest

from agent_squad.errors import ValidationError
from agent_squad.models.contract import (
    ContractVerdict,
    RequestContract,
    VerificationOutcome,
)
from agent_squad.models.task import AcceptanceCriterion, CheckKind, Role, TaskSpec
from agent_squad.planning.contract import build_contract, enforce_plan_contract
from agent_squad.planning.replan import check_revision
from agent_squad.security.approval import ApprovalBinding, ApprovalGrant


def criterion(
    cid: str,
    kind: str = "file_exists",
    spec: dict | None = None,
    description: str | None = None,
) -> AcceptanceCriterion:
    """A criterion with a readable identity."""
    return AcceptanceCriterion(
        id=cid,
        description=description or cid,
        kind=CheckKind(kind),
        spec=spec if spec is not None else {"path": f"{cid}.py"},
    )


def task(
    task_id: str,
    criteria: tuple[AcceptanceCriterion, ...],
    *,
    title: str = "t",
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        run_id="r",
        title=title,
        objective="o",
        acceptance_criteria=criteria,
        role=Role.IMPLEMENTER,
    )


# --------------------------------------------------------------------------
# identity is bound to meaning
# --------------------------------------------------------------------------


def test_a_criterion_key_covers_what_the_criterion_checks() -> None:
    """Two criteria with the same id but different checks differ."""
    weak = criterion("must-pass", "file_exists", {"path": "a.py"})
    strong = criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "x"})
    assert weak.semantic_key != strong.semantic_key
    # The key is about the meaning, so the id is not part of it.
    assert weak.semantic_key == criterion("must-pass", "file_exists", {"path": "a.py"}).semantic_key


def test_a_plan_may_not_reuse_a_required_id_with_a_different_meaning() -> None:
    """The weakening that an id-based comparison cannot see."""
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "x"}),),
    )
    weakened = criterion("must-pass", "file_exists", {"path": "a.py"})

    assert contract.check_criterion_semantics((weakened,)) == ["must-pass"]
    with pytest.raises(ValidationError) as exc:
        contract.assert_criterion_semantics((weakened,))
    assert "must-pass" in str(exc.value)
    assert "different meanings" in str(exc.value)


def test_the_same_criterion_is_not_a_weakening() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "x"}),),
    )
    same = criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "x"})
    assert contract.check_criterion_semantics((same,)) == []
    contract.assert_criterion_semantics((same,))


def test_a_plan_that_drops_a_required_outcome_is_still_refused() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass"),),
    )
    with pytest.raises(ValidationError) as exc:
        enforce_plan_contract(contract, (criterion("something-else"),))
    assert "must-pass" in str(exc.value)


def test_an_owner_request_may_not_reuse_an_id_with_a_different_meaning() -> None:
    """Two tasks that claim the same id mean two different requirements."""
    spec = {
        "objective": "o",
        "tasks": [
            {
                "id": "one",
                "title": "one",
                "objective": "o",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "shared",
                        "description": "a.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "a.py"},
                    }
                ],
            },
            {
                "id": "two",
                "title": "two",
                "objective": "o",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "shared",
                        "description": "a.py contains the answer",
                        "kind": "file_contains",
                        "spec": {"path": "a.py", "pattern": "42"},
                    }
                ],
            },
        ],
    }
    with pytest.raises(ValidationError) as exc:
        build_contract("the objective", spec)
    assert "shared" in str(exc.value)


def test_an_owner_request_may_state_the_same_requirement_twice() -> None:
    """The same criterion in two tasks is one requirement, not a conflict."""
    spec = {
        "objective": "o",
        "tasks": [
            {
                "id": "one",
                "title": "one",
                "objective": "o",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "shared",
                        "description": "a.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "a.py"},
                    }
                ],
            },
            {
                "id": "two",
                "title": "two",
                "objective": "o",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "shared",
                        "description": "a.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "a.py"},
                    }
                ],
            },
        ],
    }
    contract = build_contract("the objective", spec)
    assert [c.id for c in contract.required_outcomes] == ["shared"]


def test_a_supplementary_outcome_may_not_redefine_a_required_one() -> None:
    """A silent drop would hide the attempt to weaken the contract."""
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "x"}),),
    )
    with pytest.raises(ValidationError) as exc:
        contract.with_supplementary((criterion("must-pass", "file_exists", {"path": "a.py"}),))
    assert "must-pass" in str(exc.value)


def test_a_supplementary_outcome_may_strengthen_the_contract() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass"),),
    )
    strengthened = contract.with_supplementary((criterion("extra"),))
    assert {c.id for c in strengthened.required_outcomes} == {"must-pass", "extra"}


# --------------------------------------------------------------------------
# a replan may not weaken the contract
# --------------------------------------------------------------------------


def test_a_replan_may_not_reuse_a_required_id_with_a_different_meaning() -> None:
    """The replanning path is the one that runs after a failure."""
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(
            criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "42"}),
        ),
    )
    previous = (task("t", (criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "42"}),)),)
    revised = (
        task(
            "t",
            (criterion("must-pass", "file_exists", {"path": "a.py"}),),
            title="weakened",
        ),
    )
    with pytest.raises(ValidationError) as exc:
        check_revision(
            previous=previous,
            revised=revised,
            contract=contract,
            completed_task_ids=(),
        )
    assert "must-pass" in str(exc.value)


def test_a_replan_may_not_drop_a_required_outcome() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass"),),
    )
    previous = (task("t", (criterion("must-pass"),)),)
    revised = (task("t", (criterion("other"),), title="changed"),)
    with pytest.raises(ValidationError) as exc:
        check_revision(
            previous=previous,
            revised=revised,
            contract=contract,
            completed_task_ids=(),
        )
    assert "must-pass" in str(exc.value)


def test_a_replan_that_adds_a_check_is_admissible() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass"),),
    )
    previous = (task("t", (criterion("must-pass"),)),)
    revised = (task("t", (criterion("must-pass"), criterion("extra")), title="more"),)
    changed = check_revision(
        previous=previous,
        revised=revised,
        contract=contract,
        completed_task_ids=(),
    )
    assert changed == ("t",)


# --------------------------------------------------------------------------
# an authorised revision invalidates the old evidence and approvals
# --------------------------------------------------------------------------


def test_a_revision_is_a_new_version_with_a_new_hash() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass"),),
    )
    revision = contract.revise(authorised_by="the-owner", reason="tighter scope")
    assert revision.version == contract.version + 1
    assert revision.contract_hash != contract.contract_hash
    assert revision.supersedes == contract.contract_hash
    assert revision.authorised_by == "the-owner"


def test_a_revision_may_strengthen_but_never_weaken() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(
            criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "42"}),
        ),
    )
    with pytest.raises(ValidationError) as exc:
        contract.revise(
            required_outcomes=(criterion("must-pass", "file_exists", {"path": "a.py"}),),
            authorised_by="the-owner",
        )
    assert "must-pass" in str(exc.value)

    with pytest.raises(ValidationError):
        contract.revise(required_outcomes=(), authorised_by="the-owner")

    strengthened = contract.revise(
        required_outcomes=(
            criterion("must-pass", "file_contains", {"path": "a.py", "pattern": "42"}),
            criterion("extra"),
        ),
        authorised_by="the-owner",
    )
    assert {c.id for c in strengthened.required_outcomes} == {"must-pass", "extra"}


def test_a_revision_invalidates_the_evidence_and_the_approvals() -> None:
    """Everything bound to the old contract stops applying."""
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass"),),
    )
    old_evidence = ContractVerdict(
        outcome=VerificationOutcome.ACCEPTED,
        contract_hash=contract.contract_hash,
        base_revision="base",
        candidate_tree="tree",
        verifier_version="contract-gate/1",
    )
    revision = contract.revise(authorised_by="the-owner")

    invalidated, reason = revision.invalidates(contract)
    assert invalidated is True
    assert "void" in reason

    # The evidence no longer binds to the revised request.
    bound, _ = old_evidence.binds_to(contract_hash=revision.contract_hash)
    assert bound is False

    # And an approval granted against the old contract cannot authorise an
    # effect under the new one.
    old_binding = ApprovalBinding(
        action="git.push",
        arguments=("git", "push", "main"),
        workspace="/wt",
        contract_hash=contract.contract_hash,
    )
    grant = ApprovalGrant(approval_id="a", binding=old_binding, approved=True)
    new_binding = ApprovalBinding(
        action="git.push",
        arguments=("git", "push", "main"),
        workspace="/wt",
        contract_hash=revision.contract_hash,
    )
    allowed, _reason = grant.authorises(new_binding)
    assert allowed is False


# --------------------------------------------------------------------------
# the evidence binding
# --------------------------------------------------------------------------


def _verdict() -> ContractVerdict:
    return ContractVerdict(
        outcome=VerificationOutcome.ACCEPTED,
        contract_hash="c" * 64,
        base_revision="base-revision",
        candidate_tree="candidate-tree",
        verifier_version="contract-gate/1",
    )


def test_a_verdict_binds_to_the_tree_the_base_the_contract_the_verifier_and_the_environment() -> None:
    verdict = _verdict()
    binding = verdict.binding
    assert binding.candidate_tree == "candidate-tree"
    assert binding.base_revision == "base-revision"
    assert binding.contract_hash == "c" * 64
    assert binding.verifier_version == "contract-gate/1"
    assert binding.environment_fingerprint
    assert binding.binding_hash == verdict.binding_hash


@pytest.mark.parametrize(
    "changed",
    [
        {"candidate_tree": "another-tree"},
        {"base_revision": "another-base"},
        {"contract_hash": "d" * 64},
        {"verifier_version": "contract-gate/2"},
        {"environment_fingerprint": "another-environment"},
    ],
)
def test_changing_any_bound_input_invalidates_the_verdict(changed: dict) -> None:
    verdict = _verdict()
    bound, reason = verdict.binds_to(**changed)
    assert bound is False
    assert reason


def test_the_verdict_still_binds_to_the_same_inputs() -> None:
    verdict = _verdict()
    bound, _ = verdict.binds_to(
        candidate_tree="candidate-tree",
        base_revision="base-revision",
        contract_hash="c" * 64,
        verifier_version="contract-gate/1",
        environment_fingerprint=verdict.environment_fingerprint,
    )
    assert bound is True


def test_the_environment_is_part_of_the_binding_even_when_it_is_not_stated() -> None:
    """A verdict that does not say where it was produced is not bound."""
    verdict = _verdict()
    assert verdict.environment_fingerprint
    assert verdict.binding_hash != ContractVerdict(
        outcome=VerificationOutcome.ACCEPTED,
        contract_hash="c" * 64,
        base_revision="base-revision",
        candidate_tree="candidate-tree",
        verifier_version="contract-gate/1",
        environment_fingerprint="somewhere-else",
    ).binding_hash


def test_the_contract_hash_covers_the_revision() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(criterion("must-pass"),),
        notes="first",
    )
    revision = contract.revise(authorised_by="the-owner")
    assert contract.contract_hash != revision.contract_hash
    assert contract.contract_hash == contract.contract_hash
