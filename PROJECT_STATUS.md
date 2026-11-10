# Project status

**Codename:** `agent-squad`
**Version:** 0.1.0
**Last updated:** 2026-11-10

---

## Summary

A working multi-agent orchestration runtime for software engineering. A
repository engineering request is turned into a validated task DAG, executed
by specialised agents in isolated git worktrees, verified independently of
the agents' own claims, integrated under a real conflict-detection
boundary, and recorded in an append-only event log that supports replay,
recovery and benchmarking.

The system runs end to end with no paid model API access, using a
deterministic mock provider that emits genuine tool calls.

**Current state: all 159 tests pass. The end-to-end path produces real
patches, real verification and a successful integration.**

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 | Architecture and specifications | **Complete** |
| 1 | Minimal working orchestrator | **Complete** |
| 2 | Real multi-agent coordination | **Complete** |
| 3 | Robust integration and evaluation | **Complete** |
| 4 | Durable runtime | **Complete** |
| 5 | Adaptive orchestration | **Complete** (heuristics; not calibrated) |
| 6 | Observability and debugging | **Partial** — event log complete, OTel partial, replay complete |
| 7 | Security and chaos engineering | **Partial** — capability layer complete, chaos harness partial |
| 8 | Portfolio-quality productisation | **Partial** — API complete, dashboard complete, docs complete |

---

## What is implemented and verified

### Core runtime
- **Task specification** (`models/task.py`) with a strictly enforced lifecycle
  table; every transition goes through `assert_transition`.
- **Task DAG** (`dag/graph.py`): cycle detection, dangling-dependency
  detection, delegation-depth bound, topological ordering, dependency
  levels, critical path, parallel width, scope-overlap detection.
- **Repository inspection** (`planning/inspector.py`): read-only, no
  symlink following, injection scanning of file contents.
- **Two planners** (`planning/planner.py`): deterministic rule-based and
  LLM-backed, with identical output contracts and hard validation of model
  output.
- **Async DAG scheduler** (`scheduler/engine.py`): ready queue, bounded
  concurrency, dependency ordering, retries with deterministic backoff,
  deadlines, cancellation propagation, blocked-dependent propagation,
  attempt fencing, stacked worktrees for dependent tasks.
- **Isolated workers** (`agents/runner.py`, `workspace/worktree.py`): one
  detached git worktree per task, patch capture as a content-addressed
  artifact, worktree disposal after verification.
- **Bounded agent loop** (`agents/base.py`): iteration, turn-token and
  task-budget limits, each producing a distinct explicit outcome.
- **Typed inter-agent protocol** (`models/message.py`, `agents/messaging.py`):
  eleven message kinds with per-kind payload validation, unknown-recipient
  rejection, stale-version rejection, duplicate rejection, and a strict
  separation between informational and state-effecting messages.
- **Capability-based authorisation** (`security/capabilities.py`,
  `security/policy.py`): on the execution path of every tool invocation;
  tool allow-lists, path scoping, excluded-path enforcement, protected-path
  approval gating, audit records, secret redaction in the audit log.
- **Human-in-the-loop approvals** (`agents/approval.py`): durable request
  records, message publication, blocking wait, timeout handling, CLI
  decision command, API decision endpoint.
- **Sandbox** (`sandbox/limits.py`): stripped environment, `RLIMIT_*`
  limits, wall-clock timeout with guaranteed kill, output cap, executable
  allow-list, refusal to execute binaries planted inside the worktree,
  explicit reporting of whether network isolation was achieved.
- **Prompt-injection defences** (`security/injection.py`): pattern
  detection, untrusted-content fencing that resists delimiter injection,
  secret redaction (including Anthropic-style keys).
- **Git-native integration** (`integration/engine.py`): dependency-ordered
  application, tree-identity staleness detection, pre-application overlap
  detection, `git apply --3way`, conflict escalation, integration test
  execution, reviewable patch bundles that commit and push nothing.
- **Evidence-driven verification** (`verification/`): independent
  re-execution of acceptance criteria, eight deterministic check kinds,
  advisory-only criteria never reported as passed, six-level verification
  ladder.
- **Durable state** (`store/`): SQLite in WAL mode, append-only event log,
  materialised projections, transactional event+projection writes,
  projection rebuild from the log.
- **Recovery** (`recovery.py`): projection repair, interrupted-task
  classification, lease awareness, attempt-budget awareness, idempotent
  recovery, `resume_run` that continues from the durable record, and
  idempotency-key reporting for unresolved side effects.
