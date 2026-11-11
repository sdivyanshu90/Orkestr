# Project status

**Codename:** `agent-squad`
**Version:** 0.1.0
**Last updated:** 2026-11-11

---

## The honest headline: the formal score is 0.0, and that is correct

`docs/scoring-criteria.md` is the authoritative policy. §2 requires a
**named independent reviewer** for any criterion to be marked `PASS`, and
states explicitly that "an author reviewing their own output does not
qualify". No such review has happened. Every criterion in
[`docs/assessment-record.yaml`](docs/assessment-record.yaml) is therefore
`INCONCLUSIVE` (implemented and self-evidenced, awaiting independent
reproduction) or `MISSING` (the required artefacts do not exist), and
`raw_score = 0.0`.

That is not a bug in the assessor. It is the policy working as designed:
this repository does not certify itself. The record also publishes an
**engineering readiness** section, clearly labelled as *not* the formal
score, so a reviewer can see which criteria are one reproduction away from
a decision and which are genuinely absent.

Three criteria are **not reachable by any amount of implementation work**:
`S3` needs an implementation-independent audit, `U1` needs 3 external
developers over 30 consecutive days and 100 genuine runs, `U2` extends that
to 90 days and 10 users, and `V4` needs a second independent team on a
separately administered machine.

---

## Summary

A multi-agent orchestration runtime for software engineering. A repository
engineering request becomes a validated task DAG, executed by specialised
agents in isolated git worktrees, verified independently of the agents' own
claims, integrated under a real conflict-detection boundary, and recorded in
an append-only event log that supports replay, recovery and benchmarking.

The distinguishing property is **the acceptance boundary**. A worker's
"done" is not acceptance: the run is judged against an immutable request
contract, re-executed on the final combined tree, and reported as
`accepted`, `rejected`, `inconclusive`, `timed_out` or `cancelled`. A run
that changed nothing is `inconclusive` rather than accepted, and an outcome
that already held before the work is `preserved` only when the run actually
touched what the outcome is about.

The system runs end to end with no paid model API access, using a
deterministic mock provider that emits genuine tool calls.

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
| 7 | Security and chaos engineering | **Partial** — capability layer and hardened containment complete; chaos harness partial |
| 8 | Portfolio-quality productisation | **Partial** — API and dashboard complete, docs complete |
| R0 | Trustworthy success | **Complete** — immutable contract, final-tree judgement, no-op detection |
| R1 | Bounded execution and accounting | **In progress** — deadline phases and request ledger landing |
| R2 | Containment and deterministic recovery | **Complete** — ownership, reconciliation, retained-workspace manifest, signal handling |
| R3 | Real-repository evidence | **Not started** — needs a preregistered corpus |
| R4 | Coordination earns its overhead | **Complete** — bounded replanning |
| R5 | Operable, reproducible release | **In progress** — mypy, lockfile and CI landing |

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

The suite has grown with each roadmap stage. It is organised by what it
proves, not by module:

| Suite | What it proves |
|---|---|
| `tests/unit` | Contract semantics, DAG validation, lifecycle transitions, policy decisions, budget accounting, evidence schema, message protocol, strategy selection, artifact provenance and invalidation |
| `tests/integration` | The full end-to-end run, the hardened containment boundary, budget enforcement, the live FastAPI surface, the dashboard/API contract, provider adapters against a real HTTP server, the message protocol, observability spans, role isolation, configuration wiring, concurrency limits, control-plane replay |
| `tests/concurrency` | Parallel execution overlap, dependency ordering, attempt fencing, cancellation, duplicate and stale messages |
| `tests/chaos` | Transient and permanent provider failure, worker crash, timeout, malformed model output, stale ownership, duplicate message, conflicting patches, cancellation |
| `tests/lifecycle` | The R2 matrix: at least ten integrated cycles per failure category (success, startup failure, failed verification, task timeout, provider failure, user cancellation, SIGINT, SIGTERM, a child ignoring SIGTERM), reconciliation of a SIGKILLed owner, PID reuse, lost registry writes, and retained-workspace manifests |
| `tests/recovery` | Projection rebuild, interrupted-task recovery, lease awareness, idempotency, side-effect reporting, killed-process recovery, resume |
| `tests/security` | Sandbox limits, path traversal, symlink escape, planted binaries, injection detection, secret redaction, policy gating, approval blocking, isolation-profile refusal |
| `tests/evaluation` | The adversarial contract corpus, benchmark reproducibility, metric integrity, variance reporting, negative-result representation |

