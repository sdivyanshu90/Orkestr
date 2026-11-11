# Technical deep dive

[Documentation home](README.md) · [Architecture](architecture.md) · [Source walkthrough](implementation-walkthrough.md)

This guide explains the mechanisms that make repository-agent execution more
than a sequence of chat messages. Paths are relative to `src/agent_squad/`.
For user-visible behavior, read [concepts](concepts.md) first.

## 1. Required semantics survive decomposition

A contract check has an identity and a meaning. Comparing only IDs would allow a
planner to keep `tests-pass` while replacing the selected tests with an existing
file check. The contract builder records semantic identity and refuses conflicting
owner IDs; the planner must preserve required criteria through initial planning
and bounded revisions.

The contract's content hash binds evidence to the request version. This reduces
accidental/stale reuse, but a hash by itself grants no authority and protects no
file from the host user. Immutability, scope enforcement and evidence validation
are different responsibilities.

### The false-acceptance lesson

A command-output check can exit zero while printing the wrong text. The low-level
executor records an error for the mismatch. The contract gate must use the
complete `CheckRecord.passed` result, not reinterpret exit code zero as success.
Otherwise a perfectly working predicate can be discarded by its caller. This
exact boundary had a reproduced defect and now has regression coverage.

Similar cases include invalid regex, missing selectors, zero collected tests,
skipped required tests and advisory-only criteria. A wrapper that loses errors
can make a stronger checker less safe than its implementation suggests.

## 2. Base/candidate comparison is about causality and invariants

Checking only the candidate cannot distinguish new work from a baseline that
already passed an old test. The contract gate evaluates both trees and considers
whether the candidate is a no-op and whether the check's subject changed.

For file predicates, the subject is the relevant file. For general commands,
the subject is broader because a command may read any repository input. The
current implementation uses content identities to support established results,
preserved invariants and pre-satisfied outcomes. Read
`verification/contract_gate.py` for the exact classification rule.

A correct request often contains both a new requirement and an old regression
invariant. Rejecting every base-passing check would reject legitimate changes.
Conversely, accepting an entirely unchanged repository because the old suite
passes would reintroduce the original false success. Explicit no-op permission
and separate invariant handling are necessary.

Content digesting is also not a semantic theorem: two different files can still
implement the same behavior, and one passing command can be incomplete. The
owner must choose a meaningful oracle.

## 3. DAG execution and dependency-aware Git bases

Graph validation establishes that scheduling has a defined dependency order.
The ready set contains tasks whose predecessors have satisfied the required
execution conditions. Dispatch respects capacity and budgets; readiness is not
an unconditional promise that work will run.

Each attempt works from a known Git base. A dependent task needs the result of
its predecessor; sibling tasks can start from the same base when independent.
The final integration order follows graph dependencies, not completion order.
This prevents a fast dependent-looking patch from being applied against an
incompatible intermediate tree merely because it arrived first.

A patch carries content and provenance. Staleness checks and overlap detection
address different questions: whether it describes the right base, whether changes
conflict, and whether the union still passes integration and request checks.
A new commit identity does not necessarily imply a different content tree.

## 4. Attempts, ownership and durable messages

A retry creates another attempt. A stale attempt must not overwrite the current
state simply because it returns late. State transitions and attempt fences make
that comparison explicit rather than trusting the timing of callbacks.

A separate run-ownership fence prevents two orchestrators from driving one run.
Acquisition is atomic, heartbeat keeps the lease live, and a takeover advances the
fence. Correct integration requires ownership-sensitive writes and effects to
consult the current authority; defining the helper is not enough.

Message IDs and versions survive restarts so deduplication does not reset with
an in-memory dictionary. Informational messages communicate observations;
state-effecting messages cross a stronger validation boundary. Delivery history
and task-state transitions need not have identical exactly-once semantics.

A crash can happen after an external effect but before its acknowledgment is
recorded. No local message ID or SQLite transaction can remove that ambiguity
from an arbitrary external service. Classify effects and require reconciliation
where automatic repetition would be unsafe.

## 5. Cancellation crosses asynchronous and synchronous seams

