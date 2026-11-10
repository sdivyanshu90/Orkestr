# Benchmark methodology

This document describes how the orchestration efficiency laboratory is
designed, what it measures, and — just as importantly — what it **cannot**
show. A benchmark that cannot produce an unfavourable result is not a
benchmark.

---

## 1. The research question

> Under which conditions can adaptive multi-agent orchestration improve
> verified task completion, latency, or cost efficiency compared with a
> well-engineered single-agent system?

Three sub-questions follow from it:

1. **Completion.** Does splitting a task set across N agents change the rate
   at which work is *independently verified* as complete?
2. **Latency.** Does it change end-to-end wall-clock time, and how does that
   relate to the theoretical parallelism of the DAG?
3. **Cost.** Does it change token consumption, tool invocations and monetary
   cost per verified task?

The system must also be able to demonstrate the *opposite*: that multi-agent
execution is slower, more expensive, or less reliable than the baseline.

---

## 2. What is held constant

| Variable | How it is controlled |
|---|---|
| Task definition | Every strategy executes the **same** task spec, byte for byte |
| Repository | Each trial gets a freshly created, freshly committed repository |
| Model | The deterministic mock provider, unless a scenario installs a fault |
| Tool set | Identical per role across strategies |
| Budgets | Identical configuration; no strategy gets a larger allowance |
| Environment | Same host, same interpreter, sequential trials |

The mock provider is the key methodological choice. It is not a stub: it
emits genuine tool calls that a worker executes, so the tasks produce real
file changes, real diffs, real test runs and real conflicts. What it does not
do is *think*, which means the comparison isolates orchestration behaviour
from model variance. This has a major consequence, discussed in §7.

---

## 3. Strategies

| Strategy | Workers | Description |
|---|---|---|
| `single` | 1 | The baseline. One worker; the scheduler runs tasks one at a time. |
| `static_two` | 2 | A fixed two-worker fan-out, regardless of the DAG's shape. |
| `static_four` | 4 | A fixed four-worker fan-out, regardless of the DAG's shape. |
| `adaptive` | decided | The deterministic selector chooses the worker count from the graph. |

The adaptive strategy's decision is recorded with its rationale
(`run.strategy_selected` in the event log), so a reader can check whether
the reasoning was sensible in hindsight.

---

## 4. Scenarios

Eight scenarios span the space the research question cares about.

| Scenario | Shape | What it probes |
|---|---|---|
| `independent_parallel` | 2 independent tasks, disjoint files | The ideal fan-out case |
| `tightly_coupled` | 2 tasks, second depends on first | Whether parallelism is wasted on a chain |
| `multi_file_refactor` | 1 task touching 4 files | Whether decomposition would help or hurt |
| `bug_fix_regression` | Fix then regression test | Dependency-ordered verification |
| `integration_conflict` | 2 tasks editing the same file | The price of parallel edits |
| `failing_worker` | 1 task, provider fails once | Retry and recovery behaviour |
| `single_agent_preferred` | 1 small task | Whether the selector correctly declines to parallelise |
| `meaningful_parallel` | 4 independent tasks | Wide fan-out |

Every scenario is defined in `src/agent_squad/benchmark/tasks.py` and uses
the deterministic fixtures in `src/agent_squad/benchmark/repo_fixture.py`.

---

## 5. Metrics

All metrics are derived from the run's **own event log**, not from the
harness's assumptions about what happened. `src/agent_squad/benchmark/harness.py`
contains no hard-coded expectations about a scenario's outcome.

### Primary

| Metric | Definition |
|---|---|
| Verified completion rate | Tasks whose acceptance criteria were independently re-executed and passed, divided by the total number of tasks |
| Wall-clock time | End to end, from `run.created` to `run.completed` |
| Tokens used | Sum of reported usage across all model calls |
| Cost | Computed from configured per-token prices |
| Tool calls | Number of tool invocations recorded in the log |

### Secondary

| Metric | Definition |
|---|---|
| Integration conflicts | Conflicts detected at the integration boundary |
| Patches applied | Patches successfully combined |
| Retries | Attempts beyond the first |
| Verification failures | Gate runs that rejected the work |
| Policy denials | Tool calls refused by the capability layer |
| Human interventions | Approval requests raised |
| Coordination tokens | Estimated tokens carried by inter-agent messages |

### Reported vs. estimated usage

`Usage.estimated` distinguishes tokens the provider actually reported from
tokens the runtime estimated. The harness reports the ratio
(`reported_usage_ratio`). The mock provider's usage is always an estimate,
and this is stated rather than hidden.

---

## 6. Trials and variance

- Each (scenario, strategy) pair is run `N` times (default 1; the published
  results use 3).
- The harness reports the **mean** and the **standard deviation** across
  trials.
- A single trial cannot support a variance claim. The harness emits an
  explicit note when `trials < 2` saying so.
- The published results include the standard deviation for every metric, and
  the interpretation section compares differences against that spread rather
  than against the means alone.

---

## 7. What this harness cannot show

This is the most important section of this document.

**The mock provider has no latency.** Every model call returns immediately.
Real agents spend seconds to minutes per model call, and that is where
parallelism pays: the critical path shrinks while the fixed costs (git
worktree creation, subprocess startup, test execution) stay constant. With a
zero-latency provider, the fixed costs *dominate*, so the measured
wall-clock differences between strategies are mostly noise.

Concretely, this means:

- The harness can **reliably** measure coordination overhead, token cost,
  integration conflicts, retry behaviour and verified completion rate.
- The harness **cannot** demonstrate a model-latency speed-up. Doing so
  requires a provider with realistic, non-zero latency, which requires a
  paid API or a local model.

The published results reflect this and say so. A reader should treat the
wall-clock column as a *noise floor*, not as evidence about parallel speed-up.

**The benchmark is not SWE-bench.** The task fixtures are small and
synthetic. They exercise the orchestration machinery faithfully but they do
not represent the difficulty distribution of real software-engineering
issues.

**Cost is zero.** The mock provider has no price. Cost metrics are therefore
structurally present but numerically uninformative; the *token* metrics
carry the signal.

---

## 8. Reproducing

```bash
# The full suite, 3 trials
agent-squad benchmark --suite full --trials 3 --json > results.json

# A single scenario
python - <<'PY'
import asyncio, json
from agent_squad.benchmark.harness import run_suite
print(json.dumps(asyncio.run(run_suite(
    suite="focus", trials=3, strategies=["single", "static_two"],
    scenario_ids=["independent_parallel"],
)), indent=2))
PY
```

Published results are stored under `benchmarks/results/`. The harness writes
no files by default; the command above redirects to a file.

---

## 9. Adding a scenario

1. Add a `Scenario` to `src/agent_squad/benchmark/tasks.py` using the
   `_task()` helper. Every task must declare at least one acceptance
   criterion.
2. Optionally install a fault via `provider_factory` — this is how the
   failure paths get exercised rather than merely declared.
3. The scenario is picked up automatically by `build_scenarios()`.

Do **not** encode an expected outcome in the scenario definition. The
harness must be able to observe a strategy losing.

---

## 10. Known methodological weaknesses

- **Trial ordering.** Trials run sequentially in a fixed order; there is no
  randomisation. Thermal or cache effects are not controlled for.
- **Shared host.** All strategies share one machine. A dedicated runner per
  strategy would be better.
- **Single repository shape.** All fixtures are small Python projects. The
  cost of integration conflicts may scale differently in a polyglot
  monorepo.
- **The scheduler's own overhead is included** in wall-clock time, which is
  correct for an end-to-end measurement but means the measurement cannot
  separate "work done" from "orchestration done".
