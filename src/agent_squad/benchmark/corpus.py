"""A held-out adversarial corpus for the contract gate.

Why this module exists
-----------------------

The scoring policy asks for a corpus of at least 300 independent adversarial
cases with zero false accepts, plus at least 100 known-correct cases that a
correct verifier must accept.  A corpus of *cosmetic variants* would not
establish anything: a verifier that fails one spelling of a case fails all
of them, so the confidence limit computed over them would be fiction.

So the corpus is built from **mechanism families**.  Each family is a
distinct way a run can report success while the request was not satisfied:
the patch union dropped one side, the hidden regression test was replaced by
a trivial one, the candidate tree is an older revision that silently reverts
a base change, the check timed out, and so on.  Within a family the case is
still randomised -- module names, file names, markers, contents, paths and
commands all vary -- so that the family is a *mechanism*, not a fixed script.

Independence
------------

The policy also says that correlated cases need a justified effective
sample.  The justification here is structural, and it is worth stating
precisely because the claim is otherwise unfalsifiable:

1. **One randomisation unit per case.**  Every case draws its randomness
   from its own PRNG stream, seeded from ``(seed, case_id)``.  No two cases
   share a random draw, so no two cases share an "instance" of anything.
   The thing that is repeated across a family is the *mechanism*, and the
   mechanism is a property of the adversarial space, not a shared outcome.
2. **The family label is a randomised covariate, not a block.**  The
   assignment of cases to families is a random permutation of a balanced
   list, so the family of a case is not fixed by its position, and the
   mechanism mix is declared rather than accidental.
3. **Every case gets a fresh workspace.**  No case reads or writes another
   case's tree, so no case can be affected by another case's outcome.
4. **The only shared component is the verifier itself.**  A shared
   verifier is a *systematic* effect: it biases the false-accept rate, but
   it does not inflate the sampling variance of the rate estimate the way
   correlated draws would.  Its effect is bounded by the family analysis
   reported alongside the headline number.

The report therefore carries three numbers, not one:

* the **case-level** exact Clopper-Pearson limit, which is the figure the
  policy asks for and which is valid under the independence argument above;
* a **cluster-robust** limit computed at the effective sample size implied
  by the observed intra-cluster correlation, which is the clustered
  adjustment the policy asks for;
* a **family-level** bound that asks the pessimistic question "what if one
  whole family is broken in a way this corpus did not happen to catch?",
  because that is the failure mode a reviewer is right to worry about.

The honest summary is published in the report's ``design`` section rather
than left for the reader to infer.

What the corpus deliberately does *not* claim
---------------------------------------------

The corpus is a corpus for the *contract gate*.  It cannot certify the
whole system: a case that the gate rejects for the wrong reason still
counts as "not a false accept".  The runner therefore records the exact
verdict for every case next to the coarse accept/reject expectation, so a
reviewer can see whether a case was rejected for the intended reason.  The
excluded-case list records the mechanisms that are known *not* to be caught
by the current verifier, rather than quietly leaving them out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import RunConfig
from ..models.contract import RequestContract, VerificationOutcome
from ..models.task import AcceptanceCriterion, CheckKind
from ..verification.contract_gate import VERIFIER_VERSION, ContractGate
from .statistics import (
    binomial_limit,
    clopper_pearson_lower,
    clopper_pearson_upper,
    effective_sample_size,
    intra_cluster_correlation,
    kish_design_effect,
)

#: The seed the published corpus report is generated from.
DEFAULT_SEED = 20261111

#: The default number of adversarial cases.  The policy asks for 300; the
#: corpus is larger so that the *clustered* analysis also clears the
#: threshold, and so that a handful of unexpected cases cannot decide the
#: outcome.
DEFAULT_ADVERSARIAL_CASES = 600

#: The default number of known-correct cases.  The policy asks for 100.
DEFAULT_KNOWN_CORRECT_CASES = 160

#: The one-sided confidence level used for every limit in the report.
CONFIDENCE = 0.95

#: The false-accept ceiling the policy sets.
FALSE_ACCEPT_THRESHOLD = 0.01

#: The acceptance floor the policy sets for the known-correct set.
ACCEPTANCE_THRESHOLD = 0.95


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Expectation(str, Enum):
    """What a correct verifier must do with a case."""

    ACCEPT = "accept"
    """The request really was satisfied; the verifier must accept it."""

    REJECT = "reject"
    """The request was not satisfied; the verifier must not accept it."""


class TreeSpec(BaseModel):
    """A workspace to materialise: files, permissions and symlinks."""

    model_config = ConfigDict(frozen=True)

    files: dict[str, str] = Field(default_factory=dict)
    modes: dict[str, int] = Field(default_factory=dict)
    symlinks: dict[str, str] = Field(default_factory=dict)


class CorpusCase(BaseModel):
    """One case: a contract plus the two trees it is judged against."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    category: str
    """The mechanism family.  This is the clustering unit of the corpus."""
    mechanism: str
    """A one-line description of the failure mechanism being exercised."""
    expectation: Expectation
    expected_outcome: VerificationOutcome
    contract: RequestContract
    base: TreeSpec
    candidate: TreeSpec


class Corpus(BaseModel):
    """A generated corpus: adversarial cases plus known-correct cases."""

    model_config = ConfigDict(frozen=True)

    seed: int
    adversarial: tuple[CorpusCase, ...] = ()
    known_correct: tuple[CorpusCase, ...] = ()

    @property
    def cases(self) -> tuple[CorpusCase, ...]:
        return self.adversarial + self.known_correct


@dataclass
class CaseResult:
    """The recorded outcome of running one case through the real gate."""

    case_id: str
    category: str
    mechanism: str
    expectation: Expectation
    expected_outcome: VerificationOutcome
    actual_outcome: VerificationOutcome
    false_accept: bool
    false_reject: bool
    outcome_mismatch: bool
    detail: str
    states: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "mechanism": self.mechanism,
            "expectation": self.expectation.value,
            "expected_outcome": self.expected_outcome.value,
            "actual_outcome": self.actual_outcome.value,
            "false_accept": self.false_accept,
            "false_reject": self.false_reject,
            "outcome_mismatch": self.outcome_mismatch,
            "detail": self.detail,
            "states": self.states,
            "duration_s": round(self.duration_s, 4),
        }


# ---------------------------------------------------------------------------
# Randomised building blocks
# ---------------------------------------------------------------------------

#: A vocabulary of harmless words.  The generated source text uses only
#: these, so a case can never accidentally satisfy a criterion by accident.
_WORDS = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango",
)

_STEMS = ("calc", "util", "helper", "store", "router", "parser", "engine", "cache")


def _module(rng: random.Random) -> str:
    """A random, importable module name."""
    return f"{rng.choice(_STEMS)}_{rng.choice(_WORDS)}_{rng.randrange(1000, 9999)}"


def _modules(rng: random.Random, count: int) -> list[str]:
    """``count`` *distinct* random module names.

    Distinctness matters: if two generated names collided, a case that is
    supposed to touch a second, unrelated file would end up touching the
    subject of the contract instead, and the case would silently stop
    testing the mechanism it was written for.
    """
    names: list[str] = []
    while len(names) < count:
        name = _module(rng)
        if name not in names:
            names.append(name)
    return names


def _blob(rng: random.Random, lines: int = 4) -> str:
    """Deterministic pseudo-source text with no searchable markers in it."""
    out: list[str] = []
    for _ in range(lines):
        first = rng.choice(_WORDS)
        second = rng.choice(_WORDS)
        out.append(
            f"{first}_{second} = {rng.randrange(10, 9999)}\n"
        )
    return "".join(out)


def _marker(rng: random.Random) -> str:
    """A unique marker string that appears in exactly one generated file."""
    return f"__MARKER_{rng.choice(_WORDS).upper()}_{rng.randrange(1000, 9999)}__"


def _markers(rng: random.Random, count: int) -> list[str]:
    """``count`` distinct marker strings."""
    markers: list[str] = []
    while len(markers) < count:
        marker = _marker(rng)
        if marker not in markers:
            markers.append(marker)
    return markers


def _unicode_stem(rng: random.Random) -> str:
    """A file stem that is guaranteed to have distinct NFC and NFD forms.

    A word-only stem such as ``rapport_alpha`` is pure ASCII, so NFC and NFD
    are the same string and the two cases that used it were byte-identical.
    The accented character is what makes the normalisation mismatch a real
    distinction rather than a label on one shared case.
    """
    accent = rng.choice(("é", "ü", "ñ", "ç", "å", "ø", "œ", "æ"))
    return f"rapport_{rng.choice(_WORDS)}_{rng.randrange(1000, 9999)}_{accent}"


def _criterion(
    criterion_id: str,
    kind: str,
    spec: dict[str, Any],
    *,
    description: str | None = None,
    **kwargs: Any,
) -> AcceptanceCriterion:
    """Build one acceptance criterion.

    ``description`` defaults to the id, but families that share a mechanism
    and a fixed spec still need a materially different requirement for every
    case.  Callers pass a concrete description naming the file, pattern or
    command that case is about, so the contract hash changes because the
    requirement changed -- not because a cosmetic suffix was appended.
    """
    return AcceptanceCriterion(
        id=criterion_id,
        description=description or criterion_id,
        kind=CheckKind(kind),
        spec=spec,
        **kwargs,
    )