An absolute deadline differs from repeated per-call timeouts: many individually
legal calls can exceed a user's total allowance. `runtime/deadline.py` binds a
run scope and phase identity across planning, providers, tools, verification,
integration and teardown. Operations use the remaining budget instead of starting
new work with a fresh full timeout every time.

Synchronous operations need special care because blocking the event loop can
prevent an asynchronous cancellation timer from running. The runtime includes
phase gates and synchronous seams; qualification requires injecting a blocking
operation into each actual entry path, including startup and teardown, rather
than testing a clock helper in isolation.

Cancellation context must be released with the run. A leaked context variable
can make the next run inherit an already-cancelled token. The same concern applies
to ambient ledger and phase state: context propagation improves composition only
when the lifecycle is correctly bounded.

## 6. Resource ownership precedes cleanup

A process-name search cannot reliably distinguish a model server started by this
run from a user's existing service. The resource registry records owned process
identity/group information and artifacts while the run is alive. Cleanup sends
termination, waits, escalates when necessary, and reports survivors. Recovery can
load a prior registry instead of pretending an abandoned run owned nothing.

PID reuse, detached descendants, registry-write failure and retained workspaces
are adversarial cases, not edge cases to ignore. A successful helper-level cleanup
test does not establish that the benchmark or orchestrator actually registers
its children or calls cleanup on startup failure.

The historical timing experiment illustrated this: foreground requests were
sequential, but a server from the interrupted sweep remained resident. The later
ownership-checked sample tests teardown explicitly. Neither a comment saying
“isolated” nor a requested GPU layer count is measurement evidence.

## 7. Accounting is a ledger of attempts

A model request can fail before usage is reported, fail after some usage arrives,
be retried, or fall back to another provider. Every dispatched attempt needs a
terminal record, with provenance for reported, estimated and unavailable tokens.
Unavailable usage is null; zero would claim an observation that was not made.

Queue time belongs around provider admission, not only inside the HTTP call.
Planner and reviewer work must be counted along with worker turns. A router
wrapping a recorder must attach/adopt the same ledger rather than creating a
second empty object that looks authoritative. The ledger is exposed with the
run; older benchmark summaries have narrower worker-only accounting and retain
that historical limitation.

Cost uses configured prices. Hosted charges, estimated tokens, local electricity
and hardware amortization are different quantities. The local experiment's zero
hosted API charge is not a claim of zero resource cost.

## 8. Containment is independent of command interpretation

Tool policy can check known paths, capabilities and approval requirements. A
command scanner can reject supported protected effects and path escapes. Neither
can generally prove the behavior of arbitrary Python or shell code.

The hardened backend constructs a namespace/filesystem boundary, a private HOME,
restricted binds and a denied network. The development profile does not have the
same boundary. System-image binds, repository metadata, caches, evaluator material
and Git configuration remain part of the attack surface and need independent
review of actual commands, not only generated argument vectors.

Approvals are bound to exact parameters, workspace and request context. A changed
plan or expired grant must not authorize a different command. Host processes
running model adapters are outside worker command containment, so credential and
provider authority must be considered separately.

## 9. Replay reconstructs a decision process

A recorded response is an input to control-plane replay, not fresh intelligence.
Replay needs the original plan, task briefs and model exchanges, and executes in
scratch state. Comparing recorded outcomes can reveal drift in state or scheduling
logic. Missing inputs must remain missing, rather than triggering live model
calls that masquerade as replay.

Recovery instead reconstructs authoritative state and decides what can resume.
It may continue a failed attempt or refuse ambiguous ownership/effects. Replay
and recovery therefore have different side-effect and trust requirements.

## 10. Measurement is part of the design

A coordination heuristic needs a strong baseline, matched budgets and measured
uncertainty. Synthetic tasks that all succeed cannot demonstrate better coding
quality. A mock with negligible model latency cannot establish inference speedup.
A generated corpus tests its declared mechanisms, not a universal error rate.

The scoring policy deliberately keeps these claims separate. A locally passing
suite, typed interfaces and implemented components support an engineering review.
Held-out repository results, independent security reproduction and sustained
external use support formal qualification. Read [evaluation](evaluation.md) before
using any table as a ranking or production guarantee.
