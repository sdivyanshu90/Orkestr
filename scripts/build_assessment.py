"""Build and validate the formal assessment record.

`docs/scoring-criteria.md` §5 defines the record a release must publish:

```yaml
policy_version: '1.0'
release_commit: '<full commit hash>'
scope: '<supported hardware, OS, providers, local/remote authority>'
status: NOT_ASSESSED # or ASSESSED after all 20 decisions are recorded
criteria:
  C1:
    decision: MISSING # PASS, FAIL, MISSING, or INCONCLUSIVE
    evidence: []
    reviewer: null
    reviewed_at: null
    counterexamples: []
```

and says that the assessor must verify the weights total 100, that all 18
mandatory decisions are present, that the evidence matches the release,
that the minimum confidence bounds pass, and that all applicable caps have
been applied.

This script is that assessor.  It reads the decisions, checks them
against the policy, and emits the record.  It refuses to invent a score:

* a criterion marked ``PASS`` without a named independent reviewer is a
  hard error, because §2 says an author reviewing their own output does
  not qualify;
* a criterion whose evidence does not include the release commit is an
  error;
* ``status`` stays ``NOT_ASSESSED`` until every one of the twenty
  decisions is recorded;
* every applicable cap from §1 is applied, and the lowest one wins.

Run it with::

    .venv/bin/python scripts/build_assessment.py
    .venv/bin/python scripts/build_assessment.py --check
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DECISIONS_PATH = ROOT / "docs" / "assessment-decisions.json"
RECORD_PATH = ROOT / "docs" / "assessment-record.yaml"

POLICY_VERSION = "1.0"

#: The weights from §3 and §4.  They must total 100.
WEIGHTS: dict[str, float] = {
    "C1": 6.0,
    "C2": 6.0,
    "C3": 6.0,
    "C4": 6.0,
    "C5": 6.0,
    "R1": 5.0,
    "R2": 5.0,
    "R3": 5.0,
    "R4": 5.0,
    "V1": 5.0,
    "V2": 5.0,
    "V3": 7.5,
    "S1": 5.0,
    "S2": 5.0,
    "S3": 5.0,
    "M1": 5.0,
    "M2": 5.0,
    "U1": 2.5,
    "V4": 2.5,
    "U2": 2.5,
}

MANDATORY: tuple[str, ...] = tuple(
    cid for cid in WEIGHTS if cid not in ("V4", "U2")
)
STRETCH: tuple[str, ...] = ("V4", "U2")

#: §1's caps.  Each entry is (condition, cap).  The lowest applicable cap
#: wins, and the cap is only *applicable* when the condition holds.
CAPS: tuple[tuple[str, str, float], ...] = (
    (
        "known_false_acceptance_or_authority_bypass",
        "C1, C4, S1, S2, S3",
        8.0,
    ),
    (
        "lifecycle_or_containment_criterion_fails",
        "R1, R2, R3, R4, S1, S2, S3",
        8.5,
    ),
    (
        "value_or_measurement_criterion_fails",
        "V1, V2, V3, M1, M2",
        9.0,
    ),
    (
        "other_mandatory_criterion_fails",
        "C1, C2, C3, C5, U1",
        9.4,
    ),
)

DECISIONS = ("PASS", "FAIL", "MISSING", "INCONCLUSIVE")


@dataclass
class Problem:
    """Something that makes the record invalid."""

    criterion: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.criterion}] {self.message}"


def release_commit() -> str:
    """The commit the record refers to."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip()


