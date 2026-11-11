# 0005. Independent re-execution of acceptance criteria by the orchestrator

- Status: Accepted
- Date: 2026-11-10

## Reading this decision today

[Decision index](README.md) · [Current architecture](../architecture.md)
· [Security](../security-threat-model.md) · [Qualification](../scoring-criteria.md)

**Original decision: accepted on 2026-11-10. Context reviewed: 2026-11-11.**

The original task verifier is now complemented by an immutable owner contract and a final combined-tree contract gate. Complete check results, preserved invariants, valid no-ops and protected evaluator material matter. A predicate can fail despite exit code zero. The historical task-only measurements must not be relabeled as request-level acceptance under the newer gate.

The remaining sections preserve the original decision rationale and alternatives.
Their historical implementation descriptions are not a release qualification.

## Context

The central failure mode of LLM-driven software engineering is not a crash
and not a wrong answer — it is a *confident report of success for work that
was not done*. A model that wrote the wrong function, or that forgot a
file, or that misread a test failure, has a strong tendency to summarise
what it did as "implemented" and to state that the tests pass.

The problem is structural, not a matter of model quality:

1. **The generator and the judge are the same process, with the same
   context.** The model that produced the work has no independent
   information about whether the work is correct. It has the diff it wrote
   and its own narrative.
2. **The failure is silent and correlated.** If the model misunderstood the
   requirement, it will both implement the wrong thing *and* describe it
   accurately-but-wrongly as done. A second model asked to review the first
   model's work has the same blind spot plus a tendency to agree.
3. **The claim is not falsifiable.** "I implemented the rounding helper" is
   not something a reader can check without doing the work. "The file
   `calc.py` exists and contains `round_half_up`, and `pytest -q` on
   `tests/test_calc.py` exited 0" is.

The system already had a strong hint about the right shape. The acceptance
criterion was modelled as a typed object with a `kind` that selects a
*deterministic executor* and a `spec` that carries that executor's
parameters — `file_exists`, `file_contains`, `file_not_contains`,
`command_exits_zero`, `command_output_matches`, `pytest_selector`,
`diff_nonempty`, `diff_empty`, plus two advisory kinds, `llm_review` and
`manual`. The question was how those criteria should be evaluated.

The obvious answer — have the worker run them and report the results — is
the one the design explicitly rejects. A worker that both produces the
change and judges the change has no reason to discover its own error, and
the transcript gives no way for a reader to tell a passed check from an
assumed one.

The competing concern is cost and latency. Re-running every check for every
task doubles the command executions and adds wall-clock time. A design in
which verification is advisory or opt-in would be cheaper and would be
worthless for the project's stated purpose, which is to measure *verified*
completion rather than claimed completion.

There was also a subtlety worth getting right: "the tests pass" and "the
change is correct" are different claims, and the record should not collapse
them. A `pytest` invocation that exited 0 is evidence about the specific
selector that was run. It is not a proof that the change is correct. The
schema has to be able to say precisely what was executed and what it
returned, without claiming more than that.

## Decision

**The orchestrator re-executes the acceptance criteria itself, against the
worker's actual workspace, after the worker reports success. The worker's
claim is never sufficient. The recorded result is a `CheckRecord` describing
exactly what was run, and nothing more.**

### The mechanism

`Scheduler._execute` calls `Scheduler._verify(task, result)` after a
successful worker result and **before** the transition to `SUCCEEDED`.
`_verify` calls `VerificationGate.verify_task(task, workspace=...,
has_changes=..., evidence=...)`.

`VerificationGate._verify_criteria` iterates over the task's
`AcceptanceCriterion` objects and calls `verification.checks.run_criterion`
once per criterion. Each call returns a `CheckOutcome` wrapping a
`CheckRecord`:

```python
class CheckRecord(BaseModel):
    criterion_id: str
    kind: str
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    duration_s: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_artifact_id: str | None = None
    deterministic: bool = True
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and self.error is None
```

Every check runs against the *worker's own workspace* — the worktree path
the runner reported — not against a copy and not against the main
repository. `_resolve(workspace, relative)` in `verification/checks.py`
refuses any path that resolves outside the verification workspace.

### The pass/fail rule

Each criterion is classified from its `CheckRecord`:

| Condition | `CriterionState` |
|---|---|
| `record.deterministic` is `False` (i.e. `llm_review` or `manual`) | `ADVISORY` |
| `record.passed` is `True` | `EXECUTED_PASSED` |
| otherwise | `EXECUTED_FAILED` |

The report passes only if no **required** criterion failed:

```python
required = {c.id for c in criteria if c.required}
blocking_failures = [c for c in failed if c in required]
passed = not blocking_failures
```

The report's level is `"checks_passed"` when it passes and
`"validation_executed"` when it does not. A task with no criteria at all
gets level `"worker_reported"` and `passed=False`, with the error "the task
declares no acceptance criteria; nothing to verify" — the gate will not let
an unverifiable task pass silently.

### Advisory criteria are structurally excluded

`models/task.py` defines `ADVISORY_CHECKS = frozenset({LLM_REVIEW, MANUAL})`.
`run_criterion` returns a `CheckRecord` with `deterministic=False` and an
error for these kinds, so they land in the `advisory` list of the report and
contribute nothing to `passed`. A reviewer agent's opinion can be recorded
and surfaced; it can never satisfy a required criterion. `TaskSpec.
is_advisory_only` reports whether a task has any deterministic criterion at
all, and `TaskGraph.validate` emits the warning "task X has no deterministic
acceptance criteria; it cannot be machine-verified".

### The worker's own checks are excluded by construction

