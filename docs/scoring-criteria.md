# Strict project scoring and qualification criteria

[Documentation home](README.md) · [Current status](../PROJECT_STATUS.md)
· [Evidence interpretation](evaluation.md)

Normative internal qualification policy. This editorial refresh does not change weights, thresholds or the 18 mandatory criteria.

**Policy version: 1.0 · Adopted: 2026-11-11.**

This is the authoritative policy for a **formal Orkestr project score**. It
supersedes discretionary scoring language in the [roadmap](roadmap.md).
Implementation stages remain in that document; the qualification rules are here.
An earlier conversational rating, including 8/10, is an engineering opinion,
not a qualification under this policy. This policy defines an internal review
standard, not an external certification or a guarantee of universal correctness.

## 1. Calculation and non-negotiable rules

There are **20 binary criteria** below: **18 mandatory criteria worth 95 points**
and **2 stretch criteria worth 2.5 points each**. Each criterion receives either
its full weight or zero. There is no partial credit for a module, a commit,
a unit test, a claim in documentation, or a demonstration without qualifying
artifacts. A criterion passes only if **every requirement in its row passes**.

```text
raw_score = sum(points for criteria with reviewed PASS evidence) / 10
formal_score = min(raw_score, every applicable score cap)
```

Retain the exact result; do not round upward across a qualification threshold.
A 9.49 is not 9.5. Before issuing a formal score, publish a complete assessment
of all 20 criteria. Until that assessment exists, the status is **NOT ASSESSED**,
not an invented numeric score. In the completed assessment, missing, skipped,
expired or inconclusive evidence earns zero points.

| Condition | Maximum formal score |
|---|---:|
| Known false acceptance of an unmet required outcome, verifier tampering bypass, or critical containment/authority bypass | 8.0 |
| Any R1–R4 lifecycle criterion or S1–S3 authority/containment criterion fails or lacks qualifying evidence | 8.5 |
| Any V1–V3 real-repository value criterion or M1–M2 measurement criterion fails or lacks qualifying evidence | 9.0 |
| Any other mandatory criterion fails or lacks qualifying evidence | 9.4 |
| All 18 mandatory criteria pass | 9.5 |
| All mandatory criteria and exactly one stretch criterion pass | 9.75 |
| All 20 criteria pass | 10.0 |

Here R1–R4 are **criterion IDs**, not the roadmap's stage names. Apply the lowest
applicable cap. The last three rows are qualification levels, not caps that
prevent earning a higher level. Feature count, test count, UI quality or a
strong optional criterion cannot compensate for a failed mandatory criterion.

## 2. What counts as evidence

A PASS must refer to one immutable **release candidate commit**, its declared
supported scope, the evidence bundle and a named independent reviewer. An
independent reviewer did not implement that criterion and must reproduce its
critical result; an author reviewing their own output does not qualify.

Every bundle must contain:

- Exact commit, dependency lock/environment identifiers, model/provider/prompt
  versions, quantization, effective limits, hardware and isolation configuration.
- Executable reproduction commands, raw outcomes including failures and
  cancellations, timestamps, content hashes and the generated metric report.
- A mapping from each assertion to the relevant runtime execution path and
  evidence. Helper-only tests do not prove CLI/API/run-path enforcement.
- Baseline and candidate tree identities, request/verifier identities and all
  relevant checks. Protected evaluator contents may remain private, but an
  authorized reviewer must inspect their provenance and reproduce the verdict.
- Reviewer identity, date, decision, unresolved defects and evidence locations.
  Redact credentials and personal data without deleting failure evidence.

The full suite must pass on the release candidate with **zero unexpected skips,
xfails or failures** on every claimed supported environment. Expected
capability-dependent skips require an explicit unsupported-capability record
and evidence of refusal on that environment; they cannot establish a supported
feature. Test counts alone earn no points.

Evidence expires when code, configuration, dependencies, prompts, models or
verifier logic affecting the criterion changes. The reviewer must justify the
impact assessment and rerun affected evidence. Cosmetic-only changes may reuse
prior evidence only when the recorded reviewer explains why. A later discovered
counterexample immediately revokes the affected PASS and applies the score caps.

## 3. Mandatory criteria: 95 points

The requirements below are conjunctive. Counts are minimum coverage, not proof
that statistical precision or safety has been established.

### Correctness and trust — 30 points