- **Provider abstraction** (`providers/`): OpenAI-compatible (covering
  remote APIs, vLLM, llama.cpp, Ollama, LM Studio), Anthropic, and a
  deterministic mock; normalised messages, tool calls, usage and errors;
  raw provider payload preserved; configurable pricing; reported-vs-estimated
  usage distinguished; transient-vs-permanent error classification;
  provider fallback routing.
- **Budgets** (`providers/budget.py`): per-task, per-run and per-provider
  token and cost ceilings, checked before a call and booked after it.
- **Replay** (`replay/`): model exchange recording as artifacts, a provider
  that serves the recorded transcript without network calls, state replay
  from the event log, and control-plane replay that compares the replayed
  outcome sequence against the recorded one.
- **Benchmark laboratory** (`benchmark/`): eight scenarios spanning
  parallel, sequential, conflicting, failing and single-agent-preferred
  work; four strategies; metrics derived from each run's own event log;
  variance reporting; comparisons that include negative results.
- **Observability** (`observe/`): structured logger, span recorder with
  critical-path extraction, optional OpenTelemetry export, a full metric
  model.
- **API** (`api/server.py`): FastAPI service exposing runs, tasks, the DAG
  for visualisation, events, artifacts, messages, approvals and cancellation.
- **CLI** (`cli/main.py`): `run`, `plan`, `status`, `inspect`, `approve`,
  `cancel`, `resume`, `replay`, `benchmark`, with `--json` throughout.
- **Dashboard** (`dashboard/`): React + TypeScript + React Flow, reading
  live backend state.

---

## Test coverage

268 tests across seven suites, all passing in ~31 s:

| Suite | Tests | What it covers |
|---|---:|---|
| `tests/unit` | 104 | DAG validation, lifecycle table, policy decisions, budget accounting, evidence schema, message protocol, strategy selection, artifact provenance and invalidation |
| `tests/integration` | 78 | The full end-to-end run, budget enforcement, the live FastAPI surface, the dashboard/API contract, provider adapters against a real HTTP server, the message protocol, observability spans, role isolation, configuration wiring, concurrency limits |
| `tests/concurrency` | 8 | Parallel execution overlap, dependency ordering, attempt fencing, cancellation, duplicate and stale messages |
| `tests/chaos` | 11 | Transient and permanent provider failure, worker crash, timeout, malformed model output, stale ownership, duplicate message, conflicting patches, cancellation |
| `tests/recovery` | 12 | Projection rebuild, interrupted-task recovery, lease awareness, idempotency, side-effect reporting, killed-process recovery, resume |
| `tests/security` | 24 | Sandbox limits, path traversal, symlink escape, planted binaries, injection detection, secret redaction, policy gating, approval blocking |
| `tests/evaluation` | 14 | Benchmark reproducibility, metric integrity, variance reporting, negative-result representation |

Run them with:

```bash
pytest                    # everything
pytest -m "not slow"      # skip the slow benchmark scenarios
```

The suite is hermetic: no network access, no paid model API, and no reliance
on wall-clock timing for correctness. The concurrency tests assert on the
*interleaving of recorded events*, not on elapsed milliseconds.

---

## Measured results

See [`docs/benchmark-results.md`](docs/benchmark-results.md). Summary of the
headline findings, all from `benchmarks/results/suite-full-3trials.json`
(96 runs, 8 scenarios, 4 strategies, 3 trials):

- **No strategy improved the verified completion rate** — all four achieved
  1.0000 ± 0.0000. This is a negative result for the multi-agent hypothesis
  in this experiment.
- **Multi-agent execution was marginally slower, not faster**: `static_two`
  +2.8 %, `static_four` +8.9 %, `adaptive` −0.4 % against the baseline.
  Every difference is within one standard deviation.
- **The coordination cost was concentrated in the tightly coupled
  scenario**, where fixed fan-out was 36 % slower than the baseline — the
  behaviour the theory predicts.
- **Integration conflicts were detected in every run of the conflicting
  scenario**, even though both workers passed their own criteria
  independently. This is the central claim of the verification design,
  confirmed empirically.
- **The failing worker was retried and the work still completed**, at a
  verified completion rate of 1.0.

The harness **cannot** demonstrate a model-latency speed-up, because the
deterministic provider has no latency. This is stated in the methodology
and in the results rather than glossed over.

---

## Known gaps and blockers

The full, evidence-backed analysis is in
[`docs/improvement-audit.md`](docs/improvement-audit.md). Ordered by how much
they limit the project's claims.

1. **No latency-bearing provider.** The speed-up half of the research
   question is untested. *Blocker: requires a paid API or a local model.*
   The `ModelProvider` protocol already abstracts this; the missing piece is
   a latency-injecting wrapper, and the orchestrator needs no changes.
