# Limitations and non-claims

[Documentation home](README.md) · [Current status](../PROJECT_STATUS.md) · [Scoring policy](scoring-criteria.md)

This page separates implementation from proof. It describes the current local
runtime and preserves the limits of historical measurements. A feature can be
present while its qualification evidence remains missing or inconclusive.

## Acceptance is only as strong as its oracle

The request contract prevents a planner from legitimately weakening owner
criteria. The final candidate gate uses full predicate results and distinguishes
new results, preserved invariants and unsupported no-ops. These improve the
acceptance boundary; they do not turn an incomplete test into a complete
specification. Free text without independently declared outcomes cannot establish
independent request acceptance.

Worker-created tests alone are not an independent oracle. Protected paths and
post-run hashes are useful but do not, by themselves, establish that an arbitrary
worker command could not read or temporarily alter evaluator material. This
boundary needs qualified containment and independent review.

Execution status and the contract verdict remain separate. The CLI currently
uses execution status for its exit code; callers requiring acceptance must
inspect `contract.outcome`. `--no-integrate` does not perform the normal final
combined-tree judgment. A finished worker or zero exit code is not a request-
acceptance API.

## Containment depends on the execution profile and host

Git worktrees isolate source trees, not host authority. Development mode is best
effort. Hardened execution uses a real containment backend and required probes,
and refuses unsupported boundaries. A private HOME variable or a command scanner
alone is not filesystem isolation. The supported hardened environment requires
Linux capabilities beyond merely installing bubblewrap.

The provider adapter remains a host-side component with its own data/network
exposure. API authentication, multi-user approvals, public tenancy, distributed
workers and cross-host coordination are not established deployment features.
The append-oriented database is not cryptographically protected from its owner.

## Resource mechanisms require end-to-end evidence

The implementation includes phase deadlines, cancellation context, resource
registries, ownership/fencing, durable deduplication and reconciliation. This is
stronger than the older helper-only implementation. It still does not justify a
universal claim that every synchronous startup, constructor, signal, process
group or close path is bounded and safe on every host. Test the complete entry
point and declared environment.

Exactly-once task-state transitions do not make arbitrary external effects
exactly-once. Recovery can encounter commands whose effects occurred before their
acknowledgment was recorded. Such effects need idempotency or reconciliation.

Model placement is hardware/configuration dependent. Requested GPU layers and
concurrency settings are not proof of actual memory fit, offload, useful
parallelism or throughput. No hosted charge means no hosted bill, not zero local
energy/hardware cost.

## The evidence is deliberately narrower than a product guarantee

- The original deterministic benchmark uses known mock behavior and synthetic
  fixtures. It does not demonstrate model reasoning or coding improvement.
- The historical real-model workflow report records then-current worker gates,
  not the newer independent contract semantics. Do not reinterpret those flags
  as independently accepted code repairs.
- The original direct inference comparison has five tiny prompts per model/device
  configuration and a residual-server timing caveat. It is not a clean hardware
  ranking or a general model leaderboard.
- The later clean timing sample measures three configurations and verifies
  ownership. It demonstrates the mechanism, not the whole qualification study.
- Generated adversarial cases exercise selected mechanisms. Separate randomness
  and filenames do not by themselves establish independent real-world failures.
- The required held-out real-repository value study, implementation-independent
  security review and sustained external operational record remain separate
  qualification requirements.

## Reproducibility and support

The package metadata allows Python 3.11+, but the declared release matrix is
Linux x86-64 on Python 3.11–3.13. Development on another interpreter is not a
release qualification. CI configuration is not evidence that every matrix cell
passed. Optional API/dashboard dependencies also need explicit installation and
validation; there is no `api` extra in the current package metadata.

Changing source, dependencies, model/template, verifier or effective configuration
can invalidate evidence. The checked-in assessment belongs to its pinned release
and may lag HEAD. Passing local checks does not silently promote inconclusive
criteria to PASS. A complete formal assessment requires the
[strict scoring policy](scoring-criteria.md), including independent review.

## Work intentionally deferred

The selector remains a heuristic whose empirical benefit needs held-out evidence.
Bounded replanning exists, but it is not an unrestricted autonomous supervisor.
Delegation-depth fields do not establish a general runtime for arbitrary spawned
agents. Distributed scheduling, public multi-tenancy and unrestricted deployment
are outside the present qualified scope. Retention and long-history performance
need explicit policy and measurement before scale claims.

The [roadmap](roadmap.md) defines next work. This page must not be “resolved” by
deleting limitations without the corresponding code and qualifying evidence.
