# Project status

**Documentation review: 2026-11-11 · source baseline: `f8404f0` · package version: 0.1.0.**

[Overview](README.md) · [Documentation](docs/README.md) · [Roadmap](docs/roadmap.md)
· [Strict qualification](docs/scoring-criteria.md)

## What the project currently is

Orkestr is a local-first repository-agent runtime. The distribution/CLI is
`agent-squad`; the Python package is `agent_squad`. It plans typed task DAGs,
executes bounded workers in worktrees, mediates tools/approvals, combines patches,
judges owner request contracts, and stores execution/evidence for inspection,
recovery and replay. Its scope is single-user, single-host local operation.

Implementation status below describes source and author-produced tests. It does
not mean every strict scoring criterion is independently qualified.

## Capability and evidence matrix

| Area | Implemented mechanism | Qualification position |
|---|---|---|
| Request acceptance | Owner contracts, semantic criterion preservation, final-tree gate, full predicate results, no-op/invariant handling | Independent validation of C1–C5 remains required |
| Planning and scheduling | Rule/model planners, DAG checks, strategies, retries and bounded eligible replanning | Real-repository selection/coordination benefit is not demonstrated |
| Workspaces and integration | Git worktrees, dependency-aware bases, patch provenance, overlap/staleness checks and staging verification | Passing task patches are not a correctness theorem for their union |
| Runtime control | Phase/deadline context, cancellation, private HOME, owned-resource registry and reconciliation | R1/R2 require release-bound integrated failure evidence |
| Authority and durability | Bound approvals, run ownership/fencing, durable message deduplication | Independent authority/recovery review remains required |
| Containment | Development profile and hardened bubblewrap path with probes/refusal | Host capability and independent S1–S3 evidence are necessary |
| Providers/accounting | Mock, OpenAI-compatible and Anthropic adapters; ledger/routing/fallback paths | Protocol/model compatibility and full accounting need actual endpoint evidence |
| Inspection and replay | SQLite events/projections, artifacts, checkpoints and recorded control-plane replay | Replay does not establish fresh model behavior or undo external effects |
| Interfaces | CLI, optional local API and React dashboard | Optional dependencies and all surfaces need explicit validation |
| Tooling | Hash lock, configured CI matrix, lint and critical-path type checks | A workflow definition alone is not proof that every supported cell passed |

## Validation facts, with their scope

The recent implementation review reproduced the old exit-zero output-mismatch
case and observed a rejected contract verdict. It ran **80 focused contract,
ownership and accounting tests successfully**. Lint passed and mypy reported no
issues in **65 source files** under its configured scope.

The attempted full-suite review was stopped by a **240-second timeout**, with no
failure reported before the cutoff. That is not a full-suite pass. During this
documentation audit, collection discovered **615 tests** with the configured
importlib behavior; collection is not test execution. Older records of 159, 268,
294 or 365 passing tests describe earlier snapshots, not the current suite.

The release support matrix is Linux x86-64 and Python 3.11–3.13. The local review
used Python 3.14.4, which is a development environment rather than a qualified
release cell. See [reproduction](docs/reproduction.md) for the intended matrix.

## Measured experiments

| Evidence | Scope and limitation |
|---|---|
| Original deterministic suite | 96 mock-driven fixture runs; measures orchestration rather than model reasoning |
| Historical local workflow sweep | 62 completed trials and 2 cancellations; worker-gate flags predate newer contract semantics |
| Historical direct probes | 45 requests across eight checkpoints/device configurations; five tiny tasks and a residual-server timing caveat |
| Generated verifier corpus | Published 600 adversarial / 160 known-correct cases; author evidence with sampling assumptions requiring review |
| Later clean timing sample | Three model configurations, two repetitions and ownership checks; a mechanism demonstration, not full qualification |

Read [evaluation](docs/evaluation.md) before comparing these reports.

## Formal qualification

The checked-in assessment has **13 INCONCLUSIVE and 7 MISSING decisions, with no
PASS criteria**. Its validator awards zero formal evidence points. That does not
mean the implementation has zero engineering value. It means the policy's
release-bound independent qualification has not been established.

The record is pinned to its declared commit, which predates this documentation
baseline. It is not a live score for HEAD. Do not edit reviewer fields or claim
PASS because implementation status says complete.

## Highest-value next work

1. Reproduce critical acceptance, lifecycle and containment cases independently
   on the claimed supported release/environment.
2. Publish the preregistered held-out real-repository corpus and strong matched
   single-agent baseline, with complete accounting and uncertainty.
3. Resolve independent security findings and demonstrate actual external
   operation, recovery and inspectable failures.
4. Calibrate/restrict adaptive coordination from held-out evidence rather than
   assuming more workers improve quality or speed.

The [roadmap](docs/roadmap.md) defines delivery order. The
[scoring criteria](docs/scoring-criteria.md) define the non-negotiable gates.
No documentation update or larger test count substitutes for that evidence.
