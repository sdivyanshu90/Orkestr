# Benchmark results

**Status: measured.** Every number below was produced by
`agent-squad benchmark --suite full --trials 3` on the development host and
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
| Wall-clock duration of the suite | 46.0 s |
| Machine | single Linux host, Python 3.14 |

---

## 2. Aggregate results

Mean across 24 runs per strategy (8 scenarios × 3 trials), with the
standard deviation across those runs.

### `single` — baseline, 1 worker

| Metric | Mean | Std. dev. |
|---|---:|---:|
| Verified completion rate | 1.0000 | 0.0000 |
| Wall-clock (s) | 0.4768 | 0.1741 |
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
| Wall-clock (s) | 0.4732 | 0.1845 |
| Tokens used | 2271.1 | 986.7 |
| Cost (USD) | 0.0000 | 0.0000 |
| Tool calls | 2.25 | 1.11 |
| Integration conflicts | 0.25 | 0.68 |
| Retries | 0.125 | 0.000 |

### `static_four` — fixed 4 workers

| Metric | Mean | Std. dev. |
|---|---:|---:|
| Verified completion rate | 1.0000 | 0.0000 |
| Wall-clock (s) | 0.4842 | 0.1833 |
| Tokens used | 2271.9 | 986.8 |
| Cost (USD) | 0.0000 | 0.0000 |
| Tool calls | 2.25 | 1.11 |
| Integration conflicts | 0.25 | 0.68 |
| Retries | 0.125 | 0.000 |

### `adaptive` — worker count chosen by the selector

| Metric | Mean | Std. dev. |
|---|---:|---:|
| Verified completion rate | 1.0000 | 0.0000 |
| Wall-clock (s) | 0.4635 | 0.1766 |
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

### 3.2 The wall-clock differences are indistinguishable from noise

| Comparison | Baseline (`single`) | Candidate | Difference | Within 1 s.d.? |
|---|---:|---:|---:|:---:|
| `static_two` wall-clock | 0.4768 s | 0.4732 s | **−0.8 %** | yes (s.d. ≈ 0.18) |
| `static_four` wall-clock | 0.4768 s | 0.4842 s | **+1.6 %** | yes |
| `adaptive` wall-clock | 0.4768 s | 0.4635 s | **−2.8 %** | yes |

The standard deviation across runs (0.18 s) is roughly **37 % of the mean**
(0.48 s). Every difference between strategies is one to two orders of
magnitude smaller than that spread, and **the sign of the difference is not
stable across strategies** — `static_two` and `adaptive` came out marginally
faster, `static_four` marginally slower. **No strategy is demonstrably faster
or slower than the baseline in this experiment**, and the correct reading of
this table is "no effect was detectable" — not "multi-agent is 1.6 % slower"
and not "multi-agent is 2.8 % faster".

A second, independent observation points the same way: re-running the suite
produces a *different* sign for the same comparison. Wall-clock figures in
this document are only meaningful alongside their standard deviations.

Two of the six comparisons per strategy are reported by the harness as
*unchanged* (the verified completion rate and the tool-call count are
identical), and the harness distinguishes that from *worse* rather than
folding the two together.

### 3.3 Coordination overhead was measurable but tiny

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

### 3.4 Per-scenario wall-clock means (3 trials each)

| Scenario | `single` | `static_two` | `static_four` | `adaptive` |
|---|---:|---:|---:|---:|
| `independent_parallel` | 0.460 | 0.447 | 0.449 | 0.450 |
| `tightly_coupled` | 0.744 | 0.728 | 0.698 | 0.703 |
| `multi_file_refactor` | 0.393 | 0.377 | 0.397 | 0.361 |
| `bug_fix_regression` | 0.645 | 0.714 | 0.747 | 0.668 |
| `integration_conflict` | 0.152 | 0.146 | 0.146 | 0.145 |
| `failing_worker` | 0.424 | 0.441 | 0.452 | 0.441 |
| `single_agent_preferred` | 0.415 | 0.373 | 0.403 | 0.355 |
| `meaningful_parallel` | 0.581 | 0.561 | 0.581 | 0.584 |

Two observations:

- **The scenarios are not distinguished by strategy.** The spread within a
  row (e.g. `meaningful_parallel`: 0.561–0.584, a 4 % range) is the same
  size as the spread across strategies, and both are dwarfed by the
  between-scenario spread (0.145–0.747). The dominant variable is the
  scenario, not the orchestration strategy.
- **`integration_conflict` is much faster than everything else** (0.15 s)
  because the run aborts at the integration boundary instead of running the
  full test suite. Time is not a proxy for success — a run that fails
  early can be the fastest run in the suite.

