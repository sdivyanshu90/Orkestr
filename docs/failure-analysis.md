# Failure analysis

This document records failures that actually occurred while building
`agent-squad`: the bugs, the design mistakes and the wrong turns, and what
each one taught. A project that only documents its successes teaches its
readers nothing about how to maintain it.

Every entry below describes a real failure in this repository. Where a bug
was found by a test rather than by inspection, that is stated.

---

## 4. A parallel patch was wrongly rejected as stale

**Symptom.** Two independent tasks both succeeded and both verified, but the
integration reported one conflict and failed:

```
integration passed=False applied=1 conflicts=1
  conflict stale_patch: patch was produced against tree 6cbfa3f66000
  but the integration tree is at 1b77ff8dad01
```

**Cause.** The integration engine decided that a patch was stale by
comparing the tree the patch was computed against with the integration
tree's *current* tree. That is the wrong comparison. The integration tree
changes every time a patch is applied, so the second of two independent
patches would always look stale.

The rule that was actually needed is about *provenance*, not about the
current state: a patch is stale when the tree it was computed against is
not the tree the orchestrator expects it to apply to — the run's base tree
for an independent task, the dependency's head tree for a dependent one.

**Fix.** The engine now computes the expected base tree from the
dependency structure and tracks the head tree each applied patch produced.

**Lesson.** "Stale" and "conflicting" are different concepts and they need
different checks. A stale patch is computed against a tree that no longer
exists; a conflicting patch is computed against a tree that does exist but
has been changed in the same place. Conflating them makes every parallel
patch look like a conflict, and — worse — makes a real conflict look like a
bookkeeping problem.

This bug also invalidated the first published benchmark results, which had
been measured while the parallel path was short-circuiting. The results
were re-measured after the fix, and the earlier "36 % slower on the tightly
coupled scenario" figure was withdrawn: it was an artefact of the bug, not a
property of the system. The corrected numbers show no measurable difference
between strategies.

---

## 5. The mock provider returned the same tool calls forever

**Symptom.** The first end-to-end run never terminated. Every task failed
after exactly twelve iterations with `agent exceeded the iteration limit`.

**Cause.** `MockProvider` synthesised its tool calls from the task brief on
*every* call. The agent loop fed the tool results back and asked again, and
the provider cheerfully produced the identical `write_file` calls. The loop
had no way to reach a terminal state.

**Fix.** The provider now checks whether the conversation already contains
tool results and, if so, returns a summary with no tool calls — the
behaviour of a well-mannered agent that does its work once.

**Lesson.** A mock that only mimics the *shape* of a model is not enough to
drive a stateful loop. The mock has to imitate the *protocol*, including
when to stop. This is the general hazard of substituting a deterministic
double for a stochastic component: the double exercises the paths the real
component takes, and silently skips the ones it does not.

---

## 6. `git diff` does not show untracked files

**Symptom.** A worker that created a brand new module produced an empty
patch. The task "succeeded", the acceptance criterion `file_exists` passed,
and the integration stage had nothing to apply.

**Cause.** `git diff` compares the working tree against the *index*. A file
that the worker created and never staged is untracked, and untracked files
do not appear in `git diff`.

This was the most consequential bug of the project, because it made the
system agree with itself: the worker said it wrote the file, the file
existed, the criterion passed, and the patch was empty. Only the
integration stage — which had nothing to apply — revealed the discrepancy.

**Fix.** `WorktreeManager.stage_all()` stages the worktree before the diff
is captured, and `diff(staged=True)` is used.

**Lesson.** "The file exists" and "the change is captured" are different
claims, and only one of them was being checked. It is a small instance of a
general pattern: *every stage of the pipeline needs its own independent
check*, because a pipeline that trusts an upstream stage's report inherits
its errors. The same reasoning motivates the verification gate re-executing
acceptance criteria instead of reading the worker's summary.

---

## 7. The commit sha chain broke stacked worktrees

**Symptom.** After the fix above, only the *first* patch was integrated. The
dependent task's patch was rejected as stale.

**Cause.** The integration engine compared the patch's recorded base
*commit id* with the integration worktree's current head. But the
integration engine creates its own commits for the same content, with
different commit messages — so the commit ids differed while the content
was identical.

**Fix.** Compare **tree** ids, which are content-addressed. The base tree of
each patch is recorded when the patch is captured, and the integration
engine recomputes the current tree after each commit.

**Lesson.** Identity in a git-based system has two levels, and choosing the
wrong one produces false conflicts. Commit ids identify *history*; tree ids
identify *content*. When the question is "does this patch apply to the tree
I have?", the answer is a tree id.

---

## 8. A deadlock in the budget manager

**Symptom.** The unit test suite hung. Not failed — hung, with three tests
reported and no progress.

**Cause.** `BudgetManager` used `threading.Lock()`. `record()` acquired the
lock and then called `scope()`, which acquired it again. A non-reentrant lock
deadlocks.