def _contract(
    objective: str,
    criteria: Iterable[AcceptanceCriterion],
    *,
    allow_pre_satisfied: bool = False,
) -> RequestContract:
    """Build a request contract from its required outcomes."""
    return RequestContract(
        objective=objective,
        required_outcomes=tuple(criteria),
        allow_pre_satisfied=allow_pre_satisfied,
    )


# ---------------------------------------------------------------------------
# The adversarial families
# ---------------------------------------------------------------------------


def _pre_satisfied_untouched(rng: random.Random, case_id: str) -> CorpusCase:
    """An outcome that already held, about a file the run never touched.

    The run did change the tree -- it added an unrelated module -- so the
    "the run changed nothing" rule does not fire.  What fires is the rule
    that an outcome which was already true and whose subject was not
    touched establishes nothing about the work.
    """
    mod, other = _modules(rng, 2)
    path = f"src/{mod}.py"
    base = TreeSpec(files={path: _blob(rng), "README.md": "# fixture\n"})
    candidate = TreeSpec(files={**base.files, f"src/{other}.py": _blob(rng)})
    contract = _contract(
        f"keep {path} free of TODO markers",
        (_criterion("no-todo", "file_not_contains", {"path": path, "pattern": "TODO"}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="pre_satisfied_untouched",
        mechanism="a required outcome was already true and the run never touched its subject",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _pre_satisfied_no_op_tree(rng: random.Random, case_id: str) -> CorpusCase:
    """The candidate tree is byte-identical to the base tree."""
    mod = _module(rng)
    path = f"{mod}.py"
    files = {path: _blob(rng), "README.md": "# fixture\n"}
    base = TreeSpec(files=files)
    candidate = TreeSpec(files=dict(files))
    contract = _contract(
        f"confirm {path} exists",
        (_criterion("exists", "file_exists", {"path": path}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="pre_satisfied_no_op_tree",
        mechanism="the run changed nothing at all: the candidate tree is byte-identical to the base",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _no_op_diff_nonempty(rng: random.Random, case_id: str) -> CorpusCase:
    """The run produced an empty patch while the contract demands a change.

    The demanded change names a different subject each time, and some
    contracts also require an invariant to survive.  The empty patch fails
    the ``diff_nonempty`` outcome in every case, so the verdict is rejected
    regardless of whether a no-op is permitted.
    """
    mod, other = _modules(rng, 2)
    path = f"src/{mod}.py"
    kept = f"src/{other}.py"
    files = {path: _blob(rng), kept: _blob(rng), "README.md": "# fixture\n"}
    objective = rng.choice(
        (
            f"produce a change to {path}",
            f"make the requested change in {path}",
            f"update {path} as requested",
        )
    )
    criteria = [
        _criterion(
            f"changed-{mod}",
            "diff_nonempty",
            {},
            description=f"the run must change {path}",
        )
    ]
    shape = rng.randrange(3)
    if shape == 1:
        criteria.append(
            _criterion(
                f"still-present-{other}",
                "file_exists",
                {"path": kept},
                description=f"{kept} must remain present",
            )
        )
    elif shape == 2:
        criteria.append(
            _criterion(
                f"still-clean-{other}",
                "file_not_contains",
                {"path": kept, "pattern": "TODO"},
                description=f"{kept} must stay free of TODO markers",
            )
        )
    contract = _contract(
        objective,
        tuple(criteria),
        allow_pre_satisfied=rng.random() < 0.25,
    )
    return CorpusCase(
        case_id=case_id,
        category="no_op_diff_nonempty",
        mechanism="the patch was empty, so the required change was never made",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=TreeSpec(files=dict(files)),
        candidate=TreeSpec(files=dict(files)),
    )


def _empty_patch_reverted(rng: random.Random, case_id: str) -> CorpusCase:
    """The run's change was reverted, so the requested artefact is absent."""
    mod, other = _modules(rng, 2)
    wanted = f"src/{mod}.py"
    files = {"README.md": "# fixture\n", f"src/{other}.py": _blob(rng)}
    contract = _contract(
        f"add {wanted}",
        (_criterion("added", "file_exists", {"path": wanted}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="empty_patch_reverted",
        mechanism="the run's change was reverted, so the requested file does not exist",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=TreeSpec(files=files),
        candidate=TreeSpec(files=dict(files)),
    )


def _permitted_noop_but_unmet(rng: random.Random, case_id: str) -> CorpusCase:
    """A contract that permits a no-op, plus an outcome that is still unmet.

    ``allow_pre_satisfied`` is a permission, not a waiver: it lets an
    already-true outcome count, and it must not let an unmet outcome
    count.
    """
    mod, other = _modules(rng, 2)
    present = f"{mod}.py"
    missing = f"src/{other}.py"
    files = {present: _blob(rng)}
    candidate = TreeSpec(files={**files, f"notes_{rng.randrange(100, 999)}.md": "# note\n"})
    contract = _contract(
        f"confirm {present} and add {missing}",
        (
            _criterion("present", "file_exists", {"path": present}),
            _criterion("missing", "file_exists", {"path": missing}),
        ),
        allow_pre_satisfied=True,
    )
    return CorpusCase(
        case_id=case_id,
        category="permitted_noop_but_unmet_outcome",
        mechanism="the contract permits a pre-satisfied outcome but another outcome is still unmet",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=TreeSpec(files=files),
        candidate=candidate,
    )


def _union_lost_side(rng: random.Random, case_id: str) -> CorpusCase:
    """Two writers touched one file; the merge kept only the second side."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    lost, kept = _markers(rng, 2)
    base = TreeSpec(files={path: _blob(rng)})
    candidate = TreeSpec(files={path: f"{_blob(rng)}{kept} = 1\n"})
    contract = _contract(
        f"both writers' changes to {path} must survive the merge",
        (_criterion("lost-side", "file_contains", {"path": path, "pattern": lost}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="union_lost_side",
        mechanism="the merged patch union silently dropped one writer's change",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _union_broken_syntax(rng: random.Random, case_id: str) -> CorpusCase:
    """The merged file kept both sides but no longer parses."""
    mod = _module(rng)
    path = f"{mod}.py"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={path: "def broken(:\n    return 1\n"})
    contract = _contract(
        f"{mod} must import cleanly",
        (_criterion("imports", "command_exits_zero", {"command": ["python3", "-c", f"import {mod}"]}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="union_broken_syntax",
        mechanism="the merged file is syntactically invalid, so the module cannot be imported",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _union_marker_wrong_file(rng: random.Random, case_id: str) -> CorpusCase:
    """The lost side's marker survived, but in the wrong file."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    lost = _marker(rng)
    base = TreeSpec(files={path: _blob(rng)})
    candidate = TreeSpec(files={path: _blob(rng), "notes.txt": f"{lost}\n"})
    contract = _contract(
        f"{path} must contain the marker",
        (_criterion("marker", "file_contains", {"path": path, "pattern": lost}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="union_marker_wrong_file",
        mechanism="the marker survived the merge in an unrelated file, so the required file lacks it",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _union_drops_behaviour(rng: random.Random, case_id: str) -> CorpusCase:
    """The conflict resolver kept the last writer, so a behaviour vanished."""
    mod = _module(rng)
    good = rng.randrange(100000, 999999)
    bad = rng.randrange(100000, 999999)
    path = f"{mod}.py"
    base = TreeSpec(files={path: f"def value():\n    return {good}\n"})
    candidate = TreeSpec(files={path: f"def value():\n    return {bad}\n"})
    contract = _contract(
        f"value() must still return {good}",
        (
            _criterion(
                "value",
                "command_output_matches",
                {"command": ["python3", "-c", f"import {mod}; print({mod}.value())"], "pattern": str(good)},
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="union_drops_behaviour",
        mechanism="the conflict resolution dropped one writer's behaviour even though the file still exists",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _weak_test_replaces_suite(rng: random.Random, case_id: str) -> CorpusCase:
    """The worker replaced the hidden regression test with a trivial one.

    The suite file and its marker are both drawn per case, so the contract
    names a different hidden test each time rather than repeating one path.
    """
    mod = _module(rng)
    suite = f"test_hidden_{rng.choice(_STEMS)}_{rng.randrange(1000, 9999)}"
    path = f"tests/{rng.choice(('unit', 'regression', 'guards'))}/{suite}.py"
    marker = (
        f"# ORIGINAL_SUITE_{rng.choice(_WORDS).upper()}_{rng.randrange(1000, 9999)}"
    )
    left = rng.randrange(2, 12)
    right = rng.randrange(2, 12)
    base = TreeSpec(
        files={
            path: (
                f"{marker}\n\ndef {suite}():\n"
                f"    assert {left} + {right} == {left + right}\n"
            )
        }
    )
    candidate = TreeSpec(
        files={
            path: f"def {suite}():\n    pass\n",
            f"src/{mod}.py": _blob(rng),
        }
    )
    contract = _contract(
        f"the hidden regression test in {path} must still be the original one",
        (
            _criterion(
                f"original-suite-{suite}",
                "file_contains",
                {"path": path, "pattern": marker},
                description=f"{path} must still contain {marker}",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="weak_test_replaces_suite",
        mechanism="the worker rewrote the hidden test into a trivial one that proves nothing",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _weak_test_feature_missing(rng: random.Random, case_id: str) -> CorpusCase:
    """The worker wrote a passing test for a feature it never wrote."""
    mod, worker = _modules(rng, 2)
    feature = f"src/{mod}.py"
    worker_test = f"tests/test_worker_{worker}.py"
    candidate = TreeSpec(
        files={
            worker_test: (
                f"def test_worker_{worker}():\n"
                f"    assert {rng.randrange(2, 99)} + {rng.randrange(2, 99)} > 0\n"
            ),
            f"notes/{worker}.md": "# worker notes\n",
        }
    )
    contract = _contract(
        f"implement {feature}",
        (
            _criterion(
                f"feature-{mod}",
                "file_exists",
                {"path": feature},
                description=f"the requested module {feature} must exist",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="weak_test_feature_missing",
        mechanism="the worker wrote a trivial passing test for a feature it never implemented",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=TreeSpec(files={"README.md": "# fixture\n"}),
        candidate=candidate,
    )


def _weak_test_asserts_nothing(rng: random.Random, case_id: str) -> CorpusCase:
    """The worker's own test asserts nothing, and the required test fails.

    Both the required module and the worker's module are named per case and
    their bodies vary.  Previously every case shared one path, one body and
    one contract, so the family contributed exactly one observation.
    """
    required = f"test_{rng.choice(_STEMS)}_{rng.randrange(1000, 9999)}"
    path = f"tests/{rng.choice(('unit', 'regression'))}/{required}.py"
    worker = f"test_worker_{rng.choice(_STEMS)}_{rng.randrange(1000, 9999)}"
    left = rng.randrange(2, 12)
    right = rng.randrange(2, 12)
    truth = left + right
    base = TreeSpec(files={path: f"def {required}():\n    assert {left} + {right} == {truth}\n"})
    candidate = TreeSpec(
        files={
            path: f"def {required}():\n    assert {left} + {right} == {truth + 1}\n",
            f"tests/worker/{worker}.py": f"def {worker}():\n    payload = {rng.randrange(2, 999)}\n",
        }
    )
    contract = _contract(
        f"the required regression test {path} must pass",
        (
            _criterion(
                f"required-test-{required}",
                "pytest_selector",
                {"selector": path},
                description=f"the required test module {path} must pass",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="weak_test_asserts_nothing",
        mechanism="the worker wrote a test that asserts nothing while the required test fails",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _weak_test_shadowed_by_stale_copy(rng: random.Random, case_id: str) -> CorpusCase:
    """A stale copy of the test module shadows the real one during collection.

    The original suite, the backup directory and the stale test function all
    vary.  The contract names the concrete suite so two cases do not share a
    hash even though the selector covers the same ``tests`` root.
    """
    stem = f"test_{rng.choice(_STEMS)}_{rng.randrange(1000, 9999)}"
    subdir = rng.choice(("unit", "regression", "components"))
    original_path = f"tests/{subdir}/{stem}.py"
    backup = f"tests/backup_{rng.randrange(10, 999)}"
    stale_path = f"{backup}/{stem}.py"
    left = rng.randrange(2, 12)
    right = rng.randrange(2, 12)
    original = f"def {stem}():\n    assert {left} + {right} == {left + right}\n"
    stale = f"def test_stale_{rng.choice(_STEMS)}():\n    assert True\n"
    base = TreeSpec(files={original_path: original})
    candidate = TreeSpec(files={original_path: original, stale_path: stale})
    contract = _contract(
        f"the suite containing {original_path} and its backup copies must stay collectable",
        (
            _criterion(
                f"suite-{stem}",
                "pytest_selector",
                {"selector": "tests"},
                description=f"the suite containing {original_path} must collect and pass",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="weak_test_shadowed_by_stale_copy",
        mechanism="a stale copy of the test module shadows the real one and breaks collection",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _stale_base_revert(rng: random.Random, case_id: str) -> CorpusCase:
    """The candidate tree is an older revision that reverts a base change."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    symbol = f"def {rng.choice(_WORDS)}_{rng.randrange(100, 999)}("
    base = TreeSpec(files={path: f"{_blob(rng)}\n{symbol}    return 1\n"})
    candidate = TreeSpec(files={path: _blob(rng)})
    contract = _contract(
        f"{path} must still expose the base API",
        (_criterion("api", "file_contains", {"path": path, "pattern": symbol}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="stale_base_revert",
        mechanism="the candidate tree came from an older revision and silently reverted a base change",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _stale_artifact_copied(rng: random.Random, case_id: str) -> CorpusCase:
    """A report artifact was copied from an earlier attempt."""
    mod = _module(rng)
    report = f"reports/{mod}.txt"
    marker = _marker(rng)
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={report: f"STATUS: STALE\n{marker}\n"})
    contract = _contract(
        f"{report} must record the completed status",
        (_criterion("status", "file_contains", {"path": report, "pattern": "STATUS: DONE"}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="stale_artifact_copied",
        mechanism="a stale report artifact from an earlier attempt was reused",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _stale_test_snapshot(rng: random.Random, case_id: str) -> CorpusCase:
    """A test file from an earlier attempt refers to a function that is gone."""
    mod = _module(rng)
    path = f"tests/test_snapshot_{mod}.py"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={path: f"from {mod} import missing_function\n\ndef test_snapshot():\n    assert missing_function()\n"}
    )
    contract = _contract(
        f"the stale test snapshot {path} must not be part of the suite",
        (_criterion("snapshot", "pytest_selector", {"selector": path}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="stale_test_snapshot",
        mechanism="a test snapshot from an earlier attempt imports a function that no longer exists",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _stale_evidence_claims_success(rng: random.Random, case_id: str) -> CorpusCase:
    """The workspace carries stale evidence asserting that the work succeeded."""
    mod, other = _modules(rng, 2)
    feature = f"src/{mod}.py"
    candidate = TreeSpec(
        files={
            "evidence/verdict.json": (
                '{"outcome": "accepted", "note": '
                f'"from an earlier attempt {rng.choice(_WORDS)}_{rng.randrange(100, 999)}"}}\n'
            ),
            f"src/{other}.py": _blob(rng),
            "README.md": "# fixture\n",
        }
    )
    contract = _contract(
        f"implement {feature}",
        (
            _criterion(
                f"feature-{mod}",
                "file_exists",
                {"path": feature},
                description=f"the requested module {feature} must exist",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="stale_evidence_claims_success",
        mechanism="stale evidence from an earlier attempt claims success while the feature is absent",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=TreeSpec(files={"README.md": "# fixture\n"}),
        candidate=candidate,
    )


def _broken_invariant_marker(rng: random.Random, case_id: str) -> CorpusCase:
    """The run introduced a marker the baseline did not contain."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    forbidden = f"import {rng.choice(_WORDS)}"
    base = TreeSpec(files={path: _blob(rng)})
    candidate = TreeSpec(files={path: f"{forbidden}\n{_blob(rng)}"})
    contract = _contract(
        f"{path} must not import {forbidden.split()[1]}",
        (_criterion("no-import", "file_not_contains", {"path": path, "pattern": forbidden}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="broken_invariant_marker",
        mechanism="the run broke a baseline invariant by adding a forbidden marker",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _broken_invariant_test_suite(rng: random.Random, case_id: str) -> CorpusCase:
    """The run broke the baseline test suite.

    The suite path, test function, arithmetic fact and the unrelated module
    the run added all vary.  A family that always broke
    ``tests/test_baseline.py`` would be one observation with sixteen labels:
    a verifier that mishandles that one file would decide the whole family.
    """
    mod = _module(rng)
    suite = f"test_{rng.choice(_STEMS)}_{rng.randrange(1000, 9999)}"
    subdir = rng.choice(("unit", "integration", "regression"))
    path = f"tests/{subdir}/{suite}.py"
    left = rng.randrange(2, 12)
    right = rng.randrange(2, 12)
    correct = left * right
    broken = correct + rng.choice((-2, -1, 1, 2))
    base_files = {
        path: f"def {suite}():\n    assert {left} * {right} == {correct}\n"
    }
    candidate_files = {
        path: f"def {suite}():\n    assert {left} * {right} == {broken}\n",
        f"src/{mod}.py": _blob(rng),
    }
    criteria = [
        _criterion(
            f"baseline-suite-{suite}",
            "pytest_selector",
            {"selector": path},
            description=f"the baseline module {path} must still pass unchanged",
        )
    ]
    if rng.random() < 0.35:
        # Some cases also carry an invariant that the run preserved.  The
        # broken suite still decides the verdict, but the required-outcome
        # combination is no longer the same single-criterion contract.
        kept = f"src/{_module(rng)}.py"
        base_files[kept] = _blob(rng)
        candidate_files[kept] = f"{_blob(rng)}\n{rng.choice(_WORDS)} = 1\n"
        criteria.append(
            _criterion(
                f"kept-{rng.randrange(1000, 9999)}",
                "file_not_contains",
                {"path": kept, "pattern": "TODO"},
                description=f"{kept} must stay free of TODO markers",
            )
        )
    contract = _contract(
        f"fix the regression while the baseline suite in {path} keeps passing",
        tuple(criteria),
        allow_pre_satisfied=rng.random() < 0.25,
    )
    return CorpusCase(
        case_id=case_id,
        category="broken_invariant_test_suite",
        mechanism="the run broke an invariant that used to hold: the baseline suite no longer passes",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=TreeSpec(files=base_files),
        candidate=TreeSpec(files=candidate_files),
    )


def _broken_invariant_signature(rng: random.Random, case_id: str) -> CorpusCase:
    """The run silently changed a public signature."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    name = f"{rng.choice(_WORDS)}_{rng.randrange(100, 999)}"
    base = TreeSpec(files={path: f"def {name}(a, b):\n    return a + b\n"})
    candidate = TreeSpec(files={path: f"def {name}(a, b, c=0):\n    return a + b + c\n"})
    contract = _contract(
        f"the signature of {name} must not change",
        (_criterion("signature", "file_contains", {"path": path, "pattern": f"def {name}(a, b):"}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="broken_invariant_signature",
        mechanism="the run changed a public signature that the baseline established",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _broken_invariant_permission(rng: random.Random, case_id: str) -> CorpusCase:
    """The run made a required file unreadable."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    base = TreeSpec(files={path: _blob(rng)}, modes={path: 0o644})
    candidate = TreeSpec(files={path: _blob(rng)}, modes={path: 0o000})
    contract = _contract(
        f"{path} must stay readable",
        (_criterion("readable", "file_contains", {"path": path, "pattern": "="}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="broken_invariant_permission",
        mechanism="the run removed read permission from a file the contract inspects",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _output_mismatch_exit_zero(rng: random.Random, case_id: str) -> CorpusCase:
    """A command exits zero and produces the wrong answer."""
    mod = _module(rng)
    report = f"reports/{mod}.txt"
    base = TreeSpec(files={report: "STATUS: OK\n"})
    candidate = TreeSpec(files={report: "STATUS: FINE\n"})
    contract = _contract(
        f"{report} must say STATUS: OK",
        (
            _criterion(
                "report",
                "command_output_matches",
                {"command": ["cat", report], "pattern": "STATUS: OK"},
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="output_mismatch_exit_zero",
        mechanism="the command exited zero but its output does not match the required text",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _output_mismatch_case(rng: random.Random, case_id: str) -> CorpusCase:
    """The output matches except for its case."""
    mod = _module(rng)
    report = f"reports/{mod}.txt"
    base = TreeSpec(files={report: "STATUS: DONE\n"})
    candidate = TreeSpec(files={report: "status: done\n"})
    contract = _contract(
        f"{report} must say STATUS: DONE",
        (
            _criterion(
                "report",
                "command_output_matches",
                {"command": ["cat", report], "pattern": "STATUS: DONE"},
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="output_mismatch_case",
        mechanism="the output differs from the required text only in letter case",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _output_mismatch_regex_anchor(rng: random.Random, case_id: str) -> CorpusCase:
    """A regex that looks satisfied by eye is not satisfied by the matcher."""
    mod = _module(rng)
    report = f"reports/{mod}.txt"
    base = TreeSpec(files={report: "DONE\n"})
    candidate = TreeSpec(files={report: "DONE (partial)\n"})
    contract = _contract(
        f"{report} must be exactly DONE",
        (
            _criterion(
                "report",
                "command_output_matches",
                {"command": ["cat", report], "pattern": "^DONE$", "regex": True},
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="output_mismatch_regex_anchor",
        mechanism="an anchored regular expression does not match the produced output",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _invalid_regex_file(rng: random.Random, case_id: str) -> CorpusCase:
    """The contract's pattern is not a valid regular expression."""
    mod, other = _modules(rng, 2)
    path = f"src/{mod}.py"
    base = TreeSpec(files={path: _blob(rng)})
    candidate = TreeSpec(files={path: _blob(rng), f"src/{other}.py": _blob(rng)})
    contract = _contract(
        f"{path} must match the required pattern",
        (_criterion("pattern", "file_contains", {"path": path, "pattern": "[unterminated", "regex": True}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="invalid_regex_file",
        mechanism="the required pattern is not a valid regular expression, so the check cannot pass",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _invalid_regex_output(rng: random.Random, case_id: str) -> CorpusCase:
    """The output matcher was given an invalid regular expression."""
    mod = _module(rng)
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={"README.md": "# fixture\n", f"{mod}.txt": "output\n"})
    contract = _contract(
        f"the output of {mod}.txt must match the required pattern",
        (
            _criterion(
                "output",
                "command_output_matches",
                {"command": ["cat", f"{mod}.txt"], "pattern": "(unbalanced", "regex": True},
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="invalid_regex_output",
        mechanism="the output matcher was given an invalid regular expression",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _path_escape_relative(rng: random.Random, case_id: str) -> CorpusCase:
    """The contract points outside the workspace with a relative path."""
    mod = _module(rng)
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={"README.md": "# fixture\n", f"{mod}.py": _blob(rng)})
    contract = _contract(
        f"read ../{mod}.secret outside the workspace",
        (_criterion("escape", "file_exists", {"path": f"../{mod}.secret"}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="path_escape_relative",
        mechanism="the contract's path escapes the verification workspace",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _path_escape_symlink(rng: random.Random, case_id: str) -> CorpusCase:
    """A symlink inside the workspace points outside it."""
    mod = _module(rng)
    link = f"{mod}.link"
    base = TreeSpec(files={"README.md": "# fixture\n"}, symlinks={link: "/etc/hostname"})
    candidate = TreeSpec(files={"README.md": "# fixture\n"}, symlinks={link: "/etc/hostname"})
    contract = _contract(
        f"{link} must exist",
        (_criterion("link", "file_exists", {"path": link}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="path_escape_symlink",
        mechanism="a symlink inside the workspace resolves outside it",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _path_escape_absolute(rng: random.Random, case_id: str) -> CorpusCase:
    """The contract uses an absolute path.

    The escaped path and the (benign) workspace fixture both vary.  A fixed
    ``/etc/hostname`` against two fixed trees was one case repeated, not
    fifteen independent probes of the escape rule.
    """
    mod = _module(rng)
    # The path is absolute *and* case-specific.  A fixed ``/etc/hostname``
    # was the same requirement in several cases, so the contract hashes only
    # differed by an unrelated criterion id even after the tree varied.
    absolute = f"{rng.choice(('/tmp', '/var/tmp', '/opt', '/srv'))}/{case_id}.secret"
    fixture = f"src/{mod}.py"
    files = {"README.md": "# fixture\n", fixture: _blob(rng)}
    contract = _contract(
        f"read {absolute} and report its content",
        (
            _criterion(
                f"absolute-{mod}",
                "file_exists",
                {"path": absolute},
                description=f"the absolute path {absolute} must be inspected",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="path_escape_absolute",
        mechanism="the contract's absolute path resolves outside the workspace",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=TreeSpec(files=dict(files)),
        candidate=TreeSpec(files=dict(files)),
    )


def _command_timeout(rng: random.Random, case_id: str) -> CorpusCase:
    """The check command runs past its deadline.

    The command, its sleep duration and the deadline all vary, so the
    contract is about a different timing failure in each case rather than
    one shared script with a different unrelated file beside it.
    """
    mod = _module(rng)
    pause = rng.randrange(5, 30)
    timeout = rng.choice((0.2, 0.25, 0.3, 0.4))
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={"README.md": "# fixture\n", f"src/{mod}.py": _blob(rng)}
    )
    contract = _contract(
        f"the check on {mod} must finish within {timeout}s",
        (
            _criterion(
                f"slow-{mod}",
                "command_exits_zero",
                {
                    "command": [
                        "python3",
                        "-c",
                        f"import time; time.sleep({pause})",
                    ]
                },
                timeout_s=timeout,
                description=f"the check on {mod} must finish within {timeout}s",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="command_timeout",
        mechanism="the check command exceeded its deadline",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _command_timeout_after_output(rng: random.Random, case_id: str) -> CorpusCase:
    """The command produced the right output and then hung."""
    mod = _module(rng)
    marker = _marker(rng)
    pause = rng.randrange(5, 30)
    timeout = rng.choice((0.2, 0.25, 0.3, 0.4))
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={"README.md": "# fixture\n", f"src/{mod}.py": _blob(rng)}
    )
    contract = _contract(
        f"the check that prints {marker} must finish within {timeout}s",
        (
            _criterion(
                f"slow-after-output-{mod}",
                "command_exits_zero",
                {
                    "command": [
                        "python3",
                        "-c",
                        (
                            f"print({marker!r}, flush=True); "
                            f"import time; time.sleep({pause})"
                        ),
                    ]
                },
                timeout_s=timeout,
                description=(
                    f"the command that prints {marker} must finish within "
                    f"{timeout}s"
                ),
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="command_timeout_after_output",
        mechanism="the command printed the expected output and then timed out",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _pytest_zero_collected(rng: random.Random, case_id: str) -> CorpusCase:
    """The selected test file contains no tests at all."""
    stem = f"test_{rng.choice(_STEMS)}_{rng.randrange(1000, 9999)}"
    path = f"tests/{rng.choice(('unit', 'integration', 'legacy'))}/{stem}.py"
    files = {
        path: (
            f"VALUE = {rng.randrange(1, 9999)}\n\n"
            f"def helper_{rng.choice(_STEMS)}():\n"
            f"    return {rng.randrange(1, 9999)}\n"
        )
    }
    contract = _contract(
        f"the selected module {path} must contain a passing test",
        (
            _criterion(
                f"collected-{stem}",
                "pytest_selector",
                {"selector": path},
                description=f"{path} must collect at least one passing test",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="pytest_zero_collected",
        mechanism="the selected test module collects zero tests",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=TreeSpec(files=dict(files)),
        candidate=TreeSpec(files=dict(files)),
    )


def _pytest_missing_file(rng: random.Random, case_id: str) -> CorpusCase:
    """The contract points at a test module that does not exist."""
    path = f"tests/test_absent_{rng.randrange(100, 999)}.py"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={"README.md": "# fixture\n", f"{_module(rng)}.py": _blob(rng)})
    contract = _contract(
        f"the required test module {path} must exist and pass",
        (_criterion("missing", "pytest_selector", {"selector": path}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="pytest_missing_file",
        mechanism="the contract's test selector points at a module that does not exist",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _advisory_llm_review(rng: random.Random, case_id: str) -> CorpusCase:
    """The only required outcome is an advisory model review.

    The review subject, the concrete question and the combination of
    advisory outcomes vary.  A contract made only of advisory checks cannot
    establish a deterministic outcome, but each case asks a different
    advisory question about a different tree.
    """
    mod, other = _modules(rng, 2)
    subject = f"src/{mod}.py"
    topic = rng.choice(
        ("API compatibility", "error handling", "accessibility", "naming")
    )
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={
            "README.md": "# fixture\n",
            subject: _blob(rng),
            f"notes/{other}.md": "# reviewer notes\n",
        }
    )
    criteria = [
        _criterion(
            f"review-{mod}",
            "llm_review",
            {},
            description=f"a model must review {subject} for {topic}",
        )
    ]
    if rng.random() < 0.5:
        criteria.append(
            _criterion(
                f"review-{other}",
                "llm_review",
                {},
                description=f"a model must also review the notes in {other}",
            )
        )
    contract = _contract(f"obtain a model review of {subject}", tuple(criteria))
    return CorpusCase(
        case_id=case_id,
        category="advisory_llm_review",
        mechanism="the only required outcome is advisory, so nothing deterministic was established",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _advisory_manual(rng: random.Random, case_id: str) -> CorpusCase:
    """The only required outcome needs a human.

    The review subject and question vary per case, and half of the contracts
    pair the manual check with a model review so the required-outcome
    combination is not identical across the family.  Both kinds are
    advisory, so the correct verdict remains inconclusive.
    """
    mod, other = _modules(rng, 2)
    subject = f"src/{mod}.py"
    topic = rng.choice(
        ("visual layout", "operator safety", "deployment readiness", "licensing")
    )
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={
            "README.md": "# fixture\n",
            subject: _blob(rng),
            f"docs/{other}.md": "# review context\n",
        }
    )
    criteria = [
        _criterion(
            f"manual-{mod}",
            "manual",
            {},
            description=f"a human must review {subject} for {topic}",
        )
    ]
    if rng.random() < 0.5:
        criteria.append(
            _criterion(
                f"manual-model-{other}",
                "llm_review",
                {},
                description=f"a model must summarise the review context in {other}",
            )
        )
    contract = _contract(f"obtain human sign-off for {subject}", tuple(criteria))
    return CorpusCase(
        case_id=case_id,
        category="advisory_manual",
        mechanism="the only required outcome is a manual check, so nothing deterministic was established",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _empty_contract(rng: random.Random, case_id: str) -> CorpusCase:
    """The contract states no required outcome at all."""
    mod, other = _modules(rng, 2)
    target = f"src/{mod}.py"
    objective = rng.choice(
        (
            f"tidy up {target}",
            f"make {target} better",
            f"look into {target} and do what seems right",
            f"improve the module at {target} without saying how",
            f"address the concerns about {target}",
        )
    )
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={
            "README.md": "# fixture\n",
            target: _blob(rng),
            f"notes/{other}.md": "# work log\n",
        }
    )
    contract = _contract(
        objective, (), allow_pre_satisfied=rng.random() < 0.5
    )
    return CorpusCase(
        case_id=case_id,
        category="empty_contract",
        mechanism="the contract requires nothing, so no independent check was established",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.INCONCLUSIVE,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _wrong_filename_case(rng: random.Random, case_id: str) -> CorpusCase:
    """The run created a file whose name differs only in case.

    The stem includes a random number as well as a random word.  With only
    the eight word stems, fifteen cases produced eight contracts: several
    cases were the very same requirement.
    """
    word = rng.choice(_WORDS).capitalize()
    number = rng.randrange(1000, 9999)
    wanted = f"{word}_{number}.py"
    created = f"{word.lower()}_{number}.py"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={created: _blob(rng)})
    contract = _contract(
        f"add {wanted}",
        (
            _criterion(
                f"named-{word.lower()}-{number}",
                "file_exists",
                {"path": wanted},
                description=f"the requested file {wanted} must exist",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="wrong_filename_case",
        mechanism="the run created a file whose name differs from the contract only in case",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _unicode_normalisation_mismatch(rng: random.Random, case_id: str) -> CorpusCase:
    """The contract's path and the file on disk use different normalisations."""
    stem = _unicode_stem(rng)
    composed = unicodedata.normalize("NFC", f"{stem}.txt")
    decomposed = unicodedata.normalize("NFD", f"{stem}.txt")
    if composed == decomposed:  # pragma: no cover - guarded by _unicode_stem
        decomposed = composed + "\u0301"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={decomposed: _blob(rng)})
    contract = _contract(
        f"add the report file {composed}",
        (
            _criterion(
                f"unicode-{rng.randrange(1000, 9999)}",
                "file_exists",
                {"path": composed},
                description=f"the composed path {composed!r} must exist",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="unicode_normalisation_mismatch",
        mechanism="the contract's path and the file on disk differ in unicode normalisation",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _mutually_exclusive_requirements(rng: random.Random, case_id: str) -> CorpusCase:
    """Two required outcomes that cannot both hold."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    # The two markers must be distinct, otherwise the "mutually exclusive"
    # outcome would be satisfiable after all.
    first_word, second_word = rng.sample(_WORDS, 2)
    first = f"MODE_{first_word.upper()}"
    second = f"MODE_{second_word.upper()}"
    base = TreeSpec(files={path: _blob(rng)})
    candidate = TreeSpec(files={path: f"{first} = 1\n"})
    contract = _contract(
        f"{path} must satisfy both modes",
        (
            _criterion("first", "file_contains", {"path": path, "pattern": first}),
            _criterion("second", "file_contains", {"path": path, "pattern": second}),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="mutually_exclusive_requirements",
        mechanism="two required outcomes cannot both hold, and only one was met",
        expectation=Expectation.REJECT,
        expected_outcome=VerificationOutcome.REJECTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


#: The adversarial families.  Every entry is a distinct failure mechanism.
ADVERSARIAL_FAMILIES: dict[str, Callable[[random.Random, str], CorpusCase]] = {
    "pre_satisfied_untouched": _pre_satisfied_untouched,
    "pre_satisfied_no_op_tree": _pre_satisfied_no_op_tree,
    "no_op_diff_nonempty": _no_op_diff_nonempty,
    "empty_patch_reverted": _empty_patch_reverted,
    "permitted_noop_but_unmet_outcome": _permitted_noop_but_unmet,
    "union_lost_side": _union_lost_side,
    "union_broken_syntax": _union_broken_syntax,
    "union_marker_wrong_file": _union_marker_wrong_file,
    "union_drops_behaviour": _union_drops_behaviour,
    "weak_test_replaces_suite": _weak_test_replaces_suite,
    "weak_test_feature_missing": _weak_test_feature_missing,
    "weak_test_asserts_nothing": _weak_test_asserts_nothing,
    "weak_test_shadowed_by_stale_copy": _weak_test_shadowed_by_stale_copy,
    "stale_base_revert": _stale_base_revert,
    "stale_artifact_copied": _stale_artifact_copied,
    "stale_test_snapshot": _stale_test_snapshot,
    "stale_evidence_claims_success": _stale_evidence_claims_success,
    "broken_invariant_marker": _broken_invariant_marker,
    "broken_invariant_test_suite": _broken_invariant_test_suite,
    "broken_invariant_signature": _broken_invariant_signature,
    "broken_invariant_permission": _broken_invariant_permission,
    "output_mismatch_exit_zero": _output_mismatch_exit_zero,
    "output_mismatch_case": _output_mismatch_case,
    "output_mismatch_regex_anchor": _output_mismatch_regex_anchor,
    "invalid_regex_file": _invalid_regex_file,
    "invalid_regex_output": _invalid_regex_output,
    "path_escape_relative": _path_escape_relative,
    "path_escape_symlink": _path_escape_symlink,
    "path_escape_absolute": _path_escape_absolute,
    "command_timeout": _command_timeout,
    "command_timeout_after_output": _command_timeout_after_output,
    "pytest_zero_collected": _pytest_zero_collected,
    "pytest_missing_file": _pytest_missing_file,
    "advisory_llm_review": _advisory_llm_review,
    "advisory_manual": _advisory_manual,
    "empty_contract": _empty_contract,
    "wrong_filename_case": _wrong_filename_case,
    "unicode_normalisation_mismatch": _unicode_normalisation_mismatch,
    "mutually_exclusive_requirements": _mutually_exclusive_requirements,
}


# ---------------------------------------------------------------------------
# The known-correct families
# ---------------------------------------------------------------------------
#
# These are the cases that a correct verifier must accept.  They are the
# other half of the criterion: a verifier that rejects everything would
# score a perfect false-accept rate and fail every one of these.


def _established_new_module(rng: random.Random, case_id: str) -> CorpusCase:
    """The run created the module the contract asked for."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={"README.md": "# fixture\n", path: _blob(rng)})
    contract = _contract(
        f"add {path}",
        (_criterion("module", "file_exists", {"path": path}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_new_module",
        mechanism="the run established a newly requested module",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _established_new_symbol(rng: random.Random, case_id: str) -> CorpusCase:
    """The run added the function the contract asked for."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    symbol = f"def {rng.choice(_WORDS)}_{rng.randrange(100, 999)}("
    base = TreeSpec(files={path: _blob(rng)})
    candidate = TreeSpec(files={path: f"{_blob(rng)}\n{symbol}    return 1\n"})
    contract = _contract(
        f"{path} must define the new symbol",
        (_criterion("symbol", "file_contains", {"path": path, "pattern": symbol}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_new_symbol",
        mechanism="the run established a newly requested symbol in an existing file",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _preserved_invariant_touched(rng: random.Random, case_id: str) -> CorpusCase:
    """The run edited the file and kept the invariant that file carried."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    base = TreeSpec(files={path: _blob(rng)})
    candidate = TreeSpec(files={path: f"{_blob(rng)}\n{rng.choice(_WORDS)} = 1\n"})
    contract = _contract(
        f"{path} must stay free of TODO markers",
        (_criterion("no-todo", "file_not_contains", {"path": path, "pattern": "TODO"}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="preserved_invariant_touched",
        mechanism="the run preserved a baseline invariant in a file it edited",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _preserved_existing_symbol(rng: random.Random, case_id: str) -> CorpusCase:
    """The run kept an existing public symbol while extending the file."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    symbol = f"def {rng.choice(_WORDS)}_{rng.randrange(100, 999)}("
    base = TreeSpec(files={path: f"{_blob(rng)}{symbol}    return 1\n"})
    candidate = TreeSpec(
        files={path: f"{_blob(rng)}{symbol}    return 1\n\n{rng.choice(_WORDS)} = 2\n"}
    )
    contract = _contract(
        f"{path} must keep the existing symbol",
        (_criterion("symbol", "file_contains", {"path": path, "pattern": symbol}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="preserved_existing_symbol",
        mechanism="the run preserved an existing symbol while extending the file",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _legit_noop_permitted(rng: random.Random, case_id: str) -> CorpusCase:
    """A genuinely already-satisfied request, with the no-op explicitly allowed."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    files = {path: _blob(rng), "README.md": "# fixture\n"}
    contract = _contract(
        f"confirm {path} exists",
        (_criterion("exists", "file_exists", {"path": path}),),
        allow_pre_satisfied=True,
    )
    return CorpusCase(
        case_id=case_id,
        category="legit_noop_permitted",
        mechanism="a legitimate no-op: the request was already satisfied and the contract permits it",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=TreeSpec(files=files),
        candidate=TreeSpec(files=dict(files)),
    )


def _legit_noop_permitted_with_change(rng: random.Random, case_id: str) -> CorpusCase:
    """A permitted no-op alongside an unrelated change."""
    mod, other = _modules(rng, 2)
    path = f"src/{mod}.py"
    files = {path: _blob(rng), "README.md": "# fixture\n"}
    candidate = TreeSpec(files={**files, f"src/{other}.py": _blob(rng)})
    contract = _contract(
        f"confirm {path} exists",
        (_criterion("exists", "file_exists", {"path": path}),),
        allow_pre_satisfied=True,
    )
    return CorpusCase(
        case_id=case_id,
        category="legit_noop_permitted_with_change",
        mechanism="a legitimate no-op on one outcome while the run changed something else",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=TreeSpec(files=files),
        candidate=candidate,
    )


def _established_by_pytest(rng: random.Random, case_id: str) -> CorpusCase:
    """The run added a test module that passes."""
    name = f"tests/test_{rng.choice(_STEMS)}_{rng.randrange(100, 999)}.py"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={
            "README.md": "# fixture\n",
            name: "def test_new_behaviour():\n    assert 2 + 2 == 4\n",
        }
    )
    contract = _contract(
        f"the new regression test {name} must pass",
        (_criterion("new-test", "pytest_selector", {"selector": name}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_by_pytest",
        mechanism="the run established a newly requested outcome that is checked by pytest",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _preserved_test_suite(rng: random.Random, case_id: str) -> CorpusCase:
    """The baseline suite still passes after a change elsewhere."""
    mod = _module(rng)
    suite = f"test_suite_{rng.choice(_STEMS)}_{rng.randrange(1000, 9999)}"
    path = f"tests/{rng.choice(('unit', 'integration', 'regression'))}/{suite}.py"
    left = rng.randrange(2, 12)
    right = rng.randrange(2, 12)
    body = f"def {suite}():\n    assert {left} * {right} == {left * right}\n"
    base = TreeSpec(files={path: body})
    candidate = TreeSpec(files={path: body, f"src/{mod}.py": _blob(rng)})
    contract = _contract(
        f"the regression suite in {path} must still pass",
        (
            _criterion(
                f"suite-{suite}",
                "pytest_selector",
                {"selector": path},
                description=f"the baseline suite {path} must still pass",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="preserved_test_suite",
        mechanism="the run preserved the baseline invariant that the test suite still passes",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _established_output_match(rng: random.Random, case_id: str) -> CorpusCase:
    """The run produced the report the contract asked for."""
    mod = _module(rng)
    report = f"reports/{mod}.txt"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={"README.md": "# fixture\n", report: "STATUS: DONE\n"})
    contract = _contract(
        f"{report} must say STATUS: DONE",
        (
            _criterion(
                "report",
                "command_output_matches",
                {"command": ["cat", report], "pattern": "STATUS: DONE"},
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_output_match",
        mechanism="the run established a newly requested output match",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _established_command_exit_zero(rng: random.Random, case_id: str) -> CorpusCase:
    """The run added a script that runs successfully."""
    mod = _module(rng)
    script = f"{mod}.py"
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(
        files={"README.md": "# fixture\n", script: "def main():\n    return 0\n\n\nmain()\n"}
    )
    contract = _contract(
        f"{script} must run successfully",
        (_criterion("runs", "command_exits_zero", {"command": ["python3", script]}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_command_exit_zero",
        mechanism="the run established a newly requested command that exits zero",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _established_unicode_path(rng: random.Random, case_id: str) -> CorpusCase:
    """The run created a file whose name is not ASCII."""
    name = unicodedata.normalize("NFC", f"{_unicode_stem(rng)}.txt")
    base = TreeSpec(files={"README.md": "# fixture\n"})
    candidate = TreeSpec(files={"README.md": "# fixture\n", name: _blob(rng)})
    contract = _contract(
        f"add the unicode-named notes file {name}",
        (
            _criterion(
                f"unicode-{rng.randrange(1000, 9999)}",
                "file_exists",
                {"path": name},
                description=f"the requested unicode path {name!r} must exist",
            ),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_unicode_path",
        mechanism="the run established a newly requested file with a unicode name",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _established_and_preserved(rng: random.Random, case_id: str) -> CorpusCase:
    """One outcome established, one preserved, in the same contract."""
    mod, other = _modules(rng, 2)
    new_path = f"src/{mod}.py"
    touched = f"src/{other}.py"
    base = TreeSpec(files={touched: _blob(rng)})
    candidate = TreeSpec(
        files={touched: f"{_blob(rng)}\n{rng.choice(_WORDS)} = 1\n", new_path: _blob(rng)}
    )
    contract = _contract(
        f"add {new_path} while keeping the invariant in {touched}",
        (
            _criterion("new-module", "file_exists", {"path": new_path}),
            _criterion("no-todo", "file_not_contains", {"path": touched, "pattern": "TODO"}),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_and_preserved",
        mechanism="one outcome established and one preserved in the same contract",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _established_removal(rng: random.Random, case_id: str) -> CorpusCase:
    """The run removed the offending content."""
    mod = _module(rng)
    path = f"src/{mod}.py"
    forbidden = f"import {rng.choice(_WORDS)}"
    base = TreeSpec(files={path: f"{forbidden}\n{_blob(rng)}"})
    candidate = TreeSpec(files={path: _blob(rng)})
    contract = _contract(
        f"{path} must not import {forbidden.split()[1]}",
        (_criterion("no-import", "file_not_contains", {"path": path, "pattern": forbidden}),),
    )
    return CorpusCase(
        case_id=case_id,
        category="established_removal",
        mechanism="the run established a newly requested absence by removing the offending content",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


def _preserved_multiple_invariants(rng: random.Random, case_id: str) -> CorpusCase:
    """Several baseline invariants, all preserved."""
    first, second = _modules(rng, 2)
    first_path = f"src/{first}.py"
    second_path = f"src/{second}.py"
    base = TreeSpec(files={first_path: _blob(rng), second_path: _blob(rng)})
    candidate = TreeSpec(
        files={
            first_path: f"{_blob(rng)}\n{rng.choice(_WORDS)} = 1\n",
            second_path: f"{_blob(rng)}\n{rng.choice(_WORDS)} = 2\n",
        }
    )
    contract = _contract(
        f"both {first_path} and {second_path} invariants must hold",
        (
            _criterion("first", "file_not_contains", {"path": first_path, "pattern": "TODO"}),
            _criterion("second", "file_not_contains", {"path": second_path, "pattern": "FIXME"}),
        ),
    )
    return CorpusCase(
        case_id=case_id,
        category="preserved_multiple_invariants",
        mechanism="the run preserved several baseline invariants at once",
        expectation=Expectation.ACCEPT,
        expected_outcome=VerificationOutcome.ACCEPTED,
        contract=contract,
        base=base,
        candidate=candidate,
    )


#: The known-correct families: every one of these must be accepted.
KNOWN_CORRECT_FAMILIES: dict[str, Callable[[random.Random, str], CorpusCase]] = {
    "established_new_module": _established_new_module,
    "established_new_symbol": _established_new_symbol,
    "preserved_invariant_touched": _preserved_invariant_touched,
    "preserved_existing_symbol": _preserved_existing_symbol,
    "legit_noop_permitted": _legit_noop_permitted,
    "legit_noop_permitted_with_change": _legit_noop_permitted_with_change,
    "established_by_pytest": _established_by_pytest,
    "preserved_test_suite": _preserved_test_suite,
    "established_output_match": _established_output_match,
    "established_command_exit_zero": _established_command_exit_zero,
    "established_unicode_path": _established_unicode_path,
    "established_and_preserved": _established_and_preserved,
    "established_removal": _established_removal,
    "preserved_multiple_invariants": _preserved_multiple_invariants,
}


#: Mechanisms the current verifier does not catch.  They are listed rather
#: than silently dropped, because a corpus that hides its own blind spots is
#: not evidence.  See the module docstring of ``verification/checks.py``:
#: a pytest selector is judged on its exit code, and pytest exits zero when
#: the selected test is *skipped*.
EXCLUDED_CASES: tuple[dict[str, str], ...] = (
    {
        "category": "skipped_required_test",
        "mechanism": (
            "the required test is skipped; pytest exits zero, so an exit-code "
            "based selector treats the requirement as met"
        ),
        "status": (
            "covered by the dedicated adversarial tests in "
            "tests/evaluation/test_adversarial_contracts.py, not by this "
            "generated corpus"
        ),
        "owner": "the deterministic check executor, not the contract gate",
    },
)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _balanced_schedule(families: Sequence[str], count: int, rng: random.Random) -> list[str]:
    """Assign ``count`` cases to families, balanced and then shuffled.

    The balance guarantees that every mechanism is exercised the same number
    of times, so the corpus cannot accidentally be dominated by one easy
    family.  The shuffle means the family of a case is not determined by its
    position, which is what lets the case-level analysis treat the cases as
    draws from one distribution rather than as blocks.
    """
    if not families:
        raise ValueError("there must be at least one family")
    schedule: list[str] = []
    for index in range(count):
        schedule.append(families[index % len(families)])
    rng.shuffle(schedule)
    return schedule


def _build_cases(
    families: dict[str, Callable[[random.Random, str], CorpusCase]],
    schedule: Sequence[str],
    seed: int,
    prefix: str,
) -> tuple[CorpusCase, ...]:
    """Materialise the cases for one half of the corpus."""
    cases: list[CorpusCase] = []
    for index, family in enumerate(schedule):
        case_id = f"{prefix}-{family}-{index:04d}"
        # Every case gets its own random stream.  Two cases never share a
        # draw, which is what makes them independent observations.
        rng = random.Random(f"{seed}:{case_id}")
        cases.append(families[family](rng, case_id))
    return tuple(cases)


def generate_corpus(
    seed: int = DEFAULT_SEED,
    *,
    adversarial_count: int = DEFAULT_ADVERSARIAL_CASES,
    known_correct_count: int = DEFAULT_KNOWN_CORRECT_CASES,
) -> Corpus:
    """Generate the whole corpus deterministically from ``seed``."""
    rng = random.Random(f"{seed}:schedule")
    adversarial_families = sorted(ADVERSARIAL_FAMILIES)
    known_families = sorted(KNOWN_CORRECT_FAMILIES)
    adversarial_schedule = _balanced_schedule(adversarial_families, adversarial_count, rng)
    known_schedule = _balanced_schedule(known_families, known_correct_count, rng)
    return Corpus(
        seed=seed,
        adversarial=_build_cases(ADVERSARIAL_FAMILIES, adversarial_schedule, seed, "adv"),
        known_correct=_build_cases(KNOWN_CORRECT_FAMILIES, known_schedule, seed, "ok"),
    )


def stratified_sample(corpus: Corpus, per_family: int) -> Corpus:
    """Take the first ``per_family`` cases of every family.

    The default test run uses this: every mechanism is still exercised, so
    the sample cannot pass by accident, but the suite stays fast.
    """
    def take(cases: Sequence[CorpusCase]) -> tuple[CorpusCase, ...]:
        seen: dict[str, int] = {}
        chosen: list[CorpusCase] = []
        for case in cases:
            count = seen.get(case.category, 0)
            if count < per_family:
                seen[case.category] = count + 1
                chosen.append(case)
        return tuple(chosen)

    return Corpus(
        seed=corpus.seed,
        adversarial=take(corpus.adversarial),
        known_correct=take(corpus.known_correct),
    )


# ---------------------------------------------------------------------------
# Running the corpus
# ---------------------------------------------------------------------------


def _materialise(spec: TreeSpec, root: Path) -> None:
    """Write one tree to disk."""
    root.mkdir(parents=True, exist_ok=True)
    for name, content in spec.files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for name, destination in spec.symlinks.items():
        link = root / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(destination)
    for name, mode in spec.modes.items():
        os.chmod(root / name, mode)


async def run_case(
    case: CorpusCase,
    gate: ContractGate,
    workspace: Path,
) -> CaseResult:
    """Run one case through the real contract gate."""
    case_root = workspace / case.case_id
    base = case_root / "base"
    candidate = case_root / "candidate"
    _materialise(case.base, base)
    _materialise(case.candidate, candidate)
    started = time.monotonic()
    verdict = await gate.verify(case.contract, base_tree=base, candidate_tree=candidate)
    duration = time.monotonic() - started
    accepted = verdict.outcome is VerificationOutcome.ACCEPTED
    should_accept = case.expectation is Expectation.ACCEPT
    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        mechanism=case.mechanism,
        expectation=case.expectation,
        expected_outcome=case.expected_outcome,
        actual_outcome=verdict.outcome,
        false_accept=accepted and not should_accept,
        false_reject=(not accepted) and should_accept,
        outcome_mismatch=verdict.outcome is not case.expected_outcome,
        detail=verdict.detail,
        states={v.criterion_id: v.state.value for v in verdict.verdicts},
        duration_s=duration,
    )


async def run_corpus(
    corpus: Corpus,
    *,
    workspace: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[CaseResult]:
    """Run every case in the corpus through the real contract gate."""
    root = Path(tempfile.mkdtemp(prefix="agent-squad-corpus-")) if workspace is None else workspace
    root.mkdir(parents=True, exist_ok=True)
    config = RunConfig(run_id=f"corpus-{corpus.seed}", repo_path=str(root))
    gate = ContractGate(config)
    results: list[CaseResult] = []
    total = len(corpus.cases)
    try:
        for index, case in enumerate(corpus.cases, start=1):
            results.append(await run_case(case, gate, root))
            if progress is not None:
                progress(index, total)
    finally:
        if workspace is None:
            shutil.rmtree(root, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _cluster_summary(results: Sequence[CaseResult], key: str) -> dict[str, Any]:
    """The clustered analysis of one half of the corpus.

    The clusters are the mechanism families.  The reported design effect is
    the Kish one, ``1 + (m - 1) * rho``, with ``rho`` the observed
    intra-cluster correlation of the false-accept (or acceptance)
    indicator.  The effective sample size is ``n / DEFF``, and the clustered
    limit is the exact Clopper-Pearson limit computed at that size.
    """
    groups: dict[str, list[CaseResult]] = {}
    for result in results:
        groups.setdefault(result.category, []).append(result)
    sizes = [len(members) for members in groups.values()]
    proportions = [
        sum(1 for r in members if getattr(r, key)) / len(members)
        for members in groups.values()
    ]
    total = sum(sizes)
    rho = intra_cluster_correlation(proportions, sizes)
    average_size = total / len(groups) if groups else 1.0
    design_effect = kish_design_effect(average_size, rho)
    effective = effective_sample_size(total, design_effect)
    return {
        "clusters": len(groups),
        "cluster_sizes": {name: len(members) for name, members in sorted(groups.items())},
        "average_cluster_size": round(average_size, 6),
        "intra_cluster_correlation": round(rho, 8),
        "design_effect": round(design_effect, 8),
        "effective_sample_size": round(effective, 6),
        "effective_independent_clusters": round(len(groups) / design_effect, 6),
    }


def _family_level_bounds(results: Sequence[CaseResult], key: str) -> dict[str, Any]:
    """The pessimistic family-level question.

    "What if one whole family is broken in a way this corpus did not happen
    to catch?"  A family with ``m`` cases and no observed events still has
    an upper limit on its own rate; the family's contribution to the
    overall rate is its share of the corpus times that limit.  The
    Bonferroni-adjusted variant charges the family-selection correction for
    looking at all of the families at once.
    """
    groups: dict[str, list[CaseResult]] = {}
    for result in results:
        groups.setdefault(result.category, []).append(result)
    total = sum(len(members) for members in groups.values())
    if not groups:
        return {}
    alpha = 1.0 - CONFIDENCE
    per_family: dict[str, dict[str, float]] = {}
    for name, members in sorted(groups.items()):
        size = len(members)
        events = sum(1 for r in members if getattr(r, key))
        per_family[name] = {
            "cases": size,
            "events": events,
            "upper_limit_95": round(clopper_pearson_upper(events, size), 10),
            "upper_limit_family_corrected": round(
                clopper_pearson_upper(events, size, confidence=1.0 - alpha / len(groups)),
                10,
            ),
        }
    worst_unadjusted = max(
        (stats["cases"] / total) * stats["upper_limit_95"] for stats in per_family.values()
    )
    worst_corrected = max(
        (stats["cases"] / total) * stats["upper_limit_family_corrected"]
        for stats in per_family.values()
    )
    return {
        "per_family": per_family,
        "worst_single_family_contribution_95": round(worst_unadjusted, 10),
        "worst_single_family_contribution_family_corrected": round(worst_corrected, 10),
        "cluster_level_rate_upper_limit_95": round(
            clopper_pearson_upper(0, len(groups)), 10
        ),
        "note": (
            "the worst-family figures bound the overall rate under the "
            "pessimistic assumption that one whole mechanism family is "
            "broken at the level its own sample size cannot rule out"
        ),
    }


def analyse_adversarial(results: Sequence[CaseResult]) -> dict[str, Any]:
    """The false-accept statistics for the adversarial half of the corpus."""
    total = len(results)
    false_accepts = sum(1 for r in results if r.false_accept)
    limits = binomial_limit(false_accepts, total, confidence=CONFIDENCE)
    clusters = _cluster_summary(results, "false_accept")
    clustered_limit = clopper_pearson_upper(
        false_accepts, max(1, round(clusters["effective_sample_size"]))
    )
    return {
        "cases": total,
        "false_accepts": false_accepts,
        "false_accept_rate": false_accepts / total if total else 0.0,
        "upper_confidence_limit_95": round(limits.upper, 12),
        "confidence": CONFIDENCE,
        "method": limits.method,
        "threshold": FALSE_ACCEPT_THRESHOLD,
        "meets_threshold": limits.upper < FALSE_ACCEPT_THRESHOLD,
        "clustered_upper_confidence_limit_95": round(clustered_limit, 12),
        "clustering": clusters,
        "family_level": _family_level_bounds(results, "false_accept"),
    }


def analyse_known_correct(results: Sequence[CaseResult]) -> dict[str, Any]:
    """The acceptance statistics for the known-correct half of the corpus."""
    total = len(results)
    accepted = sum(1 for r in results if not r.false_reject)
    limits = binomial_limit(accepted, total, confidence=CONFIDENCE)
    clusters = _cluster_summary(results, "false_reject")
    clustered_limit = clopper_pearson_lower(
        accepted, max(1, round(clusters["effective_sample_size"]))
    )
    return {
        "cases": total,
        "accepted": accepted,
        "false_rejects": total - accepted,
        "acceptance_rate": accepted / total if total else 0.0,
        "lower_confidence_limit_95": round(limits.lower, 12),
        "confidence": CONFIDENCE,
        "method": limits.method,
        "threshold": ACCEPTANCE_THRESHOLD,
        "meets_threshold": limits.lower >= ACCEPTANCE_THRESHOLD,
        "clustered_lower_confidence_limit_95": round(clustered_limit, 12),
        "clustering": clusters,
        "family_level": _family_level_bounds(results, "false_reject"),
    }


def category_breakdown(results: Sequence[CaseResult]) -> dict[str, Any]:
    """Per-category counts for the report."""
    breakdown: dict[str, dict[str, Any]] = {}
    for result in results:
        entry = breakdown.setdefault(
            result.category,
            {
                "cases": 0,
                "false_accepts": 0,
                "false_rejects": 0,
                "accepted": 0,
                "expected_outcome": result.expected_outcome.value,
                "mechanisms": set(),
            },
        )
        entry["cases"] += 1
        entry["false_accepts"] += int(result.false_accept)
        entry["false_rejects"] += int(result.false_reject)
        entry["accepted"] += int(result.actual_outcome is VerificationOutcome.ACCEPTED)
        entry["mechanisms"].add(result.mechanism)
    for entry in breakdown.values():
        entry["mechanisms"] = sorted(entry["mechanisms"])
    return breakdown


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

INDEPENDENCE_ARGUMENT = (
    "Every case draws its randomness from its own PRNG stream, seeded from "
    "(seed, case_id), so no two cases share a random draw; the mechanism "
    "family is a randomised covariate rather than a block; every case is "
    "materialised in its own workspace, so no case can influence another; "
    "and the only shared component is the verifier itself, which is a "
    "systematic effect on the rate rather than a sampling covariance.  The "
    "case-level Clopper-Pearson limit is therefore the valid bound for the "
    "corpus.  The clustered and family-level analyses are reported next to "
    "it so that a reviewer can see what the claim would look like under the "
    "pessimistic assumption that a whole family is broken."
)


class CorpusReport(BaseModel):
    """The machine-readable result of a corpus run."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "agent-squad/corpus-report/1"
    generated_at: str
    seed: int
    verifier_version: str
    duration_s: float
    summary: dict[str, Any]
    adversarial: dict[str, Any]
    known_correct: dict[str, Any]
    categories: dict[str, Any]
    cases: list[dict[str, Any]]
    excluded_cases: list[dict[str, str]]
    design: dict[str, Any]
    notes: list[str]

    def to_json(self) -> str:
        """The report as pretty-printed JSON."""
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)


def build_report(
    corpus: Corpus,
    results: Sequence[CaseResult],
    *,
    duration_s: float,
) -> CorpusReport:
    """Assemble the report from a completed run."""
    adversarial_results = [r for r in results if r.expectation is Expectation.REJECT]
    known_results = [r for r in results if r.expectation is Expectation.ACCEPT]
    adversarial = analyse_adversarial(adversarial_results)
    known_correct = analyse_known_correct(known_results)
    false_accepts = adversarial["false_accepts"]
    false_rejects = known_correct["false_rejects"]
    design = {
        "independence_argument": INDEPENDENCE_ARGUMENT,
        "mechanism_families": {
            "adversarial": len(ADVERSARIAL_FAMILIES),
            "known_correct": len(KNOWN_CORRECT_FAMILIES),
        },
        "randomisation_unit": "one independent PRNG stream per case",
        "clustering_unit": "the mechanism family",
        "primary_limit": "case-level exact Clopper-Pearson one-sided limit",
        "clustered_limit": (
            "exact Clopper-Pearson limit computed at the design-effect "
            "adjusted effective sample size"
        ),
        "family_level_limit": (
            "the pessimistic 'one whole family is broken' bound, reported so "
            "that the residual risk of a mechanism-shaped blind spot is visible"
        ),
        "effective_sample_justification": (
            "The clustering unit is the mechanism family and every case has its "
            "own PRNG stream, workspace and contract hash.  The report gives the "
            "case-level exact limit, the Kish design-effect adjusted effective "
            "sample size and its limit, every family's own exact limit, and the "
            "worst single-family contribution.  A reviewer can therefore reject "
            "the independence claim and still read the conservative bound."
        ),
    }
    notes = [
        "A case counts as a false accept when the gate accepts a case whose "
        "request was not satisfied; it counts as a false reject when the gate "
        "does not accept a case whose request was satisfied.",
        "The corpus exercises the contract gate.  It cannot certify the rest "
        "of the system, so the exact verdict of every case is recorded next "
        "to the coarse expectation.",
        "Mechanisms the current verifier does not catch are listed under "
        "'excluded_cases' rather than silently dropped.",
    ]
    return CorpusReport(
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        seed=corpus.seed,
        verifier_version=VERIFIER_VERSION,
        duration_s=round(duration_s, 3),
        summary={
            "false_accepts": false_accepts,
            "false_rejects": false_rejects,
            "outcome_mismatches": sum(1 for r in results if r.outcome_mismatch),
            "adversarial_cases": adversarial["cases"],
            "known_correct_cases": known_correct["cases"],
            "upper_confidence_limit_95": adversarial["upper_confidence_limit_95"],
            "lower_confidence_limit_95": known_correct["lower_confidence_limit_95"],
            "upper_limit_method": adversarial["method"],
            "lower_limit_method": known_correct["method"],
            "adversarial_effective_sample_size": adversarial["clustering"][
                "effective_sample_size"
            ],
            "known_correct_effective_sample_size": known_correct["clustering"][
                "effective_sample_size"
            ],
            "meets_false_accept_threshold": adversarial["meets_threshold"],
            "meets_acceptance_threshold": known_correct["meets_threshold"],
        },
        adversarial=adversarial,
        known_correct=known_correct,
        categories={
            "adversarial": category_breakdown(adversarial_results),
            "known_correct": category_breakdown(known_results),
        },
        cases=[result.to_dict() for result in results],
        excluded_cases=[dict(entry) for entry in EXCLUDED_CASES],
        design=design,
        notes=notes,
    )


def write_report(report: CorpusReport, path: Path) -> Path:
    """Write the report to ``path``, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json() + "\n", encoding="utf-8")
    return path


def default_report_path(seed: int = DEFAULT_SEED) -> Path:
    """Where the published report for ``seed`` lives."""
    return Path("benchmarks") / "results" / f"corpus-{seed}.json"


# ---------------------------------------------------------------------------
# Command line entry point
# ---------------------------------------------------------------------------


async def _run_and_report(
    seed: int,
    adversarial_count: int,
    known_correct_count: int,
    output: Path | None,
) -> CorpusReport:
    corpus = generate_corpus(
        seed,
        adversarial_count=adversarial_count,
        known_correct_count=known_correct_count,
    )
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        if done % 50 == 0 or done == total:
            print(f"  {done}/{total} cases", flush=True)

    results = await run_corpus(corpus, progress=progress)
    report = build_report(corpus, results, duration_s=time.monotonic() - started)
    if output is not None:
        write_report(report, output)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Generate, run and report the corpus."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--adversarial", type=int, default=DEFAULT_ADVERSARIAL_CASES)
    parser.add_argument("--known-correct", type=int, default=DEFAULT_KNOWN_CORRECT_CASES)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    output = args.output or default_report_path(args.seed)
    report = asyncio.run(
        _run_and_report(args.seed, args.adversarial, args.known_correct, output)
    )
    print(json.dumps({
        "adversarial": report.adversarial,
        "known_correct": report.known_correct,
        "report": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
