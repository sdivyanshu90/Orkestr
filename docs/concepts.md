# Concepts and terminology

[Documentation home](README.md) · [Project guide](project-guide.md) · [Reference](reference.md)

## Request, contract and plan

The **objective** describes the desired change in human terms. The structured
**request** supplies tasks and acceptance criteria. The **request contract** is
built from that owner input before the model proposes its plan. Required
outcomes are preserved by identity and semantics, not merely by copying their
IDs. Duplicate IDs with different meanings are invalid input.

A **plan** contains typed tasks, briefs and dependencies. A planner may add
supplementary checks or propose a different decomposition, but it cannot grant
itself authority to weaken the owner contract. A **revision** is a bounded,
recorded plan change after a classified failure; an identical retry is not a
revision. An approved contract change is different again: it changes what counts
as success and invalidates evidence bound to the old contract.

## Task DAG, strategy and attempt

A directed edge `A → B` means B depends on A. A valid DAG has no cycle or missing
prerequisite. **Ready** tasks have their prerequisites satisfied and can be
considered for dispatch; being ready does not guarantee a provider slot or GPU
capacity. A blocked prerequisite can prevent a dependent from running.

A **strategy** controls the worker count/selection policy: `single`,
`static_two`, `static_four`, or `adaptive`. A **worker** is an execution role;
an **attempt** is one bounded execution of a task. Retries consume additional
time, tokens and possibly external effects. A fencing token makes stale
ownership/attempt writes distinguishable from current writes.

The selector's critical-path estimate uses declared/estimated durations. Its
predictions are not measured speedups. Provider semaphores, model placement,
file overlap, tools and verification constrain the parallelism actually obtained.

## Worktree, patch and candidate

A **worktree** is a Git checkout for a worker or staging operation. It isolates
source trees, not operating-system authority. The worker can produce an internal
commit and a **patch** relative to a known base. Artifacts bind those changes to
revision and content identities.

The **candidate tree** is the combined result after dependency-ordered patch
integration. A patch that passes alone can fail with another patch. The normal
integration path uses a staging workspace and produces a reviewable bundle;
it does not automatically publish your branch or push a remote.

## Three different outcome vocabularies

| Vocabulary | Examples | Meaning |
|---|---|---|
| Execution/task state | pending, ready, running, succeeded, failed, blocked, cancelled | What the control plane did |
| Evidence level | worker_reported, patch_produced, validation_executed, checks_passed, integration_validated, human_approved | What kind of evidence/authorization was recorded |
| Contract verdict | accepted, rejected, inconclusive, timed_out, cancelled | Whether the original request was established by the final candidate |

The evidence ladder is not a theorem that later badges imply universal
correctness. Human approval grants authority; it is not a stronger test oracle.
The UI also represents the absence of evidence as `none`.

The contract evaluates the base and candidate to distinguish a new result from
an already true condition. The current model includes established outcomes,
preserved invariants, pre-satisfied conditions, unsatisfied requirements and
unverifiable checks. An existing invariant can remain valid while new work is
established. A fully unchanged candidate needs an explicitly permitted no-op.

## Check, oracle and protected material

A deterministic **check** is a file predicate or an executed command/test
selector. A **check record** includes its result and any error. Exit zero alone
is insufficient when a required output predicate fails. Selected tests that
collect nothing or skip required evidence must not establish success.

An **oracle** determines the expected result. Tests written by the worker are
useful regression material, but are not independently owned evidence by
construction. **Protected material** identifies evaluator files the worker may
not modify. Path policy, command mediation, containment and final-tree tamper
checks cover different parts of that boundary; post-run hashing alone does not
prove a worker could never access a secret test during execution.

`protected_material_paths` means verifier material must remain intact.
`approval.protected_paths` means writes require a human authorization decision.
They are separate configuration concepts.

## Ownership, history and effects

The durable **event log** records transitions and decisions. **Projections**
store current state for efficient queries. A **checkpoint** marks a historical
position. Content-addressed **artifacts** store evidence and model exchanges.
None of this makes the SQLite file cryptographically tamper-proof to the host
user who can edit it.

**Resume** repairs/reconstructs state and continues eligible work under run
ownership. **Replay** executes recorded decisions/exchanges without new model
reasoning. External effects can remain ambiguous after a crash: a fencing token
for task state is not an exactly-once guarantee for arbitrary external systems.

## Implementation versus qualification

A mechanism can be implemented and tested locally while its formal score remains
inconclusive. [Scoring](scoring-criteria.md) requires release-bound independent
review, realistic evaluation and operational evidence. The
[assessment record](assessment-record.yaml) describes its own pinned commit;
it is not automatically a qualification for the latest branch.