**Lesson.** A hang is a much worse failure mode than an exception: it burns
the whole test budget and hides everything else. The fix was `threading.RLock()`.
The deeper lesson is about *test design*: a unit test that acquires a lock
in a nested call path needs the lock to be re-entrant, and the test suite
found this only because it exercised `record()` directly. Nested locking is
a design smell that a lock type can hide rather than solve; the comment in
the code now says why the lock is re-entrant, so the next reader does not
"simplify" it back to `Lock`.

---

## 9. The policy engine was never called

**Symptom.** None. Every test passed. The code looked correct.

**Cause.** `PolicyEngine.authorize()` was implemented, unit-tested in
isolation, and wired into nothing. `Orchestrator` constructed the engine and
stored it; `AgentLoop._invoke_tool` accepted a `capabilities` argument and
ignored it. The *tool registry* restricted which tools a role could name
(reviewers got no `write_file`), but the capability layer — path scoping,
protected paths, approval gating, the audit log — was inert.

**How it was found.** By an independent documentation agent that read the
code for a threat model and asked "where is `authorize` called?". The
question "who calls the thing I just wrote?" is worth asking of every
security control.

**Fix.** `AgentLoop._invoke_tool` now consults the policy before executing
any tool, and handles all three outcomes: allow, deny (returned to the model
as a tool error), require-approval (routed to the approval broker, which
blocks until a decision is recorded).

**Lesson.** A security control that is not on the execution path is
documentation, not a control. This is the single most important failure in
this list, because everything the threat model claims about capabilities,
scoping and approvals depended on it.

---

## 10. The worktree was removed before verification

**Symptom.** Discovered by the documentation agent, which noticed that
worktrees were *never* removed — and then traced why.

**Cause.** Two bugs with opposite symptoms and the same root:

1. `WorkerRunner._cleanup` removed the worktree at the end of the worker's
   attempt — but the verification gate runs *after* the worker returns, so
   the checks ran against a directory that no longer existed.
2. The fix for "the worktree disappeared" was to keep it unconditionally
   (`keep_worktrees_on_failure` defaulted to `True` and was applied
   regardless of the outcome), which meant worktrees were never cleaned up.

**Fix.** The runner no longer disposes of the worktree at all. The
scheduler disposes of it *after* verification, keeping it when the attempt
failed, when verification failed, or when the configuration asks for it.

**Lesson.** Resource lifetime has to be owned by the component that knows
when the resource is still needed. The runner knew the work was done; it did
not know that verification was about to happen.

---

## 11. The tool context could not express "where"

**Symptom.** The policy engine checked paths against `scope_root` and
`scope_patterns`, but `ToolContext.scope_patterns` was populated and never
read, and `scope_root` was set to the worktree path.

**Cause.** The `ToolContext` carried the information; the *policy* was the
component that needed it, and the connection was never made.

**Lesson.** The same lesson as failure 5, in a different guise: data that
flows into an object is not the same as data that is used. The cure in both
cases is the same — a test that exercises the path end to end, rather than
testing the parts in isolation.

---

## 12. The recovery path lost the lease

**Symptom.** A task whose worker was still alive was resumed by the recovery
path, which could have started a second worker for the same task.

**Cause.** `StateStore.rebuild_projection` reconstructed each task's state
from the event log, but did not restore `lease_expires_at`. The lease is
recorded in the `task.attempt_started` event payload, and the rebuild simply
did not read it back. The lease check therefore saw `None` and treated the
task as abandoned.

**Fix.** The rebuild now restores the lease timestamp from the event.

**Lesson.** A projection that is *rebuilt* must be rebuilt **completely**,
or the rebuild is a data-loss bug wearing the costume of a recovery
mechanism. The dangerous shape of this bug is that it only appears after a
crash, when the person investigating has the least context.

---

## 13. `hash()` is salted, so the "deterministic" jitter was not

**Symptom.** Not a failure — a discrepancy between the code and its
documentation.

**Cause.** `Scheduler._backoff` used Python's built-in `hash()` to derive
its jitter factor, and the docstring called it deterministic. `hash()` on a
`str` is salted per process (`PYTHONHASHSEED`), so the value differed
between processes. Within one process it was stable; across a replay, on a
different machine, it was not.

**Fix.** `hashlib.sha256` over `run_id:task_id:attempt`.

**Lesson.** "Deterministic" is a claim about a *process boundary*, and
`hash()` fails that test. This matters specifically because replay depends
on it.

---

## 14. The approval flow existed but was unreachable

**Symptom.** `StateStore.create_approval` was implemented and stored
approvals in a table, and the CLI had an `approve` command — but nothing
ever created an approval, so the CLI always had nothing to approve. The
`AWAITING_APPROVAL` task status was never entered.

**Cause.** The approval *record* was built before the approval *request*
path existed. The two halves were never connected.