2. **No adversarial task fixtures.** Every fixture is designed to succeed,
   so the verified completion rate cannot discriminate between strategies.
   The discriminating experiment is the one where the single-agent strategy
   wins and the multi-agent strategy conflicts.
3. **The strategy selector is uncalibrated.** Its thresholds were chosen by
   hand. The benchmark publishes predictions and observations side by side,
   but nothing feeds the comparison back. The published run shows a
   predicted 22.5 % improvement that was **not** observed.
4. **No replanning during execution.** `EventType.PLAN_REVISED` is read by
   the replayer but never emitted; the plan is produced once, before the
   scheduler starts.
5. **The event log grows without bound.** There is no compaction or
   retention policy, and `rebuild_projection` replays the whole log.
6. **Message validation state is not durable.** `MessageBus._seen_ids` and
   `_versions` are in-memory, so a message can be re-delivered after a
   restart. The attempt fence still protects correctness; only the record
   can duplicate.
7. **The verification gate re-runs the whole suite per task**, which on a
   real repository would dominate the wall-clock time.
8. **The chaos harness covers nine of the eleven catalogue faults.** Message
   loss and stalled-worker detection are declared but have no dedicated test.
9. **No lockfile or type checking.** `pyproject.toml` specifies lower
   bounds only, and `mypy` is not run — several bugs found during
   development would have been caught by a type checker.
10. **No multi-user or authentication model.** Anyone with filesystem access
    to the data directory can approve a protected action.
11. **Not distributed.** One process, one host, one SQLite file; no
    cross-process run-ownership lock.

---

## Next steps

### Immediate (highest value)
1. Add adversarial fixtures where a plausible decomposition yields a
   plausible but incorrect result, so the verified completion rate can
   actually discriminate. This needs no external resources and unblocks the
   completion-rate half of the research question.
2. Add a latency-injecting provider wrapper and re-run the benchmark. This
   is the only way to answer the latency half.
3. Record the selector's predicted speed-up next to the observed one, and
   fit the coordination constants to the difference.

### Near term
4. Implement bounded replanning: after a verification failure, allow the
   supervisor to revise the DAG subject to the existing delegation-depth
   bound, and emit `PLAN_REVISED`.
5. Add event-log compaction and a retention policy, so recovery time stops
   growing with the run's history.
6. Make the message validation state durable by moving the seen-id and
   version columns into the `messages` table.
7. Cache pytest results per (tree, selector) pair so verification does not
   re-run the whole suite for every task.
8. Generate a lockfile with hashes and add `mypy` to the toolchain.

### Later
9. A SWE-bench-lite subset, once the evaluation harness is stable.
10. Distributed execution, if the local runtime's limits are actually
    reached. The design deliberately stops at a single host today.
11. Counterfactual branching from a checkpoint with an alternative
    scheduling policy — the machinery is mostly in place.

---

## Repository map

```
src/agent_squad/       ~14,100 lines  the runtime (45 modules)
tests/                 ~2,900 lines   188 tests
docs/                  14 files       architecture, 6 ADRs, threat model, reliability,
                                     methodology, results, limitations, walkthrough,
                                     failure analysis
dashboard/             React app      optional web dashboard
benchmarks/results/    1 file         raw benchmark output
configs/               2 files        TOML configuration examples
examples/              3 files        example task specifications
```

---

## Definition of done: status

| Requirement | Status |
|---|---|
| A request becomes a validated task DAG | Done |
| The scheduler executes independent tasks concurrently | Done |
| Dependent tasks wait for their prerequisites | Done |
| Workers operate in separate contexts and worktrees | Done |
| Outputs are persisted as structured artifacts | Done |
| Integration checks detect incompatible changes | Done, and demonstrated empirically |
| Verification does not rely on agent assertions | Done — the gate re-executes |
| Failed workers do not silently corrupt state | Done — typed failures, fenced transitions |
| Human approval is enforced for protected actions | Done — the broker blocks the worker |
| Single-agent and multi-agent strategies can be compared | Done |
| Tests demonstrate correctness of the execution semantics | Done — 159 tests |
| Recoverable execution after process interruption | Done — `resume_run` |
| Recorded and inspectable scheduler decisions | Done — `run.strategy_selected` with rationale |
| Replay support | Done — state replay and control-plane replay |
| Configurable budget and concurrency policies | Done |
| Adversarial and fault-injection tests | Partial — nine chaos tests cover eight catalogue faults; message loss and stalled-worker detection remain |
| Reproducible benchmark results | Done — 96 runs recorded |
| Documented measured limitations | Done — `docs/limitations.md`, `docs/benchmark-results.md` |
| A polished end-to-end demonstration | Done — `README.md` quick start |
