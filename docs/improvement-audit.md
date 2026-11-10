# Improvement audit

This is an audit of the project against itself: what is claimed, what is
demonstrated, and what remains to be done.  It is written to be *useful*,
not flattering.  Every item states the evidence for the gap, the
consequence of leaving it, and a concrete first step.

**Method.** The audit was produced by instrumenting the codebase rather than
by reading it: a static reachability script (`scripts/audit_dead_code.py`)
identifies symbols that are defined but never reached, and every candidate
was then either exercised by a test, wired into the runtime, or removed.
The list below is what survived that process.

---

## 1. The summary table

| # | Gap | Severity | Blocks the research question? |
|---|---|---|---|
| 1 | Adversarial task fixtures | **Critical** | Yes — the completion-rate half |
| 2 | The strategy selector is uncalibrated | High | Partially |
| 3 | No replanning during execution | High | No |
| 4 | The event log grows without bound | Medium | No |
| 5 | Message validation state is not durable | Medium | No |
| 6 | The verification gate re-runs the whole suite per task | Medium | No |
| 7 | The scheduler's ready scan is O(V + E) per tick | Low | No |
| 8 | `ScopeOverlap` detection is O(V² · F) | Low | No |
| 9 | Dependency integrity is not managed | Medium | No |
| 10 | No static type checking in the toolchain | Medium | No |
| 11 | Delegation is bounded but not implemented | Low | No |
| 12 | No counterfactual branching from a checkpoint | Low | No |
| 13 | The dashboard's timeline is derived, not persisted | Low | No |

> **Update.** Gap 1 in the previous revision of this document was "no
> latency-bearing provider". That gap has been **closed**: the runtime now
> benchmarks against eight real, locally served models
> (`src/agent_squad/benchmark/local.py`), and the measured results are in
> [`docs/local-benchmark-results.md`](local-benchmark-results.md). What
> remains open is the *calibration* of the strategy selector against those
> measurements, which is gap 2.

---

## 2. The two critical gaps

### 2.1 No latency-bearing provider

**Evidence.** `agents/benchmark/harness.py` always constructs a
`MockProvider`. `docs/benchmark-results.md` §7 states the consequence: the
mock has no latency, so the critical path cannot shrink and the measured
wall-clock differences are one to two orders of magnitude below the
run-to-run standard deviation.

**Consequence.** The research question has two halves — *verified
completion rate* and *latency/cost*. The harness can currently only
address the first, and only weakly (every fixture succeeds, so the metric
has no room to move).

**What would fix it.** A provider adapter that can inject a realistic
per-call latency. The `ModelProvider` protocol already abstracts this; the
missing piece is a `LatencyProvider` wrapper that sleeps for a
configurable, distributionally realistic delay *and* reports the delay as
part of the usage record. The orchestrator needs no changes.

**Second step.** Point the same harness at a real endpoint. The
`ProviderRouter` already handles fallbacks and per-provider concurrency, so
the only work is the credential plumbing, which `ProviderConfig.api_key_env`
already does.

### 2.2 No adversarial task fixtures

**Evidence.** Every scenario in `agents/benchmark/tasks.py` is designed to
succeed. `docs/benchmark-results.md` §4.5 states this explicitly.

**Consequence.** The verified completion rate is 1.0000 for every strategy
on every scenario. The metric that was supposed to discriminate between
strategies cannot.

**What would fix it.** Fixtures where the *wrong decomposition* produces a
plausible but incorrect result. Concretely:

- a task whose acceptance criterion is satisfiable by a stub;
- two tasks whose union is correct but whose halves are each wrong;
- a task where the correct fix requires a change the planner was told not
  to make;
- a fixture where the single-agent strategy succeeds and the multi-agent
  strategy conflicts.

The fourth is the most interesting, because it is the case where
multi-agent execution should *lose*.

**Design note.** These fixtures must be written so that the *expected*
outcome is not encoded in the fixture. The harness must be able to observe
a strategy losing — that is the whole point.

---

## 3. The high-severity gaps

### 3.1 The strategy selector is uncalibrated

**Evidence.** `agents/strategy/heuristics.py:CoordinationModel` uses
hand-chosen constants (`coordination_overhead_tokens=1200`,
`context_duplication_tokens=1500`). The selector's *predicted* 22.5 %
improvement on the `independent_parallel` scenario was **not observed** in
the published benchmark.