**Fix.** `agents/approval.py` implements `ApprovalBroker`: it creates the
approval, publishes an `APPROVAL_REQUEST` message, and polls the durable
store until a decision is recorded or the timeout expires. The policy
engine's `REQUIRE_APPROVAL` decision now routes through it.

**Lesson.** A feature that is *half* implemented is worse than one that is
absent, because it looks present. The audit trail showed approvals were
never requested, which is how the gap was noticed.

---

## 15. The benchmark harness crashed on the `adaptive` strategy

**Symptom.** `agent-squad benchmark` aborted with a pydantic
`ValidationError`: `max_workers` must be ≥ 1.

**Cause.** `STRATEGIES["adaptive"] = 0` — zero meaning "decided at
runtime" — was passed straight into `scheduler.max_workers`.

**Fix.** `max(1, STRATEGIES.get(strategy) or 4)`.

**Lesson.** A sentinel value that means "not yet known" leaked into a
validated field. The type system could not catch it because the sentinel
was a valid `int`.

---

## 16. The benchmark produced a result that proved nothing

**Symptom.** Not a crash — a *finding*. The first full benchmark run showed
every strategy achieving a verified completion rate of 1.0000, with wall
clock differences of 1–9 % against a standard deviation of ~0.22 s.

**Cause.** The mock provider has no latency, so the wall clock is dominated
by fixed costs (git worktree creation, subprocess startup, pytest
execution) rather than by model time. There was nothing for parallelism to
overlap.

**What was done.** Rather than presenting the numbers as a speed-up
comparison, the methodology document now states plainly that the harness
cannot demonstrate a model-latency speed-up, and the results document
presents the wall-clock column as a noise floor. The measured results were
kept, including the finding that the *tightly coupled* scenario was 36 %
slower under fixed fan-out — a real coordination cost that the theory
predicts.

**Lesson.** The most dangerous failure mode in a research project is not a
crash; it is a result that appears to answer the question when it does not.
Reporting "no measurable difference, and here is why the experiment cannot
produce one" is a more valuable output than a fabricated speed-up.

---

## 17. A fixture that failed for the right reason

**Symptom.** The end-to-end test failed at the `pytest_selector` criterion.

**Cause.** The fixture asserted `round_half_up(1.005, 2) == 1.01`. In
binary floating point, `1.005` is actually `1.00499999999999989…`, so
half-away-from-zero rounding gives `1.0`. The test was wrong.

**Fix.** The fixture now uses values that are exactly representable in
binary (`0.125`, `2.5`).

**Lesson.** Worth recording because of *how* it was found: the verification
gate caught a genuinely incorrect test. The gate is not a rubber stamp — it
executes the command and reports the exit code, and it was right. This is
the first time in this project that the verification machinery failed a
piece of work that turned out to be correct, which is a useful reminder
that a failing check is evidence, not an accusation.

---

## 18. The test suite asserted on wall-clock time

**Symptom.** A concurrency test asserting "two workers ran in parallel" was
flaky.

**Cause.** The test compared elapsed milliseconds against a threshold.

**Fix.** The test now inspects the *recorded event sequence*: the second
`agent.started` event must appear before the first `agent.finished` event.
That is a property of the scheduler's behaviour, not of the machine's speed.

**Lesson.** Concurrency tests should assert on *interleaving of observable
events*, never on durations. A test that fails on a loaded CI machine is a
test that will be ignored, and then it protects nothing.

---

## 19. Summary of the recurring patterns

| Pattern | Instances | The lesson |
|---|---|---|
| A component written but not connected | 5, 7, 10 | A control that is not on the execution path is documentation, not a control. |
| Sentinel values leaking across type boundaries | 11 | `0` meaning "unknown" is a bug waiting to happen. |
| Identity chosen at the wrong granularity | 3 | Compare content (trees), not history (commits). |
| Lifetime owned by the wrong component | 6 | The owner of a resource is the one that knows when it is still needed. |
| A result that appears to answer the question | 12, 16 | "We could not measure this" is a legitimate result. |
| A test asserting on timing | 14 | Assert on interleaving, not duration. |
| A lock that was not re-entrant | 4 | Nested locking is a design smell. |
| Two different concepts collapsed into one check | 4 | "Stale" and "conflicting" need different checks. |

The first row dominates. Four of the five most serious problems in this
project were components that existed, were tested in isolation, and were
never wired into the path that mattered. That is the strongest practical
argument for end-to-end tests that exercise the real path — and it is the
reason the integration, concurrency, recovery, security and chaos suites in
`tests/` exist at all.

The second pattern worth naming is **the mock provider's blind spot**. The
deterministic provider exercised the orchestration machinery faithfully
right up to the integration boundary, and the integration bug lived exactly
there — past the point where the mock's fidelity stopped. A deterministic
double is only as good as the paths it reaches; the chaos suite exists to
push past them.
