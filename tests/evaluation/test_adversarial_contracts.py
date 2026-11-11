"""The adversarial contract corpus.

These cases are the release evidence for R0.  Each one is a way a run could
report success while the request was *not* satisfied, and each must be
caught.

The most important case is the first: **a required outcome that was already
true before the work started**.  It is what the local-model sweep actually
produced — eleven of sixteen verified runs had made no tool call at all,
because the planner chose a check that the pre-existing fixture satisfied.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import two_independent_spec

from agent_squad.config import RunConfig
from agent_squad.models.contract import (
    OutcomeState,
    RequestContract,
    VerificationOutcome,
)
from agent_squad.models.task import AcceptanceCriterion, CheckKind
from agent_squad.orchestrator import Orchestrator
from agent_squad.planning.contract import build_contract
from agent_squad.planning.planner import PlanRequest, RuleBasedPlanner, enforce_contract
from agent_squad.verification.contract_gate import ContractGate


def criterion(cid: str, kind: str, spec: dict, required: bool = True) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid, description=cid, kind=CheckKind(kind), spec=spec, required=required
    )


@pytest.fixture()
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """A base tree and a candidate tree."""
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    (base / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (candidate / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return base, candidate


# ---------------------------------------------------------------------------
# 1. The false success: an outcome that was already true.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_pre_satisfied_outcome_is_not_evidence(trees) -> None:
    """The exact failure the local sweep produced: nothing was done."""
    base, candidate = trees
    # The candidate is identical to the base: the "work" changed nothing.
    contract = RequestContract(
        objective="make sure calc.py exists",
        required_outcomes=(
            criterion("file", "file_exists", {"path": "calc.py"}),
        ),
    )
    gate = ContractGate(RunConfig(run_id="r", repo_path=str(base)))
    verdict = await gate.verify(contract, base_tree=base, candidate_tree=candidate)

    assert verdict.outcome is VerificationOutcome.INCONCLUSIVE
    assert verdict.pre_satisfied == ("file",)
    assert verdict.established == ()


@pytest.mark.integration
async def test_an_established_outcome_is_accepted(trees) -> None:
    """The control case: the outcome failed before and passes after."""
    base, candidate = trees
    (candidate / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    contract = RequestContract(
        objective="add new_module.py",
        required_outcomes=(
            criterion("file", "file_exists", {"path": "new_module.py"}),
        ),
    )
    gate = ContractGate(RunConfig(run_id="r", repo_path=str(base)))
    verdict = await gate.verify(contract, base_tree=base, candidate_tree=candidate)

    assert verdict.outcome is VerificationOutcome.ACCEPTED
    assert verdict.established == ("file",)


@pytest.mark.integration
async def test_a_pre_satisfied_outcome_is_accepted_when_the_contract_allows_it(
    trees,
) -> None:
    """A legitimate no-op must remain expressible."""
    base, candidate = trees
    contract = RequestContract(
        objective="verify calc.py exists (it already does)",
        required_outcomes=(
            criterion("file", "file_exists", {"path": "calc.py"}),
        ),
        allow_pre_satisfied=True,
    )
    gate = ContractGate(RunConfig(run_id="r", repo_path=str(base)))
    verdict = await gate.verify(contract, base_tree=base, candidate_tree=candidate)

    assert verdict.outcome is VerificationOutcome.ACCEPTED


@pytest.mark.integration
async def test_an_unsatisfied_outcome_is_rejected(trees) -> None:
    base, candidate = trees
    contract = RequestContract(
        objective="add missing.py",
        required_outcomes=(
            criterion("file", "file_exists", {"path": "missing.py"}),
        ),
    )
    gate = ContractGate(RunConfig(run_id="r", repo_path=str(base)))
    verdict = await gate.verify(contract, base_tree=base, candidate_tree=candidate)

    assert verdict.outcome is VerificationOutcome.REJECTED
    assert verdict.not_satisfied == ("file",)


@pytest.mark.integration
async def test_a_contract_with_no_required_outcome_is_inconclusive(trees) -> None:
    """Nothing independent was required, so nothing can be established."""
    base, candidate = trees
    contract = RequestContract(objective="do something unspecified")
    gate = ContractGate(RunConfig(run_id="r", repo_path=str(base)))
    verdict = await gate.verify(contract, base_tree=base, candidate_tree=candidate)

    assert verdict.outcome is VerificationOutcome.INCONCLUSIVE
    assert "no required outcome" in verdict.detail


# ---------------------------------------------------------------------------
# 2. A plan may add checks but may not remove them.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_plan_may_not_drop_a_required_outcome() -> None:
    from agent_squad.errors import ValidationError

    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(
            criterion("must-check", "file_exists", {"path": "a.py"}),
        ),
    )
    spec = {
        "objective": "do the thing",
        "tasks": [
            {
                "id": "t",
                "title": "t",
                "objective": "o",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                # The plan proposes a weaker check instead of the required one.
                "acceptance_criteria": [
                    {
                        "id": "weak",
                        "description": "a file exists somewhere",
                        "kind": "file_exists",
                        "spec": {"path": "b.py"},
                    }
                ],
            }
        ],
    }
    # The planner refuses the plan outright: a plan may strengthen the
    # contract but may not weaken it.
    with pytest.raises(ValidationError) as exc:
        await RuleBasedPlanner().plan(
            PlanRequest(
                run_id="r",
                objective="do the thing",
                request_spec=spec,
                contract=contract,
            )
        )
    assert "must-check" in str(exc.value)


@pytest.mark.integration
async def test_a_plan_may_add_supplementary_checks() -> None:
    contract = RequestContract(
        objective="do the thing",
        required_outcomes=(
            criterion("must-check", "file_exists", {"path": "a.py"}),
        ),
    )
    spec = {
        "objective": "do the thing",
        "tasks": [
            {
                "id": "t",
                "title": "t",
                "objective": "o",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "must-check",
                        "description": "a.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "a.py"},
                    },
                    {
                        "id": "extra",
                        "description": "b.py also exists",
                        "kind": "file_exists",
                        "spec": {"path": "b.py"},
                    },
                ],
            }
        ],
    }
    plan = await RuleBasedPlanner().plan(
        PlanRequest(run_id="r", objective="do the thing", request_spec=spec, contract=contract)
    )
    assert enforce_contract(plan, contract) == []
    assert {c.id for c in plan.tasks[0].acceptance_criteria} == {"must-check", "extra"}


# ---------------------------------------------------------------------------
# 3. The contract is derived from the owner, not from the model.
# ---------------------------------------------------------------------------


def test_the_contract_is_built_from_the_owners_request() -> None:
    spec = two_independent_spec()
    contract = build_contract("the objective", spec)
    ids = {c.id for c in contract.required_outcomes}
    expected = {
        criterion_id
        for task in spec["tasks"]
        for criterion_id in [c["id"] for c in task["acceptance_criteria"]]
    }
    assert ids == expected
    assert ids, "the owner's criteria must reach the contract"


def test_a_request_without_criteria_produces_no_required_outcomes() -> None:
    contract = build_contract("the objective", None)
    assert contract.required_outcomes == ()
    assert "no structured request" in contract.notes


def test_an_unexpressible_owner_criterion_is_a_hard_error() -> None:
    from agent_squad.errors import ValidationError

    spec = {
        "objective": "o",
        "tasks": [
            {
                "id": "t",
                "title": "t",
                "objective": "o",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "broken",
                        "description": "no selector",
                        "kind": "pytest_selector",
                        "spec": {},
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValidationError):
        build_contract("the objective", spec)


def test_the_contract_hash_changes_with_the_content() -> None:
    a = RequestContract(
        objective="o",
        required_outcomes=(criterion("c", "file_exists", {"path": "a.py"}),),
    )
    b = RequestContract(
        objective="o",
        required_outcomes=(criterion("c", "file_exists", {"path": "b.py"}),),
    )
    assert a.contract_hash != b.contract_hash
    assert a.contract_hash == a.contract_hash


# ---------------------------------------------------------------------------
# 4. The end-to-end run reports an explicit outcome.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_run_reports_its_contract_outcome(tmp_path: Path, repo: Path) -> None:
    config = RunConfig(
        run_id="run_contract",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        verification={"integration_test_command": ("python3", "-m", "pytest", "-q")},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run(
            "contract run", request_spec=two_independent_spec()
        )
    finally:
        await orchestrator.aclose()

    assert outcome.contract is not None
    assert outcome.contract.outcome in set(VerificationOutcome)
    # The contract hash is recorded, so the evidence is bound to the request.
    assert outcome.contract.contract_hash
    assert outcome.contract.verifier_version


@pytest.mark.integration
async def test_a_run_that_does_nothing_is_not_accepted(
    tmp_path: Path, repo: Path
) -> None:
    """The end-to-end version of the false-success case.

    The request asks for a file that the fixture already contains, so a
    worker that does nothing satisfies the criterion.  The run must not be
    *accepted*.
    """
    spec = {
        "objective": "ensure the existing test file is present",
        "tasks": [
            {
                "id": "noop",
                "title": "Check the existing test",
                "objective": "make sure tests/test_calc.py is present",
                "role": "implementer",
                "depends_on": [],
                "edits": [],
                "acceptance_criteria": [
                    {
                        "id": "pre-existing",
                        "description": "tests/test_calc.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "tests/test_calc.py"},
                    }
                ],
            }
        ],
    }
    config = RunConfig(
        run_id="run_noop",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        verification={"integration_test_command": ("python3", "-m", "pytest", "-q")},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("a no-op request", request_spec=spec)
    finally:
        await orchestrator.aclose()

    assert outcome.contract is not None
    assert outcome.contract.outcome is VerificationOutcome.INCONCLUSIVE
    assert outcome.contract.pre_satisfied == ("pre-existing",)


@pytest.mark.integration
async def test_a_run_that_tampers_with_protected_material_is_rejected(
    tmp_path: Path, repo: Path
) -> None:
    """The evidence is void if the thing that judged the work was changed.

    The hidden regression check is outside the worker's write scope, so the
    capability layer refuses to change it.  The hash comparison is the
    independent check that the scope was not bypassed.
    """
    protected = repo / "tests" / "test_calc.py"
    original = protected.read_text(encoding="utf-8")
    spec = {
        "objective": "add a module",
        "tasks": [
            {
                "id": "impl",
                "title": "Add a module",
                "objective": "Create extra.py",
                "role": "implementer",
                "depends_on": [],
                # The task's scope excludes the protected file.
                "exclude_paths": ["tests/test_calc.py"],
                "edits": [
                    {"path": "extra.py", "content": "value = 1\n"},
                    # And the worker tries to weaken the hidden test anyway.
                    {"path": "tests/test_calc.py", "content": "def test_add(): pass\n"},
                ],
                "acceptance_criteria": [
                    {
                        "id": "extra",
                        "description": "extra.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "extra.py"},
                    }
                ],
            }
        ],
    }
    config = RunConfig(
        run_id="run_tamper",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        protected_material_paths=("tests/test_calc.py",),
        verification={"integration_test_command": ("python3", "-m", "pytest", "-q")},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        outcome = await orchestrator.run("a tampering request", request_spec=spec)
    finally:
        await orchestrator.aclose()

    assert outcome.contract is not None
    # Either the scope held and the run was accepted on its own merits, or
    # the tamper was detected and the evidence voided.  What must never
    # happen is the tamper being missed.
    protected_states = [
        v.state
        for v in outcome.contract.verdicts
        if v.criterion_id.startswith("protected:")
    ]
    if protected_states:
        assert all(
            state is not OutcomeState.NOT_SATISFIED for state in protected_states
        ), "the protected file was modified and the run was not rejected"
    # The repository's own file was never changed by the run.
    assert protected.read_text(encoding="utf-8") == original