| ID | Points | Required PASS evidence |
|---|---:|---|
| C1 | 6 | The final contract verdict uses the complete deterministic check result, including errors and match predicates, rather than exit code alone. Negative end-to-end cases cover all supported check kinds, exit-zero output mismatches, invalid regex, check exceptions, timeout, zero-test collection, skipped required tests and advisory-only checks. **Every unmet required check prevents acceptance.** The reproduced output-mismatch counterexample must be rejected through both the gate and the normal run entry point. |
| C2 | 6 | The owner-approved request and required outcomes are preserved as an immutable, versioned contract with a captured hash. Initial planning, retries and replanning cannot delete or weaken them, including by reusing an ID with different semantics. Every unauthorized mutation fails before acceptance; an authorized revision explicitly invalidates old evidence and approvals. Evidence binds to the final combined tree, base, contract, verifier and environment. Changing any bound input invalidates the verdict. |
| C3 | 6 | A held-out corpus has **at least 300 independent adversarial cases with zero false accepts** and an exact one-sided 95% false-accept upper confidence limit **below 1%**. A separate set of **at least 100 known-correct cases** has a one-sided 95% acceptance lower confidence limit **at least 95%**. Cases include incompatible patch unions, unchanged baselines, weak worker-written tests, stale evidence, legitimate no-ops and preserved baseline invariants. Repeated seeds and cosmetic variants do not count as independent cases; correlated cases require a justified effective sample and clustered analysis. An always-reject verifier cannot pass. |
| C4 | 6 | Required verifier material and expected outputs are externally owned and outside worker write authority. Through every worker execution path, attempted deletion, rewrite, skip/config manipulation and modify-then-restore tampering are denied or force rejection. Testing only a hash after execution is insufficient. The worker cannot supply the only oracle for its own changes. Hidden evaluator material is not exposed in its writable/readable task environment. |
| C5 | 6 | CLI, API, persisted run records and dashboard agree on accepted/rejected/inconclusive/timed-out/cancelled outcomes. Worker completion is distinct from request acceptance. Tests cover both integrated and non-integrated runs, partial failure, empty patches, valid no-ops, required regression checks already passing on the base, and all final-tree verdicts. No entry point reports request acceptance without matching, valid contract evidence. |

For C3, zero false accepts in 300 independent cases gives
`1 - 0.05 ** (1/300) ≈ 0.00994`. If independence is not defensible, that calculation
cannot be used to certify the threshold. Contract semantics must distinguish
preserving an existing invariant from establishing a newly requested change;
rejecting every pre-satisfied invariant is not correct verification either.

### Reliability and resource control — 20 points

| ID | Points | Required PASS evidence |
|---|---:|---|
| R1 | 5 | One absolute deadline covers download/load, inspection, initial planning, replanning, provider queue/request, worker/tools, verification, integration and teardown. Inject a blocking operation and cancellation into **each phase** through normal run/benchmark entry points. For declared deadline D, execution finishes by **D + 5 seconds** and creates no new work after expiry. No synchronous operation can escape the bound merely because the scheduler has not started. Cancellation acknowledgement occurs within **5 seconds**. Declare which non-interruptible OS failures are outside the supported scope. |
| R2 | 5 | **100 integrated lifecycle cycles** leave **zero owned live processes/groups and zero unrequested temporary workspaces** after a grace period of at most **5 seconds**. Cover normal success, startup failure, failed verification, task timeout, provider failure, user cancellation, SIGINT, SIGTERM and a child ignoring SIGTERM, with **at least 10 cases per category**; categories may overlap. Reconcile an owner killed by SIGKILL on next startup within **30 seconds**. Preserve unrelated/pre-existing processes, models and files. Retained debugging workspaces require explicit owner opt-in and a durable manifest; silently leaving them behind fails. Test PID reuse, detached descendants and a lost registry write. Registry/helper tests without actual dispatch and shutdown wiring do not qualify. |
| R3 | 5 | Every CLI/API run and resume atomically acquires durable ownership before mutation, heartbeats it, fences ownership-sensitive writes and releases/reconciles it. Concurrent resume tests through real entry points produce **one active owner**. A stale owner cannot update state or trigger effects after lease takeover. Duplicate messages remain deduplicated across restart, and crash-boundary replay reproduces state. Ambiguous non-idempotent effects require reconciliation rather than blind repetition. |
| R4 | 5 | A terminal accounting record exists for **every dispatched model attempt**, including planner, worker, reviewer, retry and fallback, success/failure/cancellation. Inject failures before and after usage arrives; counts reconcile **exactly** with a recording endpoint. Missing usage is null with provenance, never measured zero. Record queue/load/prompt/decode/tool/verification/teardown time distinctly. Resource admission uses actual model metadata and reserves weights, KV cache, workspace and RAM/VRAM headroom before load; oversized admission and bounded fallback are exercised on the supported hardware tier. |

A configured timeout, optional registry or ownership function is not evidence
that callers use it. The recorded live paths must demonstrate enforcement.

### Demonstrated developer value — 17.5 mandatory points

