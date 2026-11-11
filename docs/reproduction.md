# Reproduction

This is the procedure an independent reviewer follows on a clean machine.
It exists because `docs/scoring-criteria.md` M2 requires that a reviewer be
able to reproduce the installation, the verifier verdicts, the
artifact-schema checks, recovery and the metric generation — and because a
claim that cannot be reproduced is not evidence.

Timings are **not** expected to be identical. The policy asks the
reproduction to explain numerical variance with measured intervals, not to
reproduce a stopwatch. Every timing claim in this repository is a
distribution over a recorded sample; the sample, its conditions and its
intervals are the evidence.

---

## 1. Supported scope

| Dimension | Supported | Notes |
|---|---|---|
| OS | Linux x86-64 (Ubuntu 24.04 tested) | The containment boundary uses Linux namespaces. macOS/Windows are not a supported hardened environment. |
| Python | 3.11, 3.12, 3.13 | `requires-python = ">=3.11"`; CI runs all three. |
| Providers | `mock`, `openai_compat`, `anthropic` | `mock` is deterministic and needs no network. `openai_compat` is what a local `llama-server` speaks. |
| Containment | `bubblewrap` (hardened profile), none (development profile) | `unshare --net` alone is *not* sufficient for the hardened profile: it cannot isolate the filesystem, so a host that offers only `unshare` is refused. |
| Authority | Single user, single host, local filesystem | There is no multi-user model and no distributed execution. |

Anything outside this table is not claimed. In particular, a 9.5+ score
also requires `S3` (an implementation-independent audit), `U1`/`U2`
(30–90 days of external usage) and `V4` (a second independent team), none
of which is a reproduction step.

---

## 2. Install

```bash
git clone <repository> agent-squad
cd agent-squad
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip

# The lock is hash-pinned.  A changed artefact fails here rather than
# producing a silently different environment.
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
```

Verify the environment is the locked one:

```bash
python -m pip freeze | diff - requirements.lock && echo "environment matches the lock"
```

If `--require-hashes` fails on a platform the lock does not cover, the
supported answer is **not** to drop the flag: it is to record the
unsupported platform, because that is exactly the reproducibility claim
the criterion is testing.

---

## 3. The verifier

The acceptance boundary is the contract gate. It judges the **final combined
tree** against an immutable request contract, and it evaluates every required
outcome against **both** the base tree and the candidate tree.

```bash
# The deterministic end-to-end path (no network, no model API).
python -m pytest tests/integration/test_end_to_end.py -q

# The negative cases, including the exit-zero output mismatch.
python -m pytest tests/evaluation/test_adversarial_contracts.py -q
```

What to check by eye, because it is the whole point:

* a command that exits zero while producing the wrong output is **rejected**;
* a run whose candidate tree is byte-identical to the base tree is
  **inconclusive**, not accepted;
* an outcome that held before the work is `preserved` only when the run
  touched what the outcome is about, and `pre_satisfied` otherwise;
* a contract with no required outcomes is **inconclusive**.

Reproduce the counterexample directly:

```bash
python -m pytest \
  "tests/evaluation/test_adversarial_contracts.py::test_an_output_mismatch_is_rejected_through_the_normal_run_entry_point" -q
```

---

## 4. The held-out corpus (C3)

```bash
AGENT_SQUAD_CORPUS_FULL=1 python -m pytest tests/evaluation/test_corpus.py -q
python - <<'PY'
import json, glob
report = json.load(open(sorted(glob.glob("benchmarks/results/corpus-*.json"))[-1]))
print(json.dumps(report["summary"], indent=2))
PY
```

Expected shape of the summary (values depend on the seed, the thresholds do
not):

| Field | Requirement |
|---|---|
| `adversarial_cases` | ≥ 300 |
| `false_accepts` | 0 |
| `upper_confidence_limit_95` | < 0.01, exact one-sided Clopper–Pearson |
| `known_correct_cases` | ≥ 100 |
| `lower_confidence_limit_95` | ≥ 0.95, exact one-sided Clopper–Pearson |

The corpus generator is deterministic from its seed, and every case carries
a distinct contract hash; `test_the_cases_in_a_family_are_not_cosmetic_variants`
is the guard against a corpus that looks large and is actually repeated.

---

## 5. Artifact schema

Artifacts are content-addressed and their manifests are validated on load:

```bash
python -m pytest tests/unit/test_artifacts.py tests/unit/test_evidence_and_messages.py -q
```

To inspect a real run's evidence chain:

```bash
agent-squad run --config configs/demo.toml --json > run.json
python - <<'PY'
import json
run = json.load(open("run.json"))
print(json.dumps(run["contract"], indent=2))
PY
```

The contract verdict records the contract hash, the base revision, the
candidate tree hash, the verifier version, the executed commands and the
state of every required outcome.

---

## 6. Recovery

```bash
python -m pytest tests/recovery tests/lifecycle -q
```

This covers projection rebuild, interrupted-task recovery, ownership
takeover, reconciliation of a `SIGKILL`ed owner, PID reuse, lost registry
writes, and the hundred-cycle matrix across nine failure categories.

The five-second teardown grace and the thirty-second reconciliation bound
are the numbers to check; both are asserted with `time.monotonic()`.

---

## 7. Schema migrations and backup/restore

```bash
python -m pytest tests/unit/test_schema_migration.py tests/integration/test_backup_restore.py -q
python scripts/backup_restore.py --help
```

Migrations are ordered and idempotent, keyed on SQLite `PRAGMA user_version`.
Restoring a backup and re-opening it must produce an equivalent projection.

---

## 8. Metrics

```bash
python scripts/render_local_results.py --help
python -m pytest tests/evaluation/test_evaluation_statistics.py -q
```

The evaluator derives every published metric from the raw artifacts,
including failed, timed-out and cancelled work — the denominator is the
number of dispatched attempts, not the number that happened to finish.

---

## 9. What a reviewer should record

For each criterion, the evidence bundle must contain: the release commit,
the environment identifiers above, the raw outcomes (including failures and
cancellations), the content hashes, the reviewer's identity and date, the
decision, and any unresolved defect.

`docs/assessment-record.yaml` is generated by
`scripts/build_assessment.py`; it refuses to mark a criterion `PASS`
without a named reviewer, and it recomputes every evidence hash from the
file on disk, so a record cannot drift from the release it describes.

```bash
python scripts/build_assessment.py --check
```