**Consequence.** The selector is explainable and deterministic, but its
predictions are not trustworthy. The benchmark publishes predictions and
observations side by side, which is the precondition for fixing this — but
nothing consumes the comparison.

**First step.** The benchmark already records the decision in
`run.strategy_selected`. Extending it to record the *predicted* speed-up
next to the *observed* one, and fitting the constants to minimise the
error over the corpus, is a contained change with no architectural impact.

### 3.2 No replanning during execution

**Evidence.** `EventType.PLAN_REVISED` is defined in
`agents/store/events.py` and *read* by the replayer, but never *emitted*.
The plan is produced once, in `Orchestrator._execute`, before the scheduler
starts.

**Consequence.** A task that fails verification is retried with the same
brief. If the brief is wrong, the retry is wrong too. The retry budget
bounds the damage but does not address the cause.

**First step.** The plumbing is already there: `LLMPlanner` can be invoked
again, `TaskGraph` supports replacing a task's dependencies, and
`StateStore.upsert_task` replaces a spec while preserving the execution
history. What is missing is the *trigger* and the *bound*: a supervisor
that may revise the graph at most `k` times, and only in response to a
verification failure.

---

## 4. The medium-severity gaps

### 4.1 The event log grows without bound

**Evidence.** `agents/store/events.py` has no compaction and no retention
policy. `agents/store/db.py` has no vacuuming.

**Consequence.** A long-running installation accumulates an unbounded
table. `rebuild_projection` replays the *entire* log, so recovery time
grows linearly with the run's history.

**First step.** A `compact_events(run_id, before_seq)` operation that
folds superseded `task.status_changed` events into a single summary event
and keeps the rest. The log stays append-only for everything that matters
(a transition *into* a terminal state is evidence and is never folded).

### 4.2 Message validation state is not durable

**Evidence.** `agents/messaging.py:MessageBus._seen_ids` and `_versions`
are in-memory `set`/`dict`.

**Consequence.** After a restart, a message that was already delivered can
be delivered again. The state machine is unaffected — the attempt fence
(I3) is what protects correctness — but the *record* can contain a
duplicate.

**First step.** Move both into the `messages` table: the id is already the
primary key, so duplicate detection is a single `INSERT OR IGNORE` with a
row-count check, and the version becomes a column.

### 4.3 The verification gate re-runs the whole suite per task

**Evidence.** `agents/verification/checks.py` executes the criterion's
command from scratch. A `pytest_selector` criterion runs the full pytest
process, and the integration suite runs again.

**Consequence.** On a real repository with a slow test suite, verification
dominates the wall-clock time and the *measured* coordination overhead is
swamped by it.

**First step.** Cache pytest results per (commit tree, selector) pair. The
tree id is already the right cache key: the same tree and the same selector
must produce the same result.

### 4.4 Dependency integrity is not managed

**Evidence.** `pyproject.toml` specifies lower bounds only. There is no
lockfile and no hashes.

**Consequence.** A compromised or simply surprising transitive dependency
executes inside the orchestrator process, with access to the API keys, the
filesystem and the event log.

**First step.** Generate a lockfile with hashes (`pip-compile
--generate-hashes`) and add a CI check that the lockfile is consistent
with `pyproject.toml`.

### 4.5 No static type checking in the toolchain

**Evidence.** The code is annotated and `ruff` is configured, but `mypy` is
not installed or run.

**Consequence.** The type annotations that carry the design's intent are
not verified. Several real bugs in this project (a wrong payload field
name, a duplicated keyword argument) would have been caught by a type
checker.

**First step.** `mypy --strict` on `agents_squad` with a baseline file for
the existing errors, then ratchet the baseline to zero.

---

## 5. The low-severity gaps

### 5.1 The scheduler's ready scan is O(V + E) per tick

`agents/scheduler/engine.py` recomputes the ready set on every dispatch
decision. At realistic graph sizes (tens to hundreds of tasks) this is
negligible next to the subprocess work a dispatch triggers, and the
simplicity is worth more than the asymptotics: a priority queue would make
the "what is running and why" question harder to answer, which is the
question this system exists to answer.

**Revisit when** a graph exceeds a few hundred tasks.

### 5.2 `ScopeOverlap` detection is O(V² · F)

`agents/dag/graph.py:scope_overlaps` compares every pair of tasks. A
path-to-tasks index would make it O(total files), but the current form is
clearer and the graph sizes are small. **Revisit with the scheduler.**