`WorkerRunner._build_evidence` constructs `EvidencePackage(checks=())`. The
worker contributes no check results whatsoever — only `changed_files`, the
patch hash, the criteria it claims to address, and explicit `limitations`
such as "the attempt produced no file changes" or "criteria X are advisory
only and were not independently verified". The gate then merges *its own*
records into the package:

```python
merged = list(evidence.checks) + list(report.checks)
evidence.checks = tuple(merged)
```

So the evidence chain records who actually executed each check, and a
worker's assertion cannot be confused with a recorded result.

### Failure is a retry, not a soft warning

A failed verification moves the task to `FAILED` with
`error_class="verification"`, and the scheduler treats that as always
retryable — the worker gets another attempt, up to the attempt budget, with
the failing report available as context for the next attempt.

### What the record does and does not claim

`docs/architecture.md` §4 gives the full level-by-level table. The essential
distinction: `checks_passed` means "these specific checks were executed
against this specific workspace and produced these specific results". It
does not mean the criteria were exhaustive, that the checks were meaningful,
that no other behaviour regressed, or that the change is correct. The
evidence module states the same thing in its own words: "Neither is a proof
of overall correctness, and the schema never claims otherwise."

## Alternatives considered

- **Trust the worker's report.** Rejected: it makes the transcript
  unfalsifiable, and it makes the "verified completion rate" metric
  meaningless — it would measure how confidently the model reports success,
  not how often the work is actually correct.
- **Have the worker run the checks and report the results, but require it to
  paste the output.** Rejected as insufficient: the worker still selects
  which checks to run, can misreport the output, and can omit a failing
  check entirely. Pasting output does not create independence.
- **A second LLM reviews the first LLM's work.** Rejected because it
  substitutes one biased judge for another and does not produce a record of
  *what was executed*. It is retained only as an explicitly advisory check
  kind (`llm_review`), where it can be recorded but never relied upon.
- **A separate "verifier agent" that shares the worker's filesystem but is
  prompted to be sceptical.** Rejected for the same reason, with the added
  problem that it is non-deterministic, so it cannot be replayed or
  benchmarked.
- **Verify only the final integrated tree, not per task.** Rejected: it
  gives one bit of information for the whole run and destroys the ability
  to attribute a failure to a task. It would also make retries useless,
  since a failing task cannot be retried in isolation.
- **Verify by inspecting the diff statically** (lint, type-check, review).
  Rejected as a *supplement*, not a replacement: static analysis cannot
  establish that a behaviour changed. The chosen design runs the project's
  own test suite, which is the strongest evidence available for
  behavioural claims.
- **Trust the CI system rather than re-running checks locally.** Rejected
  because it inverts the dependency: the orchestrator would have to push,
  which it is explicitly forbidden from doing (see [0004](0004-git-worktrees-plus-capability-layer.md)),
  and it would make the run depend on an external service's availability
  and queue semantics.
- **Run the checks only once, at integration, on the combined tree.**
  Rejected: the same attribution problem, and it removes the per-task
  retry signal.
- **Make verification optional, off by default, to save cost.** Rejected.
  `VerificationConfig.require_independent_reexecution` defaults to `True`,
  and `allow_llm_only_acceptance` defaults to `False`. A design where the
  central guarantee can be switched off is a design whose central guarantee
  does not exist.
- **Make verification advisory: record the results but let the task succeed
  regardless.** Rejected: it produces exactly the ambiguity the design
  exists to remove, where "succeeded" and "verified" are different things
  and the run summary cannot distinguish them.

## Consequences

**What this buys:**

- **"Done" means something.** The `SUCCEEDED` transition is gated on a
  report that the orchestrator produced by executing the criteria itself.
- **Failure is detected early and attributable.** A task that fails its
  checks is retried with the failing report in hand, rather than being
  integrated and discovered to be broken at the end.
- **The metrics become meaningful.** `observe/metrics.py` can compute
  `verified_completion_rate` and `tokens_per_verified_task` as distinct
  quantities, because the runtime knows which tasks passed an executed
  check and which merely reported success.
- **The evidence is replayable.** `CheckRecord` captures the command, the
  exit code, the output excerpts and the duration, so a reviewer can
  re-derive exactly what was checked. `EvidencePackage.state_for(criterion)`
  reports the strongest state achieved for a criterion — `EXECUTED_PASSED`,
  `EXECUTED_FAILED`, `ADVISORY` or `UNVERIFIED` — per criterion, not per
  task.
- **The security posture survives a compromised model.** A worker that
  claims to have passed a check it never ran produces a `FAILED`
  verification, because the gate runs the check itself.

**What it costs:**

- **Command executions roughly double.** Each task's checks run once
  inside the worker's loop (if the worker chose to run them) and once
  independently by the gate. The gate's executions are the ones that count.
- **Wall-clock and token cost per task increase.** Verification runs after
  the worker finishes, inside the task's deadline
  (`_timeout_for(task)`), so a slow test suite can consume the task's
  remaining time and cause a `TIMED_OUT` on an otherwise successful attempt.
- **The criteria have to be written well.** Verification is only as good as
  the acceptance criteria. A criterion that checks the wrong thing passes
  trivially. This is why `RuleBasedPlanner` warns explicitly when a plan
  has no deterministic criteria, and why `TaskGraph.validate` surfaces
  advisory-only tasks as warnings rather than silently accepting them.
- **Environment-dependent checks are environment-dependent.** A
  `pytest_selector` criterion fails if the environment is not set up, and
  the failure is indistinguishable from a genuine regression. The system
  records the exact command and exit code so the distinction is at least
  auditable.
- **Not a proof of correctness, and the documentation has to say so
  repeatedly.** The level names are deliberately precise
  (`checks_passed`, not `verified_correct`), and the run summary stores a
  *histogram of levels*, not a boolean "verified" flag.
