# Reproduction

[Documentation home](README.md) · [Environment setup](getting-started.md)
· [Evidence interpretation](evaluation.md) · [Qualification](scoring-criteria.md)

**Procedure, not a certificate.** Use a clean checkout and the exact release being
reviewed. Some commands write generated evidence; retain historical artifacts
before running them. A listed CI workflow is not evidence that its jobs passed.

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
| Python | 3.11, 3.12, 3.13 | `requires-python = ">=3.11"`; the CI workflow declares all three. |
| Providers | `mock`, `openai_compat`, `anthropic` | `mock` is deterministic and needs no network. `openai_compat` is what a local `llama-server` speaks. |
| Containment | `bubblewrap` (hardened profile), none (development profile) | `unshare --net` alone is *not* sufficient for the hardened profile: it cannot isolate the filesystem, so a host that offers only `unshare` is refused. |
| Authority | Single user, single host, local filesystem | There is no multi-user model and no distributed execution. |

Anything outside this table is not claimed. The lock additionally carries
wheel hashes for the generator host's CPython 3.14 so the development
`.venv` can install the same file; 3.14 is not a supported release
combination and is not in CI. In particular, a 9.5+ score
also requires mandatory `S3` (independent review) and `U1` (external operation),
which are not installation/reproduction steps. `U2` and `V4` are stretch criteria
for scores above 9.5, not additional mandatory gates for 9.5 itself.

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
python scripts/lock_requirements.py --check requirements.lock
python -m pip check
```

`requirements.lock` is generated, not edited. Its header records that
versions came from the installed distributions via `importlib.metadata` and
that every sha256 was computed over a real wheel fetched by pip for CPython
3.11/3.12/3.13 on manylinux x86-64. Regenerate it with:

```bash
python scripts/lock_requirements.py --output requirements.lock
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

To inspect a real run's evidence chain, run the deterministic offline demo
from a disposable git repository:

```bash
DEMO="$(mktemp -d)/demo"
mkdir -p "$DEMO/tests"
cd "$DEMO"
git init -b main
printf 'def add(a, b):\n    return a + b\n' > calc.py
printf 'import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n' > conftest.py
printf 'from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n' > tests/test_calc.py
git add -A
git -c user.name=reviewer -c user.email=reviewer@localhost commit -m initial
AGENT_SQUAD=/path/to/agent-squad
agent-squad run "Add round_half_up" --repo . \
  --config "$AGENT_SQUAD/configs/offline.toml" \
  --spec "$AGENT_SQUAD/examples/request-rounding.json" --json > run.json
```

Check the verdict, then validate every artifact record and its blob:

```bash
python - <<'PY'
import json

run = json.load(open("run.json"))
contract = run["contract"]
assert run["status"] == "succeeded", run["status"]
assert contract is not None and contract["outcome"] == "accepted", contract
assert contract["binding_hash"], "the verdict is not bound to its inputs"
for verdict in contract["verdicts"]:
    assert verdict["state"] != "not_satisfied", verdict
print("accepted:", contract["binding_hash"])
PY

RUN_ID="$(python -c 'import json; print(json.load(open("run.json"))["run_id"])')"
agent-squad inspect "$RUN_ID" --artifacts --json > artifacts.json
python - <<'PY'
import hashlib, json
from pathlib import Path
from agent_squad.models.artifact import Artifact

payload = json.load(open("artifacts.json"))
assert payload["artifacts"], "the run recorded no artifacts"
for raw in payload["artifacts"]:
    artifact = Artifact.model_validate(raw)
    blob = Path(artifact.uri)
    assert blob.is_file(), blob
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == artifact.sha256
print(f"{len(payload['artifacts'])} artifacts validated")
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
(cd "$AGENT_SQUAD" && python -m pytest \
  tests/unit/test_schema_migration.py tests/integration/test_backup_restore.py -q)
python "$AGENT_SQUAD/scripts/backup_restore.py" --help
```

Migrations are ordered and idempotent, keyed on SQLite `PRAGMA user_version`.
A file left by an older release (`user_version = 0` but the baseline tables
present) is adopted at its recorded version and upgraded in place; a file
written by a newer build is refused rather than used with an older schema.
Opening the store twice applies nothing the second time.

