# Roadmap toward 9.5+/10

**Updated: 2026-11-11. Status: implementation roadmap; qualification is evidence-gated.**

A 9.5 rating should mean a developer can trust the result, reproduce the
measurement, recover an interrupted run, and understand the cost. More agents
or a longer feature list do not establish that. The priorities below build on
Orkestr's existing local-first scheduler, worktrees, event log and verification
boundary rather than replacing them.

Formal ratings must follow the [strict scoring policy](scoring-criteria.md),
which defines 20 binary criteria, required evidence, confidence thresholds and
non-negotiable score caps. Its 18 mandatory criteria total 95 points: **all must
pass for 9.5/10**. The earlier conversational rating is not formal qualification.
These are internal release requirements, not a guarantee of universal safety.
No phase is complete merely because its implementation or unit tests exist.

## Evidence that determines the order

- The local sweep completed **62 trials**, with **2 user-requested cancellations**.
  **11 of 16 runtime-gate passes recorded no worker tool calls**. This does not
  establish that the requested edits were made: the planner can choose weaker
  criteria and existing fixture state can satisfy them.
- The direct comparison contains **45 requests**, five tiny tasks per model or
  device configuration. Qwen3-1.7B and the third-party Kimi distillation scored
  4/5; that is insufficient evidence for a software-engineering leaderboard.
- Cleanup found a residual model server. Sequential foreground requests therefore
  did not establish isolated GPU/CPU timing. The eight checkpoints include
  third-party MiniMax/Kimi distillations, not official vendor models.
- The 2026-11-10 benchmark snapshot recorded **294 passing tests**. This is useful regression
  coverage, but it does not close the independently documented verification,
  containment, recovery and evaluation gaps.

Sources: [workflow results](local-benchmark-results.md),
[inference results](local-inference-results.md),
[methodology](local-benchmark-methodology.md),
[limitations](limitations.md), and [improvement audit](improvement-audit.md).

## Release order and decision gates

| Stage | Outcome | Depends on | Required evidence to exit |
|---|---|---|---|
| R0 | Success means the original request passed independent checks | None | Adversarial false-success suite, protected request contract, verified final tree |
| R1 | Runs respect resource limits and stop cleanly | Start alongside R0 | No owned child processes remain across 100 cancellation/failure cycles; complete attempt accounting |
| R2 | Untrusted work is contained and crashes are recoverable | R0 + R1 | Enforced isolation profile, durable run ownership, crash/replay and side-effect tests |
| R3 | Benefits hold on realistic tasks | R0–R2 | Held-out real-repository evaluation, uncertainty estimates, reproducible artifacts |
| R4 | Coordination earns its overhead | R3 | Held-out quality/speed/cost gains over the single-agent baseline, with ablations |
| R5 | Another developer can operate a supported release | R0–R4 | Independent clean-machine onboarding, versioned release, operational evidence and review |

These are dependency gates, not calendar promises. R0 and R1 are the first
implementation milestones. R2 precedes evaluation on untrusted repositories.
Do not expand the model or feature matrix while those gates are failing.
The strict policy takes precedence over any abbreviated gate description here.

| Roadmap stage | Required scoring criteria |
|---|---|
| R0 | C1–C5 |
| R1 | R1, R2, R4 |
| R2 | R3, S1–S3 |
| R3 | V1, V2, M1, M2 |
| R4 | V3 |
| R5 | U1, plus continued passage of all earlier mandatory criteria |

V4 and U2 are stretch criteria for 9.75 and 10.0; they cannot compensate for
missing mandatory evidence. Stage names and criterion IDs are separate labels.

## Immediate closure work after the implementation review