### 5.3 Delegation is bounded but not implemented

`TaskSpec.delegation_depth` is validated against
`SchedulerConfig.max_delegation_depth` and cycles are rejected, but no
runtime path lets an agent spawn a sub-agent. The bound is a guard for a
future feature, not a description of a current one. This is stated in
`docs/limitations.md` §2.

### 5.4 No counterfactual branching from a checkpoint

The checkpoint machinery stores the event sequence and the replay can be
limited to a checkpoint, but there is no "re-run from here with a different
scheduler configuration" facility. The pieces exist: `run-config.json`
records the configuration, `resume_run` restarts the scheduler, and
`StrategySelector` accepts an override.

### 5.5 The dashboard's timeline is derived, not persisted

The Gantt view reconstructs bars from `agent.started` / `agent.finished`
events. That works, but a persisted timeline would be cheaper and would
survive a change to the event schema.

---

## 6. What was audited and found sound

For balance, these were checked and did **not** turn up a problem:

| Area | Finding |
|---|---|
| DAG validation | Cycles, dangling dependencies, self-dependencies and depth violations are all rejected before execution. |
| The attempt fence | Enforced in one place, inside the writing transaction. Exercised by a dedicated concurrency test. |
| The transaction boundary | Event append and projection update are atomic; the projection can be rebuilt. |
| The policy engine | Consulted on every tool invocation; denials are recorded; approval blocks the worker. |
| The integration engine | Detects overlap, staleness and apply conflicts; refuses to guess a resolution. |
| The verification gate | Re-executes criteria independently; advisory criteria are never reported as passed. |
| The provider adapters | Verified against a real HTTP server: request shape, header handling, usage decoding, transient-vs-permanent classification. |
| The message protocol | Eleven of the eleven kinds are produced by a real run; the payload schemas reject malformed input. |
| The role isolation | The reviewer's tool set contains no write tool, and the capability layer denies writes independently. |
| The configuration | Every knob has a test proving it changes behaviour. |
| The artifact store | Content-addressed, integrity-checked, and invalidated when the repository moves on. |
| The sandbox | The wall-clock timeout genuinely fires and kills the process group; a planted binary in the worktree is refused. |

---

## 7. What the local-model experiment changed

Running the orchestrator against eight **real** local models (`docs/local-benchmark-methodology.md`)
surfaced three defects that the mock provider could never have found, because
the mock never produces malformed output:

| Defect | What it cost | Fix |
|---|---|---|
| A model wrote a tool call as **JSON text** instead of using the structured channel | The tool loop could not start | A strict, counted salvage path in the OpenAI-compatible adapter: only a tool that was actually offered, with object arguments |
| A model **repeated an action it had already completed** and looped until the iteration limit | A run that had already produced the correct file was recorded as a failure after 119 s | A repeat guard: the repeated call is not executed, and after a bounded number of repeats the attempt ends successfully because the work is done (119 s → 4.7 s) |
| A model asked for a **`git commit` it did not need** | The approval gate correctly refused it and the run **blocked for 289 s** waiting for a human | The agent prompt now states that committing, pushing and deleting need a human; the benchmark also caps the approval wait so a blocked run fails fast instead of stalling |

And three models produced **plans that failed schema validation** — duplicate
criterion ids, a criterion with no selector. The planner now disambiguates
duplicate ids and records an *unexpressible* criterion as an unverifiable
requirement that the verification gate refuses to pass over.

The general lesson, and the one worth keeping: **a deterministic double
exercises the paths it was written to reach.** Every one of these defects
lived past the point where the mock's fidelity stopped.

---

## 8. Recommended order of work

If the next effort is limited, this order maximises the value of the
research contribution:

1. **Adversarial fixtures** (§2.2) — unblocks the completion-rate half of
   the question, and needs no external resources.
2. **A latency provider** (§2.1) — unblocks the latency half, and the
   adapter already exists.
3. **Predict-versus-observe calibration** (§3.1) — makes the selector's
   explanations trustworthy rather than merely present.
4. **Replanning** (§3.2) — the largest missing *capability* rather than
   the largest missing *evidence*.
5. **Event log compaction** (§4.1) — needed before the system is run at
   scale.
6. **Lockfile and type checking** (§4.4, §4.5) — hygiene, but cheap and
   they prevent a whole class of bug.

Items 1 and 2 together would let the project answer its own research
question for the first time.
