# Improvement audit

[Documentation home](README.md) · [Status](../PROJECT_STATUS.md) · [Roadmap](roadmap.md)

This audit distinguishes missing implementation from missing proof at baseline
`f8404f0`. Earlier claims that the project had no real provider, no lockfile,
no type checking or no replanning are no longer current. Their original context
is preserved in Git and the historical experiments, not repeated as today's
backlog.

## Priority gaps

| Priority | Gap | Why it matters | Next evidence |
|---|---|---|---|
| Critical | Independent acceptance-boundary qualification | A strong-looking check can still be weakened, misinterpreted or bypassed by its caller | Reproduce all required negative cases through real entry points and validate owner/final-tree binding |
| Critical | Containment and authority review | Command parsing, worktrees and environment settings are not a universal security boundary | Independent supported-host tests of filesystem, network, evaluator and approval authority |
| High | Complete integrated lifecycle evidence | Helpers can pass while startup, cancellation or exceptional teardown never invokes them | Full deadline and 100-cycle lifecycle matrix with external ownership observations |
| High | Real-repository value study | Synthetic fixtures and generated verifier cases do not establish coding reliability | Preregistered held-out tasks, independent oracles and a matched single-agent baseline |
| High | Empirical selector/replanning benefit | Heuristic predictions are not observed improvements | Paired quality/time/cost measurements and ablations |
| High | External operational evidence | Author-run demos do not establish another developer can recover and review work | Clean-machine reproduction, independent users and sustained run records |
| Medium | Retention and long-history behavior | Unbounded history/artifacts affect storage, recovery and sensitive-data exposure | Tested retention/backup/restore policy that preserves accepted evidence |
| Medium | Interface and packaging consistency | An optional dependency or misleading success label can make a correct mechanism unusable | Supported optional API/UI environment, acceptance-aware automation and verified CLI examples |

## Review findings and implementation progress

| Earlier finding | Current implementation change | What is still required |
|---|---|---|
| Output mismatch accepted when exit code was zero | Contract gate consumes the full check result; regression rejected in review | Independently reproduce all supported kinds and normal entry points |
| Ownership APIs existed without orchestration callers | Acquisition, heartbeat, fencing and release/resume paths now exist | Concurrent initialization/resume and stale-effect tests through the real lifecycle |
| Request ledger existed without provider wiring | Providers/router/orchestrator share accounting paths | Independent reconciliation across failures, retries, fallback and cancellation |
| Deadline missed non-scheduler phases | Phase scope and cancellation seams expanded | Declared end-to-end bound on all supported startup/verification/teardown cases |
| HOME isolation was claimed but inherited | Private HOME and real containment backend/probes added | Independent host-canary, evaluator and effect tests, not only environment assertions |
| Original timing retained a residual server | Ownership-checked timing procedure and later sample added | Valid complete qualification dataset; preserve the earlier sample with its caveat |

These are progress statements, not PASS decisions. The checked-in assessment
records missing/inconclusive criteria and is tied to its own release commit.

## Architectural work that should stay subordinate

Ready-set and overlap algorithms can be optimized after profiles show their
cost matters. Distributed queues, arbitrary spawned-agent networks, expanded UI
surface and large model catalogs add authority and reproducibility costs. They
should not displace independent verification, predictable teardown and realistic
baseline measurements.

Verification caching must include the candidate tree, contract/verifier,
dependencies, environment and command identity. Tree plus selector alone is not
a sufficient key when the environment or oracle changes. Retention/compaction
must preserve audit/recovery evidence rather than mutate old history to improve
appearance or shrink a database without a restore test.

The [strict scoring policy](scoring-criteria.md) defines exact evidence thresholds;
the [roadmap](roadmap.md) defines the sequence. This audit supplies the rationale,
not a second competing scorecard.
