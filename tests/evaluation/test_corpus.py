"""The held-out adversarial corpus for the contract gate.

These tests are the *release evidence* for the corpus criterion of the
scoring policy: a corpus of independent adversarial cases that the contract
gate must never accept, and a separate set of known-correct cases that it
must accept.

The default run uses a stratified sample -- the first two cases of every
mechanism family -- so that the suite stays fast while still exercising
every mechanism.  The full corpus (600 adversarial and 160 known-correct
cases) is run with::

    AGENT_SQUAD_CORPUS_FULL=1 .venv/bin/python -m pytest tests/evaluation/test_corpus.py -q

and it is the full run that asserts the confidence thresholds and writes
``benchmarks/results/corpus-<seed>.json``.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any

import pytest

from agent_squad.benchmark.corpus import (
    ACCEPTANCE_THRESHOLD,
    ADVERSARIAL_FAMILIES,
    CONFIDENCE,
    DEFAULT_SEED,
    EXCLUDED_CASES,
    FALSE_ACCEPT_THRESHOLD,
    KNOWN_CORRECT_FAMILIES,
    Expectation,
    build_report,
    default_report_path,
    generate_corpus,
    run_case,
    run_corpus,
    stratified_sample,
    write_report,
)
from agent_squad.benchmark.statistics import (
    clopper_pearson_lower,
    clopper_pearson_upper,
)
from agent_squad.models.contract import ContractVerdict, VerificationOutcome

#: How many cases per family the fast default run uses.
SAMPLE_PER_FAMILY = 2

#: The environment variable that switches on the full corpus run.
FULL_ENV = "AGENT_SQUAD_CORPUS_FULL"

#: The repository root, used to place the published report.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _full_requested() -> bool:
    """Whether the environment asks for the full corpus."""
    return os.environ.get(FULL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _report_path() -> Path:
    """Where the published report belongs."""
    return REPO_ROOT / default_report_path(DEFAULT_SEED)


# ---------------------------------------------------------------------------
# The shape of the corpus
# ---------------------------------------------------------------------------


def test_the_generator_is_deterministic() -> None:
    """The same seed must produce exactly the same corpus."""
    first = generate_corpus(DEFAULT_SEED, adversarial_count=20, known_correct_count=20)
    second = generate_corpus(DEFAULT_SEED, adversarial_count=20, known_correct_count=20)
    assert [c.model_dump(mode="json") for c in first.cases] == [
        c.model_dump(mode="json") for c in second.cases
    ]


def test_a_different_seed_produces_a_different_corpus() -> None:
    """The seed must actually select the cases, not just label them."""
    first = generate_corpus(DEFAULT_SEED, adversarial_count=20, known_correct_count=20)
    second = generate_corpus(DEFAULT_SEED + 1, adversarial_count=20, known_correct_count=20)
    assert [c.contract.contract_hash for c in first.cases] != [
        c.contract.contract_hash for c in second.cases
    ]


def test_the_corpus_is_large_enough_for_the_policy() -> None:
    """At least 300 adversarial and 100 known-correct cases."""
    corpus = generate_corpus(DEFAULT_SEED)
    assert len(corpus.adversarial) >= 300
    assert len(corpus.known_correct) >= 100


def test_every_case_id_is_unique() -> None:
    corpus = generate_corpus(DEFAULT_SEED)
    ids = [case.case_id for case in corpus.cases]
    assert len(ids) == len(set(ids))


def test_the_corpus_covers_the_categories_the_policy_names() -> None:
    """The policy names the mechanisms that must be represented."""
    categories = set(ADVERSARIAL_FAMILIES) | set(KNOWN_CORRECT_FAMILIES)
    # incompatible patch unions
    assert any("union" in name for name in categories)
    # unchanged baselines and empty patches
    assert {"pre_satisfied_no_op_tree", "no_op_diff_nonempty", "empty_patch_reverted"} <= categories
    # weak worker-written tests
    assert any(name.startswith("weak_test") for name in categories)
    # stale evidence
    assert any(name.startswith("stale") for name in categories)
    # legitimate no-ops
    assert {"legit_noop_permitted", "legit_noop_permitted_with_change"} <= categories
    # preserved baseline invariants
    assert any(name.startswith("preserved") for name in categories)
    assert any(name.startswith("broken_invariant") for name in categories)


def test_every_mechanism_family_is_actually_exercised() -> None:
    """A family that is never generated would be a silent hole."""
    corpus = generate_corpus(DEFAULT_SEED)
    seen = {case.category for case in corpus.cases}
    assert seen == set(ADVERSARIAL_FAMILIES) | set(KNOWN_CORRECT_FAMILIES)


def test_the_cases_in_a_family_are_not_cosmetic_variants() -> None:
    """Two cases of one family must differ in more than their label.

    This is the property the policy cares about: a family that only changes
    a random suffix would be one observation, not fifteen.
    """
    corpus = generate_corpus(DEFAULT_SEED)
    by_category: dict[str, list[Any]] = {}
    for case in corpus.cases:
        by_category.setdefault(case.category, []).append(case)
    for category, cases in by_category.items():
        assert len(cases) >= 2, category
        contracts = {case.contract.contract_hash for case in cases}
        objectives = {case.contract.objective for case in cases}
        trees = {
            (
                tuple(sorted(case.base.files)),
                tuple(sorted(case.candidate.files)),
                tuple(sorted(case.base.modes.items())),
                tuple(sorted(case.base.symlinks.items())),
                json.dumps(
                    [c.spec for c in case.contract.required_outcomes], sort_keys=True
                ),
                case.contract.allow_pre_satisfied,
            )
            for case in cases
        }
        assert len(contracts) == len(cases), f"{category}: identical contracts"
        assert len(objectives) == len(cases), f"{category}: identical objectives"
        assert len(trees) == len(cases), f"{category}: identical trees and specs"


def test_no_contract_is_reused_anywhere_in_the_corpus() -> None:
    """Independence is corpus-wide, not only within a family.

    Two different mechanisms could still share one requirement; that would
    be one observation with two family labels.  Every case must therefore
    carry its own contract hash, not merely its own family-local variant.
    """
    corpus = generate_corpus(DEFAULT_SEED)
    hashes = [case.contract.contract_hash for case in corpus.cases]
    duplicates = {
        digest: sum(1 for candidate in hashes if candidate == digest)
        for digest in set(hashes)
        if hashes.count(digest) > 1
    }
    assert not duplicates, duplicates
    assert len(hashes) == len(set(hashes))


def test_the_adversarial_set_distinguishes_rejected_from_inconclusive() -> None:
    """The corpus must not reduce every failure to one expected verdict.

    A verifier that collapses "unmet" and "unverifiable" into the same
    answer would otherwise look perfect.  The adversarial half must contain
    both genuinely unmet outcomes and cases where no deterministic
    conclusion is possible.
    """
    corpus = generate_corpus(DEFAULT_SEED)
    expected = {case.expected_outcome for case in corpus.adversarial}
    assert VerificationOutcome.REJECTED in expected
    assert VerificationOutcome.INCONCLUSIVE in expected
    assert all(
        case.expected_outcome is VerificationOutcome.ACCEPTED
        for case in corpus.known_correct
    )


def test_the_policy_thresholds_follow_from_exact_binomial_limits() -> None:
    """The headline arithmetic must be exact, not a normal approximation.

    The closed forms for the boundary cases are known: zero events in ``n``
    trials give ``1 - alpha ** (1 / n)`` and ``n`` of ``n`` successes give
    ``alpha ** (1 / n)``.  Checking against them proves the implementation
    inverts the beta distribution rather than using a Wald or Wilson
    approximation (a Wald interval on zero events has zero width).
    """
    assert clopper_pearson_upper(0, 300) == pytest.approx(
        1.0 - 0.05 ** (1.0 / 300.0), rel=1e-12
    )
    assert clopper_pearson_lower(100, 100) == pytest.approx(
        0.05 ** (1.0 / 100.0), rel=1e-12
    )
    # 300/300 false-accept-free cases clear the 1% ceiling...
    assert clopper_pearson_upper(0, 300) < FALSE_ACCEPT_THRESHOLD
    # ...and 100/100 accepted known-correct cases clear the 95% floor.
    assert clopper_pearson_lower(100, 100) >= ACCEPTANCE_THRESHOLD
    assert math.isclose(clopper_pearson_upper(0, 600), 0.00498044338036, rel_tol=1e-9)


def test_the_known_correct_set_contains_no_ops_and_preserved_invariants() -> None:
    """An always-reject verifier must not be able to pass this corpus."""
    corpus = generate_corpus(DEFAULT_SEED)
    categories = {case.category for case in corpus.known_correct}
    assert "legit_noop_permitted" in categories
    assert "legit_noop_permitted_with_change" in categories
    assert "preserved_invariant_touched" in categories
    assert "preserved_test_suite" in categories
    assert "preserved_multiple_invariants" in categories


def test_the_excluded_mechanisms_are_published_rather_than_hidden() -> None:
    """A corpus that hides its blind spots is not evidence."""
    assert EXCLUDED_CASES
    for entry in EXCLUDED_CASES:
        assert entry["category"]
        assert entry["mechanism"]
        assert entry["status"]


# ---------------------------------------------------------------------------
# Running the corpus
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus():
    """The full corpus; generating it is cheap, running it is not."""
    return generate_corpus(DEFAULT_SEED)


@pytest.fixture(scope="module")
def sample_results(corpus, tmp_path_factory):
    """The fast stratified sample, run through the real gate.

    The fixture is synchronous on purpose: it owns its own event loop, so
    the sample is run once for the whole module instead of once per test.
    """
    sample = stratified_sample(corpus, SAMPLE_PER_FAMILY)
    workspace = tmp_path_factory.mktemp("corpus-sample")
    return asyncio.run(run_corpus(sample, workspace=workspace))


async def test_the_sample_has_no_false_accepts(sample_results) -> None:
    """No adversarial case may be accepted."""
    false_accepts = [r for r in sample_results if r.false_accept]
    assert not false_accepts, [
        (r.case_id, r.actual_outcome.value, r.detail) for r in false_accepts
    ]


async def test_the_sample_has_no_false_rejects(sample_results) -> None:
    """Every known-correct case must be accepted."""
    false_rejects = [r for r in sample_results if r.false_reject]
    assert not false_rejects, [
        (r.case_id, r.actual_outcome.value, r.detail) for r in false_rejects
    ]

async def test_every_case_is_judged_the_way_it_is_meant_to_be(sample_results) -> None:
    """The verdict must be the expected one, not merely 'not accepted'.

    A case that is rejected for an unrelated reason would still satisfy a
    naive false-accept count, so the exact verdict is checked too.
    """
    mismatches = [r for r in sample_results if r.outcome_mismatch]
    assert not mismatches, [
        (r.case_id, r.expected_outcome.value, r.actual_outcome.value, r.detail)
        for r in mismatches
    ]


async def test_the_sample_covers_every_mechanism(corpus, sample_results) -> None:
    """The fast sample must still exercise every mechanism family."""
    assert {r.category for r in sample_results} == set(ADVERSARIAL_FAMILIES) | set(
        KNOWN_CORRECT_FAMILIES
    )


# ---------------------------------------------------------------------------
# The degenerate verifiers
# ---------------------------------------------------------------------------


class _AlwaysRejectGate:
    """A verifier that rejects everything.

    It exists to prove that the known-correct set is not vacuous: a
    verifier that never accepts anything must fail the corpus, so the
    corpus cannot be satisfied by rejecting indiscriminately.
    """

    async def verify(self, contract, *, base_tree, candidate_tree, **kwargs) -> ContractVerdict:
        return ContractVerdict(
            outcome=VerificationOutcome.REJECTED,
            contract_hash=contract.contract_hash,
            detail="always reject",
        )


class _AlwaysAcceptGate:
    """A verifier that accepts everything.

    It is the mirror image: it must fail the adversarial half of the
    corpus, so the corpus cannot be satisfied by accepting indiscriminately.
    """

    async def verify(self, contract, *, base_tree, candidate_tree, **kwargs) -> ContractVerdict:
        return ContractVerdict(
            outcome=VerificationOutcome.ACCEPTED,
            contract_hash=contract.contract_hash,
            detail="always accept",
        )


async def test_an_always_reject_verifier_fails_the_known_correct_set(
    corpus, tmp_path
) -> None:
    """The known-correct set is what stops 'reject everything' from passing."""
    from agent_squad.config import RunConfig
    from agent_squad.verification.contract_gate import ContractGate

    gate = _AlwaysRejectGate()
    always_reject_accepted = 0
    real_accepted = 0
    sample = stratified_sample(corpus, SAMPLE_PER_FAMILY).known_correct
    real_gate = ContractGate(RunConfig(run_id="corpus", repo_path=str(tmp_path)))
    for case in sample:
        stub_result = await run_case(case, gate, tmp_path)
        real_result = await run_case(case, real_gate, tmp_path)
        always_reject_accepted += int(
            stub_result.actual_outcome is VerificationOutcome.ACCEPTED
        )
        real_accepted += int(real_result.actual_outcome is VerificationOutcome.ACCEPTED)
    assert always_reject_accepted == 0, "the stub is not rejecting everything"
    assert real_accepted == len(sample)
    # The policy test is not merely "the stub accepted nothing": the
    # acceptance rate and its exact lower bound must fail the bar that the
    # known-correct set sets.  An always-reject verifier scores 0% and a 0%
    # Clopper-Pearson lower limit, so it cannot pass this corpus.
    always_reject_rate = always_reject_accepted / len(sample)
    assert always_reject_rate < ACCEPTANCE_THRESHOLD
    assert (
        clopper_pearson_lower(always_reject_accepted, len(sample))
        < ACCEPTANCE_THRESHOLD
    )


async def test_an_always_accept_verifier_fails_the_adversarial_set(
    corpus, tmp_path
) -> None:
    """The adversarial set is what stops 'accept everything' from passing."""
    gate = _AlwaysAcceptGate()
    accepted = 0
    sample = stratified_sample(corpus, SAMPLE_PER_FAMILY).adversarial
    for case in sample:
        result = await run_case(case, gate, tmp_path)
        accepted += int(result.actual_outcome is VerificationOutcome.ACCEPTED)
    assert accepted == len(sample)


# ---------------------------------------------------------------------------
# The full corpus
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def full_results(corpus, tmp_path_factory):
    """The complete corpus, run once through the real gate.

    The three release-evidence tests below all read the same run.  Re-running
    the 600+160 cases for each assertion would triple the cost without adding
    a single observation.
    """
    if not _full_requested():  # pragma: no cover - the tests are skipped
        pytest.skip(f"set {FULL_ENV}=1 to run the full corpus")
    workspace = tmp_path_factory.mktemp("corpus-full")
    return asyncio.run(run_corpus(corpus, workspace=workspace))


@pytest.mark.slow
@pytest.mark.skipif(
    not _full_requested(),
    reason=f"set {FULL_ENV}=1 to run the full corpus",
)
async def test_the_full_corpus_meets_the_policy_thresholds(corpus, full_results) -> None:
    """The published numbers: zero false accepts, and both limits hold."""
    report = build_report(corpus, full_results, duration_s=0.0)

    false_accepts = [r for r in full_results if r.false_accept]
    assert not false_accepts, [
        (r.case_id, r.actual_outcome.value, r.detail) for r in false_accepts
    ]
    false_rejects = [r for r in full_results if r.false_reject]
    assert not false_rejects, [
        (r.case_id, r.actual_outcome.value, r.detail) for r in false_rejects
    ]

    adversarial = report.adversarial
    assert adversarial["cases"] >= 300
    assert adversarial["false_accepts"] == 0
    assert adversarial["confidence"] == CONFIDENCE
    assert adversarial["method"] == "clopper-pearson"
    assert adversarial["upper_confidence_limit_95"] < FALSE_ACCEPT_THRESHOLD
    assert adversarial["meets_threshold"] is True

    known = report.known_correct
    assert known["cases"] >= 100
    assert known["acceptance_rate"] >= ACCEPTANCE_THRESHOLD
    assert known["lower_confidence_limit_95"] >= ACCEPTANCE_THRESHOLD
    assert known["meets_threshold"] is True

    # The clustered analysis must not be *worse* than the case-level one:
    # the corpus is built so that the effective sample is the full sample.
    assert adversarial["clustering"]["effective_sample_size"] >= 0.9 * adversarial["cases"]
    assert adversarial["clustered_upper_confidence_limit_95"] <= (
        adversarial["upper_confidence_limit_95"] * 1.05
    )

    # The pessimistic "one whole family is broken" bound must also clear
    # the threshold, otherwise the headline number rests on the
    # independence argument alone.
    assert adversarial["family_level"]["worst_single_family_contribution_95"] < (
        FALSE_ACCEPT_THRESHOLD
    )


@pytest.mark.slow
@pytest.mark.skipif(
    not _full_requested(),
    reason=f"set {FULL_ENV}=1 to run the full corpus",
)
async def test_the_full_corpus_writes_a_machine_readable_report(
    corpus, full_results
) -> None:
    """The report is published so a reviewer can check the arithmetic."""
    report = build_report(corpus, full_results, duration_s=0.0)
    path = write_report(report, _report_path())
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["seed"] == DEFAULT_SEED
    assert payload["adversarial"]["cases"] >= 300
    assert payload["known_correct"]["cases"] >= 100
    assert payload["adversarial"]["false_accepts"] == 0
    assert payload["known_correct"]["false_rejects"] == 0
    assert payload["summary"]["upper_limit_method"] == "clopper-pearson"
    assert payload["summary"]["lower_limit_method"] == "clopper-pearson"
    assert payload["summary"]["adversarial_effective_sample_size"] > 0
    assert payload["summary"]["known_correct_effective_sample_size"] > 0
    assert len(payload["cases"]) == len(corpus.cases)
    assert payload["excluded_cases"]
    assert payload["design"]["independence_argument"]
    assert payload["design"]["effective_sample_justification"]


@pytest.mark.slow
@pytest.mark.skipif(
    not _full_requested(),
    reason=f"set {FULL_ENV}=1 to run the full corpus",
)
async def test_the_published_report_is_up_to_date(corpus) -> None:
    """The checked-in report must describe the corpus the seed produces."""
    path = _report_path()
    if not path.exists():
        pytest.skip("no published report yet")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["seed"] == DEFAULT_SEED
    assert payload["adversarial"]["cases"] == len(corpus.adversarial)
    assert payload["known_correct"]["cases"] == len(corpus.known_correct)
    assert payload["adversarial"]["false_accepts"] == 0
    assert payload["known_correct"]["false_rejects"] == 0
    assert payload["adversarial"]["upper_confidence_limit_95"] < FALSE_ACCEPT_THRESHOLD
    assert payload["known_correct"]["lower_confidence_limit_95"] >= ACCEPTANCE_THRESHOLD


def test_the_expectation_vocabulary_is_explicit() -> None:
    """The corpus distinguishes "must accept" from "must not accept"."""
    assert Expectation.ACCEPT.value == "accept"
    assert Expectation.REJECT.value == "reject"