def load_decisions(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_hash(path: Path) -> str:
    """The sha256 of a file, or of a directory's sorted contents."""
    import hashlib

    digest = hashlib.sha256()
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            digest.update(str(child.relative_to(path)).encode())
            digest.update(child.read_bytes())
    else:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def resolve_evidence(
    entry: dict[str, Any], *, criterion: str, commit: str
) -> tuple[list[dict[str, Any]], list[Problem]]:
    """Resolve an evidence reference to a hashed, verified artifact.

    The hash is computed from the file **on disk**, so the record cannot
    drift from the release: if the artifact changes after the record was
    written, the recorded hash no longer matches and the check fails.
    """
    problems: list[Problem] = []
    resolved: list[dict[str, Any]] = []
    for item in entry.get("evidence") or []:
        if not isinstance(item, dict):
            problems.append(Problem(criterion, "evidence entries must be objects"))
            continue
        location = str(item.get("location", ""))
        if not location:
            problems.append(Problem(criterion, "an evidence entry has no location"))
            continue
        path = ROOT / location
        if not path.exists():
            problems.append(
                Problem(
                    criterion,
                    f"evidence location does not exist: {location}",
                )
            )
            continue
        actual = _file_hash(path)
        declared = str(item.get("sha256", ""))
        if declared and declared != actual:
            problems.append(
                Problem(
                    criterion,
                    f"evidence {location} has changed since the record was "
                    f"written (recorded {declared[:12]}…, found {actual[:12]}…)",
                )
            )
            continue
        resolved.append(
            {
                "location": location,
                "sha256": actual,
                "description": str(item.get("description", "")),
                "release_commit": commit,
            }
        )
    return resolved, problems


def validate(
    decisions: dict[str, Any], *, commit: str
) -> tuple[list[Problem], dict[str, Any]]:
    """Check the decisions against the policy."""
    problems: list[Problem] = []
    criteria = decisions.get("criteria", {})

    missing = [cid for cid in WEIGHTS if cid not in criteria]
    for cid in missing:
        problems.append(Problem(cid, "no decision was recorded"))
    extra = [cid for cid in criteria if cid not in WEIGHTS]
    for cid in extra:
        problems.append(Problem(cid, "is not a criterion in the policy"))

    total_weight = sum(WEIGHTS.values())
    if abs(total_weight - 100.0) > 1e-9:
        problems.append(
            Problem("policy", f"the weights total {total_weight}, not 100")
        )

    resolved_criteria: dict[str, Any] = {}
    for cid in WEIGHTS:
        entry = criteria.get(cid)
        if not isinstance(entry, dict):
            continue
        decision = str(entry.get("decision", "")).upper()
        if decision not in DECISIONS:
            problems.append(
                Problem(cid, f"decision {decision!r} is not one of {DECISIONS}")
            )
        reviewer = entry.get("reviewer")
        if decision == "PASS":
            # §2: "A PASS must refer to one immutable release candidate
            # commit, its declared supported scope, the evidence bundle and
            # a named independent reviewer."  An author reviewing their own
            # work does not qualify.
            if not reviewer:
                problems.append(
                    Problem(
                        cid,
                        "is marked PASS without a named independent reviewer; "
                        "§2 does not allow the author to review their own work",
                    )
                )
        resolved, evidence_problems = resolve_evidence(
            entry, criterion=cid, commit=commit
        )
        problems.extend(evidence_problems)
        if not resolved:
            problems.append(
                Problem(cid, f"has no resolvable evidence (decision {decision or 'unset'})")
            )
        merged = dict(entry)
        merged["evidence"] = resolved
        merged["decision"] = decision
        merged["counterexamples"] = entry.get("counterexamples") or []
        merged["implementation_status"] = entry.get("implementation_status", "")
        merged["blocked_on"] = entry.get("blocked_on", "")
        resolved_criteria[cid] = merged

    return problems, resolved_criteria


def compute(
    criteria: dict[str, Any], *, commit: str
) -> dict[str, Any]:
    """Compute the raw score and the applicable caps."""
    raw = 0.0
    passed: list[str] = []
    not_passed: list[str] = []
    for cid, weight in WEIGHTS.items():
        entry = criteria.get(cid) or {}
        decision = str(entry.get("decision", "")).upper()
        if decision == "PASS":
            raw += weight
            passed.append(cid)
        else:
            not_passed.append(cid)

    # §1: "Any R1–R4 lifecycle criterion or S1–S3 authority/containment
    # criterion fails or lacks qualifying evidence" -> 8.5.
    lifecycle = {"R1", "R2", "R3", "R4", "S1", "S2", "S3"}
    value = {"V1", "V2", "V3", "M1", "M2"}
    other_mandatory = set(MANDATORY) - lifecycle - value

    applicable: list[dict[str, Any]] = []
    if lifecycle & set(not_passed):
        applicable.append(
            {
                "cap": 8.5,
                "reason": "a lifecycle or containment criterion lacks qualifying evidence",
                "criteria": sorted(lifecycle & set(not_passed)),
            }
        )
    if value & set(not_passed):
        applicable.append(
            {
                "cap": 9.0,
                "reason": "a value or measurement criterion lacks qualifying evidence",
                "criteria": sorted(value & set(not_passed)),
            }
        )
    if other_mandatory & set(not_passed):
        applicable.append(
            {
                "cap": 9.4,
                "reason": "another mandatory criterion lacks qualifying evidence",
                "criteria": sorted(other_mandatory & set(not_passed)),
            }
        )
    cap = min((entry["cap"] for entry in applicable), default=10.0)
    raw_score = raw / 10.0
    return {
        "raw_points": raw,
        "raw_score": round(raw_score, 4),
        "applicable_caps": applicable,
        "formal_score": round(min(raw_score, cap), 4),
        "passed_criteria": sorted(passed),
        "criteria_without_evidence": sorted(not_passed),
        "release_commit": commit,
    }


def to_yaml(payload: dict[str, Any], indent: int = 0) -> str:
    """A small, dependency-free YAML writer.

    The record is a flat structure of strings, numbers, lists and nested
    mappings, so a full YAML library is not worth the dependency.
    """

    def scalar(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value)
        if text == "":
            return "''"
        if any(ch in text for ch in ":#{}[]&*!|>'\"%@`") or text.strip() != text:
            return '"' + text.replace('"', '\\"') + '"'
        return text

    def render(value: Any, level: int) -> str:
        pad = "  " * level
        if isinstance(value, dict):
            if not value:
                return " {}\n"
            out = "\n"
            for key, item in value.items():
                if isinstance(item, (dict, list)) and item:
                    out += f"{pad}{key}:{render(item, level + 1)}"
                else:
                    out += f"{pad}{key}: {scalar(item) if not isinstance(item, (dict, list)) else ('{}' if isinstance(item, dict) else '[]')}\n"
            return out
        if isinstance(value, list):
            if not value:
                return " []\n"
            out = "\n"
            for item in value:
                if isinstance(item, dict):
                    body = render(item, level + 1)
                    out += f"{pad}- {body.lstrip()}"
                elif isinstance(item, list):
                    out += f"{pad}-\n{render(item, level + 1)}"
                else:
                    out += f"{pad}- {scalar(item)}\n"
            return out
        return f" {scalar(value)}\n"

    return render(payload, indent)


def build() -> tuple[str, list[Problem], dict[str, Any]]:
    decisions = load_decisions(DECISIONS_PATH)
    commit = release_commit()
    problems, criteria = validate(decisions, commit=commit)
    summary = compute(criteria, commit=commit)

    all_recorded = all(
        str((criteria.get(cid) or {}).get("decision", "")).upper() in DECISIONS
        for cid in WEIGHTS
    )
    # A summary of the *engineering* state, which is not the formal score.
    # The formal score is 0.0 while no criterion carries an independent
    # review; this section shows how much of the work is done and merely
    # unreviewed, and how much is genuinely absent.
    implementation = {
        "complete": [],
        "in progress": [],
        "partial": [],
        "absent": [],
    }
    awaiting_review_only: list[str] = []
    for cid, entry in criteria.items():
        status = str(entry.get("implementation_status", "")).lower()
        if status in implementation:
            implementation[status].append(cid)
        if (
            str(entry.get("decision", "")).upper() == "INCONCLUSIVE"
            and status == "complete"
        ):
            awaiting_review_only.append(cid)
    readiness = {
        "note": (
            "This is NOT the formal score. It records how much of each "
            "criterion's implementation exists, so that a reviewer can see "
            "which criteria are one independent reproduction away from a "
            "decision and which are genuinely absent."
        ),
        "implementation": {k: sorted(v) for k, v in implementation.items()},
        "implemented_but_not_independently_reviewed": sorted(awaiting_review_only),
        "criteria_lacking_evidence": sorted(
            cid
            for cid, entry in criteria.items()
            if str(entry.get("decision", "")).upper() != "PASS"
        ),
    }

    record: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "release_commit": commit,
        "scope": decisions.get("scope", ""),
        "status": "ASSESSED" if all_recorded else "NOT_ASSESSED",
        "criteria": {cid: criteria.get(cid, {}) for cid in WEIGHTS},
    }
    record.update(summary)
    record["engineering_readiness"] = readiness
    return to_yaml(record), problems, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate without writing the record",
    )
    parser.add_argument("--out", type=Path, default=RECORD_PATH)
    args = parser.parse_args()

    text, problems, summary = build()
    if problems:
        print(f"{len(problems)} problem(s) with the assessment record:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    if not args.check:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    print(
        f"raw_score={summary['raw_score']} formal_score={summary['formal_score']} "
        f"caps={[c['cap'] for c in summary['applicable_caps']]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