Two earlier revisions of this report claimed effects that the current
measurement does not support: a 36 % penalty for fixed fan-out on the
tightly coupled scenario, and a consistent "multi-agent is slower" direction.
The first came from a run made while the integration engine still
mis-classified parallel patches as stale, which short-circuited those runs.
The second came from a run whose per-strategy ordering happened to be
consistent; re-running the suite changes the sign. Both were withdrawn, and
the withdrawal is recorded here rather than quietly edited away — a benchmark
whose numbers only ever come out one way is not reporting, it is asserting.

### 3.5 Integration conflicts are detected, and they are not free

The `integration_conflict` scenario produced **1 conflict per run** under
every strategy, and the integration boundary **failed** in every case: both
workers' patches individually satisfied their own acceptance criteria, and
the combination could not be applied.

This is the central claim of the verification design, and it is confirmed
empirically: *patches that individually pass can fail together*, and a
system that only trusted per-worker summaries would have reported success.

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
   experiment it did not, and the difference was not even measurable.
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
| 1 (`tightly_coupled`, `bug_fix_regression`) | none | none measurable |
| 2 (`independent_parallel`) | 2× | none measurable |
| 4 (`meaningful_parallel`) | 4× | none measurable |

The theoretical speed-up is real; the *realised* speed-up is not
observable, because the critical path in this benchmark is dominated by
non-model work (git worktree creation, subprocess startup, test execution).
The honest statement is: **the DAG parallelism was available but the
workload offered nothing to overlap.**

For reference, the selector's own predictions for the
`independent_parallel` scenario were:

```
serial execution estimated at 90.0s; best parallel option uses 2 worker(s) at ~69.8s
parallelism improves estimated wall-clock time by 22.5%
```

The prediction of a 22.5 % improvement was **not** observed: the measured
differences are under 3 %, and the sign is not stable across strategies or
across re-runs of the suite. The predicted speed-up is derived from the
*estimated* task durations (60 s each, supplied by the plan), while the real
work took well under a second; the model is therefore predicting an
improvement in a quantity that the mock provider makes vacuous.

This is recorded here rather than quietly omitted. The selector's speed-up
estimate has not been validated against reality, and until a latency-bearing
provider exists it cannot be. It is the second item on the list in
[`docs/improvement-audit.md`](improvement-audit.md).

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

<!-- local-model-metrics:start -->
## Local models on RTX 3050 and CPU

Measured on 2026-11-10: eight Q4_K_M checkpoints, 62 completed agent-workflow trials and 2 user-requested cancellations, 40 fixed-score GPU requests, and five CPU-baseline requests. Hardware: RTX 3050 Laptop (4 GiB VRAM), six CPU threads and 8.7 GiB RAM. Serving: CUDA llama.cpp.

| Checkpoint | Runtime gate / completed | Fixed GPU tasks / 5 | Mean GPU response s |
|---|---:|---:|---:|
| `qwen2.5-coder-1.5b-instruct` | 4 / 8 | 2 / 5 | 0.310 |
| `qwen3-1.7b` | 1 / 8 | 4 / 5 | 0.694 |
| `deepseek-r1-distill-qwen-1.5b` | 2 / 8 | 1 / 5 | 2.155 |
| `gemma-3-1b-it` | 2 / 8 | 2 / 5 | 0.301 |
| `llama-3.2-1b-instruct` | 0 / 8 | 3 / 5 | 0.228 |
| `phi-4-mini-instruct` | 7 / 8 | 2 / 5 | 0.481 |
| `minimax-m1-distill-qwen3-4b` | 0 / 8 | 2 / 5 | 3.640 |
| `kimi-k3-distill-qwen3.5-9b` | 0 / 6 | 4 / 5 | 33.841 |

Qwen2.5-Coder CPU baseline: **2 / 5** fixed tasks passed, **1.770 s** mean response time. Direct requests use one foreground configuration, one request slot, 4096 context tokens, temperature zero, seed 42, and a 256-token output cap after an untimed warmup.

**Interpretation:** runtime-gate flags use model-generated plans and criteria, so they do not establish the correctness of the original requested code. User-requested cancellations are excluded from the denominators. 11 runtime passes recorded no worker tool calls. Fixed tasks use independent checks, but five tiny tasks with one trial each do not establish general model rankings. Response latencies include differing output lengths and possible contention from a residual server found and stopped during final cleanup; they are not fully isolated hardware comparisons. MiniMax and Kimi entries are third-party Qwen-based distillations, not official vendor checkpoints.

Detailed metrics: [agent-workflow results](local-benchmark-results.md), [fixed-score inference and CPU results](local-inference-results.md), and [local methodology](local-benchmark-methodology.md). Raw JSON, per-request responses, hashes, server logs and telemetry are under `benchmarks/results/`.

**Cleanup verified:** 19,011,685,088 bytes of benchmark-owned model caches and inference-runtime downloads were removed. [Deletion receipt](../benchmarks/results/local-cleanup.json).
<!-- local-model-metrics:end -->
