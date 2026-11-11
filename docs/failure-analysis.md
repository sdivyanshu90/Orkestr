# Failure analysis and design lessons

[Documentation home](README.md) · [Reliability](reliability.md) · [Evaluation](evaluation.md)

The most useful failures are concrete counterexamples with a recorded trigger,
consequence and regression. This page distinguishes historical observations
from currently qualified guarantees. Earlier implementation details remain in
Git history; new code still needs evidence on its supported release.

## 1. A successful task did not establish the original request

The historical local sweep recorded sixteen worker-gate passes, eleven without
worker tool calls. Existing fixture state and planner-chosen criteria could be
weaker than the requested edits. That result demonstrated a trust-boundary gap,
not sixteen independently correct solutions.

The current design captures owner criteria before planning and judges the final
candidate against that contract. The lesson is broader than this fix: re-running
an agent's own weak acceptance condition faithfully can still answer the wrong
question. Independent request semantics and independently owned oracles matter.

## 2. A correct predicate lost its meaning at a wrapper

A `command_output_matches` check failed because expected text was absent, but the
contract gate formerly interpreted exit code zero as a pass. The error was in
result propagation, not the predicate itself. Review reproduced the false accept;
the current gate uses the complete check result and rejected the same case.

Regression coverage must include exit-zero mismatch, bad regex, empty/skipped
required pytest selectors and advisory criteria. A wrapper cannot safely rebuild
a verdict from one field while discarding the error/predicate information.

## 3. Foreground serialization did not isolate timing

The first direct local probe run issued one foreground configuration at a time,
but a residual server from an interrupted workflow remained until cleanup. Its
presence could affect host/GPU memory pressure. The recorded responses and task
scores remain auditable; the timings are not clean hardware comparisons.

The later timing script registers servers while alive and checks process/port
closure after each model. The lesson is to observe resource ownership before and
after measurement rather than infer isolation from a loop's structure.

## 4. Resource helpers can be disconnected from callers

An owned-process registry, ownership fence or ledger can have excellent unit
tests while the real run never uses it. The implementation review found this
integration gap in earlier revisions. The current paths attach ownership and
accounting to orchestration/provider execution and add lifecycle tests.

The regression belongs at the run boundary: start actual work, interrupt it,
observe the host and durable state, then repeat. A test that explicitly invokes
a helper demonstrates that helper, not its use in every error branch.

## 5. Cancellation can outlive a clock or leak into the next run

A whole-run deadline must include planning, queues, verification and teardown,
not only scheduler attempts. Blocking synchronous work can also prevent an
asynchronous cancellation from being observed. Phase-aware scope and injection
coverage address these seams.

Ambient cancellation state must be released with a run. Otherwise a later run
can inherit an already-set token and start cancelled. The same lifecycle concern
applies to accounting scope and resource ownership. Test repeated runs in one
process as well as clean-process demos.

## 6. Git identities and patch content answer different questions

A patch can be based on a different commit identity with the same tree, while
independent patches can genuinely overlap despite looking small. Dependency
ordering, tree identity, actual changed files and patch provenance need separate
checks. Integration must judge the union, not merely trust individual success.

Untracked files also require attention: a plain diff can omit newly created
files, and a mock that repeats the same call indefinitely can exercise the loop
without completing work. Repository tooling and iteration termination deserve
explicit regression cases rather than assumptions borrowed from a happy path.

## 7. Passing checks and statistical confidence are different evidence

Randomized case generation increases coverage. It does not automatically create
independent failure modes. A report with hundreds of variants needs family-level
analysis and an independence argument. Likewise, repeated seeds on one task are
not new repositories, and a mean response time hides tail latency.

The scoring policy requires independent review and held-out real tasks so a
regression corpus cannot be mistaken for a broad product-performance claim.
Never delete failed, timed-out or contaminated samples to improve a headline.

## How to add a failure entry

Record the minimal trigger, affected release, observed versus expected outcome,
root boundary, fix and regression command. Retain raw evidence when safe to do
so, with credentials redacted. Label unsupported-host behavior and incomplete
validation explicitly. A narrative explanation without a reproducible regression
is a hypothesis, not closure evidence.
