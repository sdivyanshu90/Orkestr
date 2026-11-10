# Limitations

This document lists what `agent-squad` does **not** do, and what its results
do **not** establish. It is deliberately long, because a system that
presents a long feature list and a short limitations list is a system whose
limitations have not been found yet.

---

## 1. Verification limits

### Verified does not mean correct

The verification gate re-executes the acceptance criteria that were
*specified*. If the specification is wrong, the verification is faithful and
wrong. The gate proves: "these commands were run, in this workspace, and
they produced these results." It does not prove: "the software is correct",
"the change is complete", or "the requirement was met".

### Acceptance criteria are only as good as the plan

The chain of trust runs: request → plan → acceptance criteria → checks →
verdict. A planner that writes weak criteria produces a run that verifies
weakly and reports success. The runtime flags this in one case — a task with
no deterministic criterion produces a plan warning and an advisory-only
evidence package — but it cannot detect a criterion that is deterministic
but wrong.

### There is no independent oracle

The orchestrator re-runs the *same* test suite the worker's changes are
supposed to satisfy. If the worker wrote both the code and the test, and the
test is trivially satisfiable, the gate will pass it. The system records the
patch, the changed files and the check records so that a reviewer can see
this, but it does not prevent it.

### No property-based or differential testing is generated automatically

The `CheckKind` vocabulary supports file, command and pytest-selector
checks. It does not synthesise property tests from the specification. The
`hypothesis` library is not currently a dependency.

---

## 2. Orchestration and scheduling limits

### The scheduler is local-first, single-process

There is one SQLite writer and one asyncio event loop. There is no
distributed queue, no leader election, no cross-node coordination. The
architecture deliberately stops here (see ADR 0003), but it means:

- throughput is bounded by one host;
- a host failure loses in-flight work (the durable record survives, the
  running worker does not);
- there is no horizontal scaling story.

### Worker affinity to a machine is not modelled

Tasks are not placed on particular machines, and there is no notion of
"this task needs the machine with the GPU". The worker pool is a semaphore.

### Replanning is limited

The plan is produced once, before execution. The runtime supports *bounded*
replanning in the sense that a failed task can be retried and that
dependencies can be re-evaluated, but there is no mechanism that
re-decomposes the task graph in response to what execution revealed. The
`plan.revised` event type exists and the `LLMPlanner` can be invoked again,
but nothing calls it automatically.

### Delegation depth is bounded but delegation is not implemented

`TaskSpec.delegation_depth` is validated against a configured maximum
(default 2) and cycles are rejected, but there is no runtime path in which
an agent spawns a sub-agent. The bound is a guard for a future feature, not
a description of a current one.

### The strategy selector is a heuristic, not a learned model

The selector is deterministic and explainable, and its decisions are
recorded. It is *not* calibrated against observed outcomes. The benchmark
reports the predictions alongside the measurements, but nothing currently
feeds the measurements back into the thresholds. The thresholds in
`StrategyConfig` were chosen by hand.

### Critical-path estimation uses declared durations

`TaskSpec.estimated_duration_s` is set by the planner. For the rule-based
planner it comes from the request; for the LLM planner it is the model's
estimate. Neither is calibrated against the actual execution time, so the
predicted critical path is only as good as the estimate.

---

## 3. Model and provider limits

### The mock provider is not a model

`MockProvider` is a deterministic policy that emits tool calls from a
structured task brief. It cannot reason, cannot discover that the task is
impossible, and cannot produce a wrong implementation that still satisfies a
criterion. The benchmark therefore measures the *orchestration* around a
model, not the model.

### Token estimates are estimates

The mock provider's usage is estimated from character counts
(`providers/base.py:estimate_tokens`). Usage records carry an `estimated`
flag, and the benchmark reports the reported/estimated ratio, but any
absolute token figure from the mock path is approximate.

### Context-window budgeting is approximate

`AgentLoop._enforce_context_budget` trims turns by an estimated token count
and keeps the system prompt and the task brief. It does not implement
provider-specific context-window sizes, and the trimming is not
provider-aware (a provider with a very small window would need a different
strategy).

### No streaming, no batching, no prompt caching

The provider interface is request/response. There is no streaming, no
request batching, and no cache-control support. Cached tokens are
*accounted* for where the provider reports them, but the runtime never
requests caching.

### Provider fallback is coarse

`ProviderRouter` falls back to the next provider only for
`TransientModelError`. A provider that returns malformed output does not
trigger a fallback, because the failure is a content problem, not an
availability problem. There is no circuit breaker.

---

## 4. Security limits

The full analysis is in [`security-threat-model.md`](security-threat-model.md).
The headline items:

- **A git worktree is not a security boundary.** It isolates code, not
  authority. A process inside a worktree can read anything the host user can
  read.