The store runs in WAL mode, so a filesystem copy of `state.db` is not a
backup. Take a consistent snapshot while the runtime is live, and restore it
only after the runtime has stopped writing:

```bash
RUN_DB=".agent-squad/runs/$RUN_ID/state.db"
python "$AGENT_SQUAD/scripts/backup_restore.py" backup \
  --database "$RUN_DB" --output "$HOME/agent-squad-backups/$RUN_ID.db"
python "$AGENT_SQUAD/scripts/backup_restore.py" verify \
  --database "$HOME/agent-squad-backups/$RUN_ID.db"
python "$AGENT_SQUAD/scripts/backup_restore.py" restore \
  --backup "$HOME/agent-squad-backups/$RUN_ID.db" --database "$RUN_DB"
```

The snapshot is checked with `PRAGMA integrity_check` before it replaces
anything, and the swap is an atomic rename. Restoring a backup and re-opening
it must produce an equivalent projection.

---

## 8. Metrics

```bash
agent-squad benchmark --suite full --trials 3 --json > benchmark.json
(cd "$AGENT_SQUAD" && python -m pytest tests/evaluation/test_evaluation_statistics.py -q)
```

The evaluator derives every published metric from the raw artifacts,
including failed, timed-out and cancelled work — the denominator is the
number of dispatched attempts, not the number that happened to finish.
Rates use exact one-sided binomial limits; latency and cost comparisons use
clustered intervals rather than treating repeated trials as independent:

```python
from agent_squad.benchmark.statistics import binomial_limit

# Example: 18 accepted out of 20 trials.
print(binomial_limit(18, 20).to_dict())
```

Reproduction means agreement **within measured intervals**, not identical
timings:

1. Run at least three trials per task/strategy cell and retain every result.
   Failed, timed-out and cancelled trials stay in the denominator.
2. Report an acceptance rate with a 95% exact binomial interval. Zero false
   accepts in 300 cases gives a one-sided 95% upper limit of about 0.994%,
   not 0%.
3. Report p50 and p95 wall-clock separately; a mean alone hides the tail.
4. For paired strategy comparisons, use paired differences and a clustered
   interval by repository/task. Repeated seeds are not independent samples.
5. Record cold/warm conditions, machine load, CPU/GPU model and process
   occupancy with the sample. A noisy host widens intervals; it does not
   license discarding inconvenient samples.
6. Overlapping timing intervals are unresolved, not proof that one
   configuration is faster.

---

## 9. CI matrix

`.github/workflows/ci.yml` runs one job per supported interpreter on
`ubuntu-24.04`:

| Cell | Python | Steps |
|---|---|---|
| 1 | 3.11 | hash-locked install, `ruff`, `mypy`, `pytest` |
| 2 | 3.12 | hash-locked install, `ruff`, `mypy`, `pytest` |
| 3 | 3.13 | hash-locked install, `ruff`, `mypy`, `pytest` |

The workflow is the source of truth for CI status: link the successful run
for the release commit in the assessment record. This document does not
claim a cell passed on its own.

---

## 10. What a reviewer should record

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

## Interpreting reproduction outcomes

A successful reproduction records its environment, command, scope and raw result.
A default corpus test is a stratified sample unless the full-corpus flag is set.
A capability-dependent skip documents an unavailable boundary; it cannot qualify
that host as supporting the skipped behavior. A timed-out suite is incomplete,
not a pass. Preserve pytest importlib mode when overriding `addopts`.

The base package currently has no `api` extra. Optional FastAPI/uvicorn setup is
explicit in [configuration](configuration.md), and optional API/UI evidence must
be established separately rather than inferred from the base lock or a skipped
test. The developer Python 3.14 environment is outside the stated release matrix.

Assessment evidence is pinned to its declared commit and can become stale after
source, documentation or configuration changes. `build_assessment.py --check`
validates/recomputes without publishing a replacement record. Do not convert
INCONCLUSIVE or MISSING to PASS without the policy's independent reproduction.