Run them with:

```bash
pytest                          # everything
pytest -m "not slow"            # skip the lifecycle matrix and heavy scenarios
pytest tests/lifecycle -q       # the R2 lifecycle matrix on its own
```

The suite is hermetic: no network access, no paid model API, and no reliance
on wall-clock timing for correctness. The concurrency tests assert on the
*interleaving of recorded events*, not on elapsed milliseconds; the timing
tests use monotonic bounds with generous margins.

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

1. **No independent review.** This is the single blocker that caps the
   formal score, and it cannot be closed from inside the repository. §2 of
   the policy requires a named reviewer who did not implement the criterion
   and who reproduces its critical result.
2. **No external usage evidence.** `U1` and `U2` require 30–90 consecutive
   days, 3–10 developers outside the implementation team, and 100–1,000
   genuine runs across 3–10 repositories. Synthetic batch repeats do not
   count.
3. **No real-repository corpus.** `V1`–`V3` require at least 60 tasks across
   6 pinned repositories, preregistered and independently reviewed, with 3
   models × 3 trials. The repository has a synthetic fixture corpus and one
   64-trial local sweep.
4. **The historical timing sample carries a caveat.** A residual model
   server was alive during part of the sweep. The results are retained with
   the caveat and are not qualification evidence; the qualification sample
   must be rerun with verified process ownership.
5. **The strategy selector is uncalibrated.** Its thresholds were chosen by
   hand. The published run shows a predicted 22.5 % improvement that was
   **not** observed.
6. **The event log grows without bound.** There is no compaction or
   retention policy, and `rebuild_projection` replays the whole log.
7. **The verification gate re-runs the whole suite per task**, which on a
   real repository would dominate the wall-clock time.
8. **No multi-user or authentication model.** Anyone with filesystem access
   to the data directory can approve a protected action.
9. **Not distributed.** One process, one host, one SQLite file.

---

## Next steps

**Current priority:** close the six blocking defects listed in
`docs/scoring-criteria.md` §6, then hand the release candidate to an
independent reviewer. Implementation status and test counts alone do not
qualify a score.

### Closed since the last status
1. Bounded replanning: a failed verification can produce a revised plan
   under the existing delegation-depth bound, and `PLAN_REVISED` is emitted.
2. Lockfile with hashes and `mypy` on the critical paths.
3. Run ownership: acquisition, heartbeat, fencing and release in every
   run and resume path.
4. A request ledger wired into the provider path.
5. An absolute deadline that covers planning and verification, not only
   scheduler attempts.
6. A hardened execution profile with a real containment boundary.

### Open, and honest
7. **Independent review** — required for any `PASS`; not an implementation
   task.
8. **External usage evidence** — `U1`/`U2` need people and time, not code.
9. **A preregistered real-repository corpus** for `V1`–`V3`.
10. **A qualification timing sample** rerun with verified process
    ownership, replacing the caveated historical sweep.
11. Event-log compaction and a retention policy, so recovery time stops
    growing with the run's history.
12. Caching verification results per (tree, selector) so the gate does not
    re-run the whole suite for every task.
13. Making message validation state durable by moving the seen-id and
    version columns into the `messages` table.
14. A SWE-bench-lite subset, once the evaluation harness is stable.
15. Distributed execution, if the local runtime's limits are actually
    reached. The design deliberately stops at a single host today.

---

## Repository map

```
src/agent_squad/       ~14,100 lines  the runtime (45 modules)
tests/                 ~9,000 lines   unit, integration, lifecycle, chaos, recovery, security, evaluation
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