| ID | Points | Required PASS evidence |
|---|---:|---|
| V1 | 5 | Preregister **at least 60 nontrivial tasks across 6 pinned real repositories**, covering bugs, features, refactors and tests with independent regression oracles. Separate training/tuning and held-out repositories before development. Record licenses, task provenance, baseline state, exclusion rules and contamination limitations. Evaluate at least **3 model configurations**, **3 repeated trials per task/configuration/policy cell** (using distinct seeds where sampling applies), and single-agent versus the candidate policy under equal declared resource/cost/time ceilings. Randomize matched execution order and have task selection independently reviewed before evaluation. Publish the entire coverage matrix; synthetic probes cannot substitute. |
| V2 | 5 | On the preregistered held-out corpus, the declared supported primary configuration achieves **at least 80% independent acceptance**, with a **95% lower confidence bound at least 75%**, and **zero known false accepts**. All budget-exhausted attempts remain failures in the primary denominator. User-aborted work is separately censored and published with sensitivity bounds; a qualification run with unresolved censoring must be completed or repeated under its preregistered rule. Report per-repository, per-task-type and per-model results so aggregate success cannot hide a broken supported tier. |
| V3 | 7.5 | Relative to a strong single-agent baseline with the same model/tool access and total budgets, the policy demonstrates a **paired 95% lower confidence bound of at least 15% median end-to-end speed improvement** on a preregistered eligible-parallel subset of **at least 20 tasks across 4 repositories**. Across the full held-out corpus, the acceptance-rate difference has a **95% lower bound no worse than −2 percentage points**, and cost and p95 wall-time regression each have a **95% upper bound no worse than +10%**. Publish ablations for planning, extra workers, replanning and routing; ambiguous cases use the single-agent fallback. No post-hoc subset selection. |

Model configurations below the primary tier may have lower acceptance, but their
results and support limits must be explicit; do not advertise the primary result
as the result of every checkpoint. Confidence methods must account for paired
runs and clustering by task/repository. Sixty tasks and three seeds are minimum
coverage, not automatically enough power for V2/V3: collect more held-out data
until the bounds pass, or leave the criterion failed. Do not relax the margins
after seeing outcomes.

### Containment and authority — 15 points

| ID | Points | Required PASS evidence |
|---|---:|---|
| S1 | 5 | Demonstrate **at least one supported hardened execution environment**; an empty hardened-support matrix cannot pass. On each such environment, actual worker commands run with enforced filesystem, network and process boundaries, a fresh private HOME, no inherited credentials, and isolated writable caches. Adversarial commands cannot read/write host canaries, access protected evaluator material, reach a denied network endpoint, or leave descendants outside cleanup authority. Missing required containment refuses the entire run before work. Environment strings, a successful capability probe or worktree scoping alone do not qualify. |
| S2 | 5 | Every protected effect requires an approval bound to action, exact arguments, workspace, contract/request version and expiry. Mutation, expiry, replay, replan and cross-run reuse tests cannot authorize the wrong effect. Repository-local/global Git configuration and hooks cannot execute outside the declared authority. Worker-controlled command execution cannot bypass file/approval policy through an alternate tool. No ambient secrets appear in logs, artifacts or provider requests in the seeded-secret suite. |
| S3 | 5 | A reviewer independent of the implementation audits verification, sandboxing, approvals, process ownership, artifact trust and recovery for the declared release scope. Publish a redacted report, severity definitions and reproduction/retest evidence. **Zero unresolved critical or high-severity findings**, and zero unresolved issues allowing false acceptance or authority bypass regardless of label. Reclassified findings need reviewer agreement and evidence, not an author-only downgrade. |

A local single-user scope is permitted. Public service access, multi-user
operation or distributed workers cannot be claimed without corresponding
identity, authorization and isolation evidence. Narrowing scope is prospective:
it cannot silently remove difficult benchmark cases after evaluation starts.

### Reproducibility and measurement — 10 points

| ID | Points | Required PASS evidence |
|---|---:|---|
| M1 | 5 | Every result is traceable to pinned task/repository/model/prompt versions, dependency locks, effective configuration, request/response, candidate patch/tree and verifier records. The evaluator derives the published metrics from raw artifacts, including failed, timed-out and cancelled work. CPU/GPU timing samples have recorded cold/warm conditions, known resource baselines, recorded process occupancy and **no surviving benchmark-owned server from a prior configuration**. Preregister allowed background-load and timing-exclusion thresholds; uncontrolled simultaneous inference invalidates the timing sample. Contaminated samples are retained and marked invalid for qualification, then rerun; a caveat does not turn invalid timing into a PASS. |
| M2 | 5 | An independent reviewer reproduces the release installation, verifier verdicts, artifact-schema checks, recovery and metric generation on a clean supported machine using the published instructions. Publish supported Python/OS/provider combinations and their CI results, dependency locks, schema migrations and backup/restore evidence. Require zero lint errors in changed authored code and zero type-check errors in verification, state, provider and orchestration paths; suppressions need reviewer-approved technical justification and cannot hide an unresolved criterion violation. All changed critical paths have regression coverage; reported claims agree across docs and product surfaces. Reproduction must explain numerical variance with measured intervals rather than demand identical timing. |