- **The sandbox is defence in depth, not a sandbox in the strong sense.**
  On most hosts `unshare --net` is not permitted, so outbound network
  access is **not** prevented — the runtime records this fact in every
  command result rather than pretending otherwise. There is no seccomp
  filter.
- **The executable allow-list is not a barrier.** `python3`, `sh` and `bash`
  are on it, so any code an agent can write can be executed.
- **The audit log is not tamper-evident.** It is an append-only SQLite
  table, but it is not cryptographically signed, so an attacker with write
  access to the database can alter history.
- **Injection detection is a signal, not a barrier.** The patterns in
  `security/injection.py` catch known phrasings and will miss determined
  attackers. The barrier is the capability layer, and the capability layer
  only defends what it gates.
- **The `git` executable is on the allow-list**, so a hostile repository can
  execute git hooks or git configuration. Worktrees are created from the
  repository under test.
- **Secret redaction is pattern-based** and will miss secrets in unusual
  formats or formats it does not recognise.

---

## 5. Durability and recovery limits

- **Recovery is manual.** `agent-squad resume` must be invoked; nothing
  restarts a run automatically after a crash.
- **Recovery does not undo side effects.** An interrupted attempt may have
  already executed a command. The runtime records the interrupted attempt
  and its idempotency key so the operator can decide, but it cannot roll
  anything back.
- **Exactly-once is claimed only for task state transitions.** Arbitrary
  external effects (a command that writes to a database, an HTTP request)
  are at-least-once.
- **The event log grows without bound.** There is no compaction or
  retention policy yet.
- **There is no cross-process lease enforcement at the database level.** The
  lease is a timestamp in the task record; a second process starting the
  same run would have to cooperate. `resume_run` is the intended entry
  point, but nothing prevents a second orchestrator from being pointed at
  the same run directory.

---

## 6. Replay limits

- **Replay reconstructs decisions, not work.** The recorded transcript is
  replayed against the control plane; no model thinks again.
- **Replay is only as complete as the recording.** If the model exchanges
  were not recorded, the control-plane replay cannot be attempted and the
  replayer says so.
- **The replayed execution is compared on task *outcomes*, not on the full
  event stream.** A finer-grained equivalence check would be more useful.
- **Branching from a checkpoint with a different policy is not
  implemented.** The checkpoint machinery stores the event sequence and the
  replay can be limited to a checkpoint, but there is no "re-run from here
  with a different scheduler configuration" facility.

---

## 7. Benchmark limits

See [`benchmark-methodology.md`](benchmark-methodology.md) §7 and
[`benchmark-results.md`](benchmark-results.md) §4. In short:

- The provider has no latency, so **no model-latency speed-up can be
  demonstrated**. Wall-clock differences between strategies are near the
  noise floor.
- The fixtures are small and synthetic; there is no SWE-bench subset.
- The mock provider is free, so dollar figures are uninformative.
- Every fixture was designed to succeed, so the verified completion rate had
  no room to move and could not discriminate between strategies.
- Trials are not randomised and all strategies share one host.

---

## 8. Interface limits

### The dashboard is read-mostly and optional

The web dashboard under `dashboard/` reads real backend state. It cannot
start a run with a full task specification from the UI beyond the simple
form, and it does not stream model output.

### The CLI is the primary interface

Everything the API can do, the CLI can do, and a few things it cannot
(for example, inspecting an individual artifact's bytes).

### No multi-user support

There is no authentication, no per-user namespace and no role separation
between operators. Anyone with filesystem access to the data directory can
approve a protected action.

---

## 9. Codebase limits

- The `Observer` layer (structured logs, spans, metrics) is present but only
  partially wired into the runtime. The event log is the primary
  observability surface; OpenTelemetry export is optional and off by default.
- The `ProviderRouter`'s fallback logic has not been tested against a real
  multi-provider outage.
- `AgentSquadError.retryable` is set on the exception classes but the
  scheduler's retry decision is driven primarily by the error *class*, not
  by that flag.
- There is no static type checker in the CI configuration; the code is
  annotated and `ruff` is configured, but mypy is not run.
- Test coverage is broad but not exhaustive. The chaos/fault-injection
  harness described in the roadmap is only partially realised: the
  `failing_worker` scenario and the recovery tests cover the transient
  provider failure and the crash-recovery paths, but worker crashes,
  message loss, rate limiting and cancellation races are not all
  systematically injected.

---

## 10. What would change the conclusions

The most valuable additions, in order:

1. **A provider with realistic latency.** Without it the speed-up question
   cannot be answered at all.
2. **Adversarial fixtures** where a plausible decomposition yields a
   plausible but incorrect result. This is the experiment that would
   discriminate between strategies on verified completion rate.
3. **A calibrated cost model**, so the selector's coordination-overhead
   estimate is grounded in measured duplication rather than a constant.
4. **A real repository corpus**, to check whether the findings hold when
   the tasks are large and the integration risk is non-trivial.