The 2026-11-11 review found real implementation progress, but also reproduced
an output-check failure being accepted by the contract gate. It additionally
found incomplete execution-path wiring for ownership and request accounting,
partial deadline enforcement, and a private-HOME claim without actual isolation.
The exact observations and closure requirements are in
[the scoring policy's blocking examples](scoring-criteria.md#6-blocking-examples-from-the-2026-11-11-review).
Treat these as release blockers to revalidate, not features completed by their
commit titles. The review passed 365 tests with one capability-dependent skip;
that run does not replace the required independent qualification evidence.

## R0 — Make success trustworthy

**Primary code:** `planning/planner.py`, `verification/`, `orchestrator.py`,
`integration/`, `models/`, and the benchmark fixtures under `src/agent_squad/`.

1. Introduce a versioned **request contract** containing the approved objective,
   required outcomes, scope and acceptance checks. Record its content hash at
   run creation. Plans may add checks or propose a contract revision, but cannot
   remove, replace or weaken a required check without explicit owner approval.
   A model-generated check is supplementary evidence, not an independent oracle.
2. Run a separately owned verifier against the **final combined candidate tree**.
   Worker success and passing each worktree independently are insufficient.
   Bind the evidence to the request hash, base revision, candidate tree hash,
   verifier version, environment and executed commands. Invalidate it after any
   candidate or contract change. An approved no-op is valid only when the
   original request explicitly allows it and the verifier establishes it.
3. Keep evaluator tests, expected outputs and authoritative baseline artifacts
   outside worker write access. Tests created by the worker remain useful, but
   cannot be the sole evidence for the request. Detect test deletion, skipped
   tests, weakened assertions, altered test configuration and changed fixtures.
4. Make outcomes explicit: **accepted, rejected, inconclusive, timed out,
   cancelled**. Missing evidence, zero collected tests, a flaky check, an
   unverifiable criterion or a worker's completion message must not become an
   accepted result. Record retries rather than silently selecting a passing run.
5. Add adversarial cases: unchanged baseline passes its old suite; stub output;
   incorrect implementation paired with a weak test; tampered verifier; mutually
   incompatible task patches; stale evidence; and a valid already-satisfied
   request. Include cases where each individual patch passes but their union fails.

**Exit:** every accepted run links to the unchanged approved contract and final
candidate evidence. Require **zero false accepts across at least 300 independent,
held-out adversarial cases**, while reporting false rejects on known-correct
candidates. Zero failures in 300 independent trials gives an approximate
one-sided 95% upper bound of 1%, not a proof of zero risk. Correlated variants
need clustered analysis and must not be treated as 300 independent cases.

**Release blocker:** any known way for a worker or planner to turn a failing
required outcome into an accepted run without owner approval.

## R1 — Bound execution, resources and accounting

**Primary code:** `benchmark/local.py`, `providers/`, `agents/`,
`scheduler/engine.py`, and `scripts/cleanup_local_models.py`.

- Enforce one absolute run deadline across downloading, startup, planning,
  worker requests, verification, retries and shutdown. Reserve teardown time;
  a per-request timeout is not a whole-run deadline. Cancellation must interrupt
  queued work and every active stage, including startup and verification.
- Give each run a durable registry of owned PIDs/process groups, model handles
  and downloaded artifacts. On success, exceptions, cancellation and signals:
  terminate, wait, force-kill within a bounded grace period, and verify exit.
  Clean only owned artifacts; preserve pre-existing user models and processes.
  Recover and reconcile abandoned resources on the next startup.
- Replace the parameter-count layer estimate with actual model metadata and
  measured allocation. Reserve VRAM for weights, KV cache, execution buffers
  and other occupants; include available RAM and swap pressure. Acquire resource
  reservations before loading. Default this 4-GiB GPU to one model at a time;
  increase concurrency only after a fit check. A failed fit gets a recorded,
  bounded CPU/offload fallback rather than repeated oversubscription.
- Account for **every** planner, worker, reviewer, retry and fallback request,
  including failed calls. Record queue, load, prompt, decode, tool, verification
  and shutdown time separately. Distinguish reported, estimated and unavailable
  usage. Missing values are null, never invented zeros. Zero hosted-API charges
  do not imply zero electricity or hardware cost.
- Record the effective configuration, actual offload, token limits and provider
  concurrency. Two workers must not be described as parallel inference when a
  provider semaphore serializes their calls.

**Exit:** 100 automated cycles covering success, startup failure, timeout,
SIGINT/SIGTERM, provider failure and cancellation leave **zero surviving owned
processes** after the configured grace period. Pre-existing processes and models
survive. Deadline overrun is bounded by that grace period. Every dispatched
request has one terminal accounting record, including unavailable usage when
appropriate. Tests include a deliberately slow-to-exit child process.

## R2 — Contain untrusted work and make recovery deterministic

**Primary code:** `sandbox/`, `security/`, `store/`, `messaging/`, `recovery/`,
`workspace/` and approval handling.

- Publish an explicit capability matrix for development and hardened profiles.
  The hardened profile must enforce the promised filesystem/network/process
  boundaries through a supported OS/container mechanism, run without host
  credentials, and refuse execution when a required boundary is unavailable.
  A worktree, executable allow-list or injection regex is not that boundary.
- Treat repositories and tool results as untrusted. Isolate dependency installs,
  home directories and caches; disable uncontrolled Git hooks and ambient Git
  configuration. Protect verifier material and approval records through actual
  permissions, not prompt instructions. Make network access explicit and scoped.
- Bind protected-action approval to the exact action, parameters, workspace,
  request version and expiry. Replanning or changed commands invalidate stale
  approval. Use a separate identity/authorization design before enabling remote
  or multi-user access; keep the first supported release local and single-user.
- Add atomic run ownership and fencing so two processes cannot resume the same
  run simultaneously. Persist message deduplication and version checks. Test
  crashes around state, message, checkpoint and artifact writes.
- Classify external effects as idempotent, reconcilable or approval-required.
  Recovery must not blindly repeat ambiguous non-idempotent actions. State
  transition fencing does not provide exactly-once external side effects.
- Keep the durable history append-only. Add retention and snapshot-based replay
  with integrity checks; archival must preserve acceptance evidence. Do not
  delete old events until restoration and audit equivalence have been tested.

**Exit:** the supported containment profile passes escape/credential-access and
policy-bypass tests; unsupported isolation fails closed. Recovery tests show
one run owner, durable message deduplication, equivalent recovered projections
and explicit reconciliation of ambiguous effects. No unresolved critical issue
in an independent review of these trust boundaries.

## R3 — Establish credible real-repository evidence

**Primary code:** `benchmark/`, `docs/*benchmark*`, fixtures and CI configuration.

1. Pilot **12 tasks across 3 pinned repositories** before enlarging the corpus.
   Include bug fixes, features, refactors and tests; hidden regression checks must
   detect no-op and plausible-but-wrong patches. Record repository licenses,
   task provenance, commit IDs, dependencies and environment fingerprints.
2. Expand to **at least 60 tasks across 6 repositories** and multiple project
   shapes. Define held-out repositories before tuning prompts or scheduling.
   Audit accidental solution leakage and model-training contamination risk;
   describe what cannot be established rather than claiming contamination-free
   evaluation without evidence.
3. Compare the single-agent baseline and candidate coordination policy using
   matched task/model/budget conditions and at least three seeded trials where
   sampling applies. Randomize execution order. Evaluate at least three model
   configurations; the tiny local checkpoints are one hardware tier, not the
   entire supported-model claim. Publish the actual coverage matrix and caps.
4. Require an idle/known resource baseline, verify ownership before and after
   each timing run, and discard contaminated timing samples with the reason
   retained. State cold/warm cache conditions. Use server decode timings for
   decode throughput, not total tokens divided by end-to-end latency.
5. Publish independent acceptance rate, false accepts/rejects, wall-time p50/p95,
   peak RAM/VRAM, reported/estimated/unavailable tokens, hosted cost and recovery
   outcomes. Include planner and retry costs. Use confidence intervals clustered
   by repository/task where appropriate, alongside per-task records.
6. Predeclare budget and cancellation handling. Deadline exhaustion is a failed
   budgeted attempt. User-aborted work is censored, with counts and sensitivity
   bounds; do not improve the headline by dropping unfinished work. Preserve the
   historical 62-completed/2-cancelled report rather than rewriting its results.

**Exit:** a clean runner reproduces the artifact schema and verifier outcomes;
performance variation is quantified rather than promised identical. An audit
can trace every headline number to task, configuration, response, patch and
verifier evidence. Claims explicitly separate synthetic fixtures, tiny direct
probes and real-repository results.

## R4 — Make coordination earn its overhead

Start with a **single agent as the default**, retaining the existing override
for controlled comparisons. Use held-out results from R3 before changing it.

- Fit the selector's duration, duplication and coordination estimates on training
  tasks. Evaluate calibration and decision regret on held-out repositories.
  Choose parallelism only when a conservative expected benefit exceeds its
  overhead; uncertain decisions fall back to single-agent execution.
- Add bounded replanning after classified failures. Preserve the request
  contract, completed verified dependencies, approval scope and global budget;
  version every plan and emit `PLAN_REVISED`. A retry with an unchanged brief is
  not a replan. Stop repeated revisions that do not improve evidence.
- Route models from measured task requirements and resource fit. Record provider
  fallbacks and parsing/tool-call recovery. Avoid silent model changes within a
  controlled comparison. Pin and review tool schemas and prompt versions.
- Optimize verification only after correctness is sound. Cache on candidate tree,
  contract/verifier version, dependency lock, environment and command identity;
  tree plus test selector alone is insufficient. Verify final integration anew
  when the accepted tree changes. Add targeted checks without replacing required
  full regression checks.

**Exit:** predeclare a practically meaningful target, initially **at least 15%
median end-to-end improvement** on eligible parallel tasks with acceptance-rate
non-inferiority within a **2 percentage-point margin** versus single-agent.
Use paired uncertainty estimates, publish tail-latency/cost regressions and
retain the single-agent fallback. These are decision thresholds, not current
measurements; if the corpus cannot support the confidence claim, collect more
runs or narrow the claim. Ablate planning, replanning, extra workers and routing
separately so improvements can be attributed.

## R5 — Ship an operable, reproducible release

- Lock application and evaluation dependencies, pin artifacts by revision/hash,
  and validate supported Python/OS/provider combinations in CI. Run a type checker
  with an explicit shrinking baseline; prioritize verification, state and provider
  interfaces. Add migration/restore tests before changing persisted schemas.
- Offer one documented path: install → declare request/checks → preview plan and
  budget → run → inspect candidate diff and evidence → approve integration.
  Distinguish acceptance, worker completion, cancellation and inconclusive checks
  consistently in CLI, API and dashboard. Make time/cost limits visible.
- Provide structured diagnostics for context exhaustion, invalid tool arguments,
  resource pressure, approval expiry and provider failures. A failure report must
  contain a reproducible configuration and a recovery action without exposing
  credentials. Redaction needs adversarial tests, not only pattern examples.
- Document backup/restore, artifact retention, supported isolation, model cleanup,
  rollback and incident handling. Record release notes and known limitations.
  Have three developers outside the implementation team reproduce onboarding
  and one representative repair on clean supported machines.

**Exit:** a versioned release installs and recovers on the supported matrix,
produces inspectable evidence, passes an independent trust-boundary review, and
has **30 days of recorded use by at least 3 external users**, including failures
and operating costs. Time passing and positive anecdotes alone do not satisfy
this gate; review the run records and unresolved defects.

## First five implementation PRs

| Order | Bounded change | Required demonstration |
|---|---|---|
| 1 | Immutable request contract and final-tree acceptance boundary | A weakened plan, stale proof and unchanged baseline cannot pass the original request; valid no-op remains expressible |
| 2 | Owned-process cleanup and whole-run deadline | Reproduce the residual-server incident, then prove cleanup across signals, startup errors and a child ignoring SIGTERM |
| 3 | Complete request accounting and model resource admission | Planner/retry failures retain usage provenance; an oversized weights/KV/workspace plan cannot silently reserve the GPU |
| 4 | Protected independent verifier and adversarial task corpus | Wrong code plus a worker-authored weak test is rejected; verifier tampering fails closed |
| 5 | Durable ownership, message deduplication and enforced isolation profile | Concurrent resume is fenced, replayed messages cannot duplicate effects, and unavailable mandatory isolation blocks execution |

Each PR includes its migration impact, regression evidence and a reproducible
before/after case. Stabilize shared interfaces before parallel implementation.

## Rating qualification and scope discipline

The [strict scoring policy](scoring-criteria.md) is the sole scoring authority.

- **9.5:** all 18 mandatory criteria PASS, with reviewed evidence for the same
  release and scope; 95 points, no applicable lower cap.
- **9.75:** the 9.5 requirements plus either independently replicated results
  (V4) or the extended operational record (U2).
- **10.0:** all 20 criteria PASS; 100 points.

An unresolved false acceptance or critical authority/containment bypass caps the
formal score at **8.0**. Missing lifecycle/authority evidence caps it at **8.5**;
missing real-repository value or measurement evidence caps it at **9.0**. Any
other missing mandatory criterion blocks 9.5 and caps it at **9.4**. Always apply
the lowest cap; do not round upward. A formal score is NOT ASSESSED until a full
assessment record exists. Missing evidence is not assumed to pass.

A local single-user release can qualify within its declared scope. Distributed
execution, public multi-tenancy and unrestricted deployment need additional
scope-specific evidence. Defer arbitrary agent spawning, large catalogs and UI
expansion until the mandatory gates pass. Optimize graph-scan asymptotics only
when profiles show they matter. More tests, agents or tokens cannot substitute
for independently demonstrated reliable outcomes.