### Usability and operations — 2.5 mandatory points

| ID | Points | Required PASS evidence |
|---|---:|---|
| U1 | 2.5 | **3 developers outside the implementation team** independently install the release, declare a request/checks, inspect plan/budget, run, inspect diff/evidence and recover an interrupted task. Over **30 consecutive days**, retain at least **100 genuine runs across 3 repositories**, involving all 3 users, plus **10 documented recovery/failure exercises**. Publish anonymized outcomes, failures, operating costs and issue disposition. No unresolved issue causes loss of an accepted artifact, duplicate protected effects or misleading acceptance. Synthetic batch repeats and developer-only demos do not satisfy usage. |

## 4. Stretch criteria: the final 5 points

These do not excuse missing mandatory evidence. They are eligible only after
all mandatory criteria pass.

| ID | Points | Required PASS evidence |
|---|---:|---|
| V4 | 2.5 | A second independent team reproduces V1–V3 from published artifacts on a separately administered supported machine, reproduces the verdicts and independently confirms the preregistered confidence-bound claims. Publish deviations, resource differences and the replication report. A second run by the original implementation team is insufficient. |
| U2 | 2.5 | Extend U1 to **90 consecutive days, 10 external users, 1,000 genuine runs and 10 repositories**. Retain all failures and publish operating-cost and incident summaries. All correctness/security incidents have independently reviewed fixes and regression evidence; there are no unresolved critical/high findings at qualification. Do not count cloned tasks, repeated synthetic fixtures or an unchanged automated loop as external use. |

## 5. Assessment record and change control

For each release, publish a record with the following fields. This is a template,
not a claim that the current project has passed these criteria.

```yaml
policy_version: '1.0'
release_commit: '<full commit hash>'
scope: '<supported hardware, OS, providers, local/remote authority>'
status: NOT_ASSESSED # or ASSESSED after all 20 decisions are recorded
criteria:
  C1:
    decision: MISSING # PASS, FAIL, MISSING, or INCONCLUSIVE
    evidence: []      # content hashes and artifact/report locations
    reviewer: null
    reviewed_at: null
    counterexamples: []
# Repeat for every ID: C1-C5, R1-R4, V1-V4, S1-S3, M1-M2, U1-U2.
raw_points: null
applicable_caps: []
formal_score: null
```

The assessor verifies the weights total 100, all 18 mandatory decisions are
present, evidence matches the release, the minimum confidence bounds pass,
and all applicable caps have been applied. **9.5+ is prohibited until every
mandatory decision is PASS.** Stretch points expire immediately if a mandatory
PASS is revoked. Roadmap stage labels and commit messages never set these fields.

Policy or threshold changes require a versioned rationale and independent
review **before** the next qualification dataset is collected. Historical
results retain their original policy and limitations. No threshold adjustment,
selective rerun or retrospective exclusion may be used to promote a failed
release into a passing one.

## 6. Blocking examples from the 2026-11-11 review

These are observations of commit `37fd1eb` and the review-time working tree,
not a permanent claim about later commits. Revalidate each against the release
candidate; the code must be fixed and the required regression evidence supplied.

| Observation | Qualification affected | Required closure |
|---|---|---|
| The low-level output check returned `passed=False` for missing expected text, while the contract verdict returned `accepted` because it used exit code 0 | C1; maximum 8.0 until resolved | Reproduce through the normal run entry point, fix complete-result propagation, and pass every supported check-kind negative case |
| Ownership acquisition/assertion functions were defined and tested without callers in orchestration/resume | R3 | Wire acquisition, heartbeat, fencing and release into every run/resume path; prove concurrent-resume exclusion end to end |
| The request ledger existed without request-path integration | R4 | Reconcile every planner/worker/retry/fallback attempt with a recording endpoint, including partial failure and cancellation |
| Deadlines were applied to scheduler attempts while initial planning and final verification could outlive them | R1 | Independently block each phase and demonstrate the declared deadline and cancellation bounds |
| The hardened guard claimed private HOME availability while environment construction retained the existing HOME | S1 | Enforce a private HOME and filesystem boundary; host-canary access fails through actual worker commands |
| Existing GPU/CPU timings included a residual server until final cleanup | M1 | Retain the historical results with their caveat; rerun qualification timing with verified process ownership and resource baseline |

The review recorded **365 passing tests and 1 capability-dependent skip**.
That does not, by itself, award any criterion or qualify a 9.5 score.
