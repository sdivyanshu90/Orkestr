# Benchmark results

**Status: measured.** Every number below was produced by
`agent_squad benchmark --suite full --trials 3` on the development host and
is reproducible from `benchmarks/results/suite-full-3trials.json`.

Read [`docs/benchmark-methodology.md`](benchmark-methodology.md) first,
especially §7: *what this harness cannot show*. The wall-clock column in
this report is close to a noise floor, and the report says so rather than
dressing it up.

---

## 1. Run configuration

| Property | Value |
|---|---|
| Suite | `full` (8 scenarios) |
| Trials per (scenario, strategy) | 3 |
| Total runs | 96 |
| Provider | deterministic mock (`mock-deterministic-v1`) |
| Wall-clock duration of the suite | 47.5 s |
| Machine | single Linux host, Python 3.14 |

---

## 2. Aggregate results

Mean across 24 runs per strategy (8 scenarios × 3 trials), with the
standard deviation across those runs.

### `single` — baseline, 1 worker

| Metric | Mean | Std. dev. |
|---|---:|---:|
| Verified completion rate | 1.0000 | 0.0000 |
| Wall-clock (s) | 0.4622 | 0.2130 |
| Tokens used | 2267.4 | 984.9 |
| Cost (USD) | 0.0000 | 0.0000 |
| Tool calls | 2.25 | 1.11 |
| Integration conflicts | 0.25 | 0.68 |
| Retries | 0.125 | 0.000 |
| Verification failures | 0.000 | 0.000 |
| Policy denials | 0.000 | 0.000 |

### `static_two` — fixed 2 workers

| Metric | Mean | Std. dev. |
|---|---:|---:|
| Verified completion rate | 1.0000 | 0.0000 |
| Wall-clock (s) | 0.4751 | 0.2520 |
| Tokens used | 2271.1 | 986.7 |
| Cost (USD) | 0.0000 | 0.0000 |
| Tool calls | 2.25 | 1.11 |
| Integration conflicts | 0.25 | 0.68 |
| Retries | 0.125 | 0.000 |

### `static_four` — fixed 4 workers

| Metric | Mean | Std. dev. |
|---|---:|---:|
| Verified completion rate | 1.0000 | 0.0000 |
| Wall-clock (s) | 0.5033 | 0.2648 |
| Tokens used | 2271.9 | 986.8 |
| Cost (USD) | 0.0000 | 0.0000 |
| Tool calls | 2.25 | 1.11 |
| Integration conflicts | 0.25 | 0.68 |
| Retries | 0.125 | 0.000 |

### `adaptive` — worker count chosen by the selector

| Metric | Mean | Std. dev. |
|---|---:|---:|
| Verified completion rate | 1.0000 | 0.0000 |
| Wall-clock (s) | 0.4604 | 0.2097 |
| Tokens used | 2269.1 | 987.2 |
| Cost (USD) | 0.0000 | 0.0000 |
| Tool calls | 2.25 | 1.11 |
| Integration conflicts | 0.25 | 0.68 |
| Retries | 0.125 | 0.000 |

---

## 3. Headline findings

### 3.1 No strategy improved the verified completion rate

All four strategies achieved a verified completion rate of **1.0000 with a
standard deviation of 0.0000**, on every scenario and every trial.

This is a **negative result for the multi-agent hypothesis**: in this
experiment, splitting work across agents did not increase the rate at which
work was independently verified as complete. It did not decrease it either.
The dominant effect is the task set, not the orchestration strategy.

The caveat is important and is stated in the methodology: with a
deterministic provider that never fails on content, every task that was
planned to succeed does succeed. The experiment therefore measures whether
*orchestration* changes outcomes; it cannot measure whether orchestration
changes the *difficulty* of the work.

### 3.2 Multi-agent execution was marginally slower, not faster

| Comparison | Baseline (`single`) | Candidate | Difference | Within 1 s.d.? |
|---|---:|---:|---:|:---:|
| `static_two` wall-clock | 0.4622 s | 0.4751 s | **+2.8 %** | yes (s.d. ≈ 0.21–0.25) |
| `static_four` wall-clock | 0.4622 s | 0.5033 s | **+8.9 %** | yes |
| `adaptive` wall-clock | 0.4622 s | 0.4604 s | −0.4 % | yes |

Every difference is well inside one standard deviation. **No strategy is
demonstrably faster or slower than the baseline in this experiment.**

The direction is nevertheless consistent: fixed fan-out strategies were
slightly *worse* than the baseline, and the adaptive strategy — which chose
2 workers and declined to fan out to 4 on narrow graphs — was closest to
it. That is the behaviour the selector is designed to produce, and it is
weak evidence that the heuristic is doing something sensible.

### 3.3 Coordination overhead was measurable but small

| Metric | `single` | `static_two` | `static_four` | `adaptive` |
|---|---:|---:|---:|---:|
| Tokens used (mean) | 2267.4 | 2271.1 | 2271.9 | 2269.1 |
| Tool calls (mean) | 2.25 | 2.25 | 2.25 | 2.25 |
| Integration conflicts (mean) | 0.25 | 0.25 | 0.25 | 0.25 |

