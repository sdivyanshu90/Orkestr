# Reliability and recovery model

[Documentation home](README.md) · [Operations](operations.md) · [Security](security-threat-model.md)

Reliability is about preserving correct authority, state and evidence when a run
fails. Successful completion is only one branch. The implementation contains
failure handling and test coverage; the [scoring policy](scoring-criteria.md)
sets the separate standard for an independently qualified guarantee.

## Lifecycle responsibilities

| Stage | Responsibility | Failure evidence to inspect |
|---|---|---|
| Construction/startup | Configuration, paths, private HOME, registry reconciliation | Startup exception, retained resource identities, ownership order |
| Ownership | Exclusive run lease, heartbeat and fence | Owner/fence history and stale-writer rejection |
| Planning | Valid typed graph that preserves owner outcomes | Schema/contract error and raw provider exchange |
| Dispatch/attempt | Ready dependencies, limits, retries, cancellation | Task state, attempt fence and error class |
| Provider call | Admission, protocol translation and terminal accounting | Queue/request timing, usage provenance, fallback/parse error |
| Tool command | Authority, containment and process ownership | Policy decision, sandbox result, registry and exit status |
| Verification | Complete predicate result and final candidate verdict | Check records, contract hash and candidate identity |
| Teardown | Close providers, reclaim owned processes/workspaces, remove private HOME | Cleanup report, survivors and explicit retention |
| Resume/replay | Recover authority or reconstruct recorded behavior | Recovery classification, model exchanges and comparison result |

## Failure taxonomy

A transient provider failure may justify bounded retry or fallback. An invalid
owner contract, malformed plan, policy refusal, absent containment capability or
failed required check is a different condition. Do not classify every failure as
“try the model again.” Read the runtime error class and retryability decisions.

The scheduler bounds task attempts and propagates failed prerequisites. Replanning
is a separate path for eligible verification failures and is limited by the
configured revision count, the original contract and global resources. A new
plan must not erase verified dependencies or turn a failed criterion into a
weaker requirement.

| Failure | Appropriate response |
|---|---|
| Invalid request or cyclic graph | Reject before execution; repair the input |
| Provider overload/unavailability | Bounded retry/fallback with all attempts accounted |
| Wrong output/tool schema | Preserve raw response and protocol failure; avoid silent success |
| Failed task check | Record rejection/failure; retry or replan only within policy |
| Context or budget exhaustion | Stop within declared limits and report the actual cause |
| Deadline expiry | Refuse new work; propagate cancellation and reserve teardown time |
| Ownership loss | Stop authoritative work and reject stale writes/effects |
| Unavailable hardened boundary | Refuse the run, rather than silently use development isolation |
| Interrupted external effect | Reconcile its status before repeating it |

## Whole-run time versus local timeouts

A request can stay within its HTTP timeout while hundreds of requests exceed a
user's total allowance. The runtime therefore carries an absolute run deadline
through phase-aware execution and clamps local budgets to what remains. Queueing,
planning, verification and integration matter as much as model decoding.

Asynchronous cancellation does not automatically interrupt synchronous blocking
work. Tests must inject blocking operations at the actual phase seams. Teardown
can use its reserve, but new work must not receive a fresh full timeout after the
run is exhausted. Initialization and capability probes also need explicit review
when claiming an end-to-end bound.

Signals are converted into cancellation where the runtime can do so; SIGKILL
cannot execute finalizers. A subsequent run loads the durable resource registry
and reconciles abandoned ownership/resources. A clock helper passing unit tests
is not a substitute for the integrated deadline/lifecycle matrix.

## Ownership, attempts and messages

Run ownership and task-attempt fencing solve different races. The former controls
which orchestrator may drive the run; the latter prevents a stale attempt from
overwriting a newer attempt. Heartbeats and takeover must be consistent with
side-effect dispatch, not only with database status updates.

Durable message IDs/version checks prevent restart from forgetting duplicates.
The event log and projections must agree on the visible result. A process crash
between an external effect and its acknowledgment still creates ambiguity that
local state transitions cannot resolve automatically.

## Cleanup must preserve unrelated work

Register resources when created, including model servers during startup and
command process groups during execution. Teardown must not rely on broad process
names or remove a shared cache merely because it looks like a model directory.

The registry terminates/waits/escalates and records survivors. A recorded process
must still be the process/group owned by this run; PID reuse, detached descendants,
lost registry writes and denied signals need tests. Explicitly retained debug
workspaces have an owner choice and manifest. Accepted evidence and durable
artifacts must survive cleanup even when temporary workspaces do not.

The historical benchmark left a residual server after interruption. That is why
process and port checks belong in measurement evidence, and why helper-only
cleanup tests are insufficient. See [failure analysis](failure-analysis.md).

## Recovery is not rollback

Resume reconstructs/repairs state, takes ownership and resumes eligible tasks.
The operator may need to inspect interrupted attempts and retained artifacts.
It does not automatically undo commands that reached a database, a remote API
or another external system. Idempotency must be provided by the effect/protocol
or resolved through explicit reconciliation.

Replay uses recorded exchanges and scratch state to reconstruct control behavior.
It must not mutate the original completed run or replace missing recordings with
new live calls. A replay match says something about the recorded execution, not
about a new endpoint or a future repository state.

## Persistence, backup and schema changes

SQLite WAL stores events and materialized state. The append-oriented event model
supports reconstruction, but the file is not tamper-proof against the host owner.
Back up a consistent database/WAL state using the tested procedure. Migrations,
restore and replay compatibility need their own evidence.

Retention should preserve accepted-request evidence, artifact identity and
recovery context. Snapshot/compaction behavior must not be assumed merely because
the roadmap proposes it. A long-lived run can accumulate history and require
an explicit storage policy.

## Qualification and residual risk

The code includes an integrated lifecycle matrix, phase tests, ownership tests
and fault injection. They must be run in the claimed environment and reviewed
against the exact release. A timeout, filtered suite or unsupported containment
skip is not proof of a full pass. Constructor, synchronous close and exceptional
paths deserve specific review rather than inheriting guarantees from `aclose()`.

The current assessment leaves reliability criteria inconclusive pending qualified
evidence. Keep that distinction when explaining a successful local test. The
[operations guide](operations.md) describes what an operator can inspect today;
[reproduction](reproduction.md) describes how to create reviewable evidence.
