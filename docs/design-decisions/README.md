# Architecture decision records

[Documentation home](../README.md) · [Current architecture](../architecture.md)

These accepted records preserve the rationale of the 2026-11-10 design. Their
status means the project chose that direction; it is not independent evidence
that every current implementation guarantee is qualified. The editorial context
below each record connects the original choice to the current implementation.

| Record | Decision | Current reading context |
|---|---|---|
| [0001](0001-build-orchestration-machinery-from-scratch.md) | Own the orchestration machinery | Explicit state/authority boundaries are useful only when wired end to end |
| [0002](0002-deterministic-control-outside-the-llm.md) | Keep control decisions outside model output | Models propose plans/calls; contracts, policy and state transitions remain runtime responsibilities |
| [0003](0003-append-only-event-log-sqlite-wal.md) | Store events and projections in SQLite WAL | Recovery, fencing and durable deduplication complement the event model; the host database is not tamper-proof |
| [0004](0004-git-worktrees-plus-capability-layer.md) | Separate workspaces and capability policy | Worktrees alone are not containment; hardened execution adds an OS boundary |
| [0005](0005-independent-reexecution-of-acceptance-criteria.md) | Re-execute checks independently | The newer owner contract and final-tree gate strengthen the original task-check boundary |
| [0006](0006-deterministic-mock-provider-with-real-tool-calls.md) | Exercise real tools with a deterministic provider | Mock results isolate orchestration and must not become model-quality claims |

The historical rationale remains below the current-context notes. Source paths
and newer behavior are documented in the [walkthrough](../implementation-walkthrough.md).
A material change to a decision should get an explicit superseding record rather
than silently changing the old decision's meaning. Git retains prior wording.