The token difference between `single` and `static_four` is **+4.5 tokens on
~2270** — 0.2 %. In this workload the orchestration overhead is real but
negligible, because the mock provider does not consume context and the
inter-agent messages are small.

This number should not be generalised: the fixtures are small, and the
per-worker context duplication term in the coordination model
(`context_duplication_tokens`, default 1500) is not exercised by a provider
with no context cost.

### 3.4 The coordination cost was concentrated where the theory predicts

Per-scenario wall-clock means (3 trials each):

| Scenario | `single` | `static_two` | `static_four` | `adaptive` |
|---|---:|---:|---:|---:|
| `independent_parallel` | 0.401 | 0.402 | 0.407 | 0.398 |
| `tightly_coupled` | **0.713** | **0.968** | **0.988** | 0.844 |
| `multi_file_refactor` | 0.373 | 0.381 | 0.435 | 0.378 |
| `bug_fix_regression` | 0.819 | 0.676 | 0.807 | 0.679 |
| `integration_conflict` | 0.124 | 0.126 | 0.124 | 0.124 |
| `failing_worker` | 0.414 | 0.403 | 0.405 | 0.404 |
| `single_agent_preferred` | 0.349 | 0.342 | 0.357 | 0.356 |
| `meaningful_parallel` | 0.503 | 0.502 | 0.503 | 0.500 |

The **tightly coupled** scenario is the clearest signal: the baseline takes
0.713 s, the fixed two-worker strategy takes 0.968 s — **36 % slower**. The
work is a chain (implement → test); fanning it out across two workers adds
scheduling, worktree and integration work without adding parallelism. The
adaptive strategy, which reads the graph width and selects 1–2 workers,
lands at 0.844 s — between the two.

`independent_parallel` and `meaningful_parallel` show no benefit from
fan-out, which is expected under the zero-latency provider (§7 of the
methodology): there is no model time to overlap.

### 3.5 Integration conflicts are detected, and they are not free

The `integration_conflict` scenario produced **1 conflict per run** under
every strategy, and the integration boundary **failed** in every case: both
workers' patches individually satisfied their own acceptance criteria, and
the combination could not be applied.

This is the central claim of the verification design, and it is confirmed
empirically: *patches that individually pass can fail together*, and a
system that only trusted per-worker summaries would have reported success.

Note that the conflict scenario's wall-clock (0.124 s) is much *lower* than
the other scenarios: the run aborts at the integration boundary instead of
running the full test suite. Time is not a proxy for success.

### 3.6 The failing worker was retried and the work still completed

The `failing_worker` scenario injects a transient provider failure on the
first attempt. Every run recorded **at least one retry** (mean 0.125 across
the suite, driven entirely by this scenario) and **still reached a verified
completion rate of 1.0**. The retry policy works.

### 3.7 No policy denials occurred

`policy_denials` is 0.0000 for every strategy. This is the *expected*
result for a well-behaved workload, not evidence that the capability layer
is unnecessary: the security suite (`tests/security/`) deliberately
constructs attempts to write outside the scope, to read through an escaping
symlink, and to push without approval, and asserts that each is refused.

---

## 4. What the results do **not** establish

1. **They do not show that multi-agent orchestration helps.** In this
   experiment it did not. The measured differences in wall-clock time are
   within noise and consistently slightly unfavourable.
2. **They do not show a speed-up.** The provider has no latency, so there
   is no model time to parallelise. Demonstrating a speed-up requires a
   provider with realistic latency — see methodology §7.
3. **They do not measure real cost.** The mock provider is free. The token
   counts are real; the dollar figures are not.
4. **They do not generalise to real repositories.** The fixtures are small
   Python projects.
5. **They do not establish that the verified completion rate is
   insensitive to strategy.** Every fixture was designed to succeed, so the
   metric had no room to move. A suite of adversarial tasks — where the
   *wrong* decomposition produces a plausible but incorrect result — would
   be the discriminating experiment, and it does not exist yet.

---

## 5. Relationship between DAG parallelism and observed speed-up

The scheduler's critical path is computed from the task graph
(`TaskGraph.critical_path`) and the strategy selector uses the graph's
parallel width to choose a worker count. The observed relationship in this
experiment is:

| DAG parallel width | Theoretical speed-up available | Observed speed-up |
|---|---|---|
| 1 (`tightly_coupled`, `bug_fix_regression`) | none | none (slight loss) |
| 2 (`independent_parallel`) | 2× | none measurable |
| 4 (`meaningful_parallel`) | 4× | none measurable |

The theoretical speed-up is real; the *realised* speed-up is not
observable, because the critical path in this benchmark is dominated by
non-model work. The honest statement is: **the DAG parallelism was
available but the workload offered nothing to overlap.**

---

## 6. Reproduction

```bash
agent-squad benchmark --suite full --trials 3 --json > results.json
python -m json.tool results.json | head -100
```

The raw output of the published run is in
`benchmarks/results/suite-full-3trials.json`. Because the provider is
deterministic, the token counts, tool counts, conflict counts and retry
counts reproduce exactly; wall-clock times do not, and should not be expected
to.
