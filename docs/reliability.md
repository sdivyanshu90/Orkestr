# Reliability: `agent-squad`

This document describes the failure behaviour of the runtime as it is
implemented. Where a mechanism is declared but not wired, that is stated.

All references are to files under `src/agent_squad/`.

## 1. Failure taxonomy

`src/agent_squad/errors.py` defines the taxonomy. `ErrorClass` values are
persisted in the event store and in `TaskState.error_class`, so they are part
of the on-disk contract.

### 1.1 How retryability is actually decided

There are two candidate sources of truth and only one of them is used.

- `AgentSquadError.retryable` (`errors.py:67`) is a class attribute. It is
  `True` on `TransientModelError`, `TimeoutError_` and `InfrastructureError`
  and `False` elsewhere. **It is never read by any code in the repository.**
- The scheduler decides with a hard-coded set of error-class *strings*:
  `retryable = result.error_class not in ("policy", "validation", "budget")`
  (`src/agent_squad/scheduler/engine.py:361`).

The consequence is concrete: `TransientModelError` and `ModelError` both map
to `ErrorClass.MODEL` → the string `"model"`, so the scheduler cannot tell a
429 from a 401 and retries both. The declared `retryable` attribute is
decorative.

### 1.2 The table

| Exception class | `ErrorClass` | Declared `retryable` | Detection point | Retry policy as implemented | Resulting task state |
| --- | --- | --- | --- | --- | --- |
| `AgentSquadError` (base) | `unknown` | False | — | Retryable in practice (`"unknown"` is not in the non-retryable set) | `FAILED` → `READY` → `RUNNING` while attempts remain |
| `ValidationError` | `validation` | False | Raised at the point of construction: `models/message.py:make_message` (empty sender/recipient), `models/task.py:assert_transition` (illegal transition), `store/state.py:transition_task` (unknown task), `store/state.py:start_attempt` (attempts exhausted), `dag/graph.py:TaskGraph.add` (duplicate id), `planning/planner.py:validate_plan`, `config.py:load_config` / `build_run_config`, `store/state.py:decide_approval` | **Not retried** (`"validation"` is in the non-retryable set) | `FAILED` (terminal for that attempt; `FAILED → READY` is legal but the scheduler will not take it) |
| `DependencyError` | `validation` | False | `dag/graph.py:TaskGraph.get`, `dag/graph.py:TaskGraph.validate` (unknown dependency, self-dependency, cycle, delegation-depth violation, unknown parent) | Not retried. Raised **before** the scheduler starts, during planning | No task is created; the run aborts with status `PLANNING` (see §1.3) |
| `PolicyError` | `policy` | False | Never raised. `PolicyDeniedError` (the only subclass) is defined at `security/policy.py:363` and raised nowhere | Not retried | n/a — the code path is unreachable (§5 of the security document) |
| `BudgetExceeded` | `budget` | False | `agents/base.py:AgentLoop.run` (lines 170-179) after each model response, when the accumulated tokens or cost exceed the task ceiling. Also `providers/budget.py:BudgetManager.check` — **never instantiated outside its own module** | **Not retried** (`"budget"` is in the non-retryable set) | `FAILED` with `error_class="budget"` (`agents/base.py:226-227`) |
| `ModelError` | `model` | False | `providers/openai_compat.py:OpenAICompatProvider.complete` (non-408/409/425/429 4xx, non-JSON body, empty `choices`), `providers/anthropic.py:AnthropicProvider.complete` (same), `providers/mock.py:MockProvider.complete`, `replay/recorder.py` | **Retried** — `"model"` is not in the non-retryable set, so a permanent 401 is retried like a 429 | `FAILED` → `READY` → `RUNNING` while attempts remain |
| `TransientModelError` | `model` | True | `providers/openai_compat.py:complete` (`httpx.TimeoutException`, `httpx.HTTPError`, status 408/409/425/429/5xx), `providers/anthropic.py:complete` (same), `providers/mock.py:complete` (injected failures) | Provider fallback first (`providers/registry.py:ProviderRouter.complete` tries the next provider only for `TransientModelError`), then task-level retry | `FAILED` → `READY` → `RUNNING` while attempts remain |
| `ToolError` | `tool` | False | `sandbox/limits.py:Sandbox.check_executable` (executable not on the allow-list), `sandbox/limits.py:Sandbox.run` (empty command, missing cwd, spawn failure), `tools/base.py:ToolContext.resolve` (path escapes the workspace), `verification/checks.py:_resolve` | **Retried** (the class says not retryable; the scheduler ignores that) | `FAILED` → `READY` → `RUNNING` while attempts remain |
| `ConflictError` | `conflict` | False | **Never raised.** Conflicts are recorded as `ConflictRecord` objects in `IntegrationResult` by `integration/engine.py:IntegrationEngine.detect_overlaps` and `IntegrationEngine.integrate` | n/a | The *integration* fails; individual tasks keep their own statuses |
| `VerificationError` | `verification` | False | **Never raised.** `verification/gate.py:VerificationGate.verify_task` returns a `VerificationReport(passed=False, ...)` instead of raising | Verification failures are **always** retried while attempts remain (`scheduler/engine.py:392` sets `retryable = True` unconditionally) | `FAILED` with `error_class="verification"` → `READY` → `RUNNING` |
| `CancelledError` | `cancelled` | False | `agents/base.py:AgentLoop.run` (lines 133-134 and 194-195) when the cancellation token is set; converted to `error_class="cancelled"` at line 224-225 | The scheduler's cancellation check at the top of the retry loop returns `CANCELLED` before another attempt starts, so it is not retried *provided the same token stays set* | `CANCELLED` (via `Scheduler._cancel_task`); if the token were cleared, `"cancelled"` is not in the non-retryable set and the attempt would be retried as a plain failure |
| `TimeoutError_` | `timeout` | True | **Never raised.** The timeout paths use the builtin `asyncio.TimeoutError` | Handled by the scheduler's dedicated timeout branch | `TIMED_OUT` → `RETRY_SCHEDULED` → `READY` while attempts remain, else `TIMED_OUT` |
| `StaleAttemptError` | `stale` | False | `store/state.py:StateStore.transition_task` (lines 223-230) when the caller's `attempt` differs from the stored `state.attempt` | Not retried; the scheduler catches it at five call sites | The rejected write does not change the stored state. At `scheduler/engine.py:259-260` the scheduler returns its default `FAILED` outcome; at lines 295, 323, 347, 390 and 414 it swallows the error and still returns the status it intended |
| `InfrastructureError` | `infrastructure` | True | `store/db.py:Database.execute` (sqlite error), `artifacts/store.py:ArtifactStore.put` (write failure), `artifacts/store.py:ArtifactStore.read` (missing blob or failed integrity check), `workspace/worktree.py:git` → `GitError` (git command failed, repository has no commits) | **Retried** (`"infrastructure"` is not in the non-retryable set) | `FAILED` with `error_class="infrastructure"` (`agents/runner.py:90-98`) → `READY` → `RUNNING` |
| `AgentSquadError` subclasses raised outside the taxonomy | varies | varies | e.g. a bare `Exception` escaping the agent loop is caught by `agents/runner.py:152-159` and recorded as `error_class="unknown"` | Retried | `FAILED` → `READY` → `RUNNING` |

### 1.3 Failures that happen before the scheduler starts

`Orchestrator.run` (`src/agent_squad/orchestrator.py:Orchestrator.run`) has no
try/except around planning. A `ValidationError` or `DependencyError` raised by
`planning/planner.py:LLMPlanner.plan` → `validate_plan` propagates out of
`Orchestrator.run` and is caught by `cli/main.py:cmd_run` (line 102), which
prints the error and exits with code 2. The run record is left at
`RunStatus.PLANNING` and no task rows exist. There is no retry and no
fallback to the rule-based planner.

Similarly, a `ModelError` during planning aborts the run the same way.

## 2. Task lifecycle

States are defined in `src/agent_squad/models/task.py:TaskStatus`; the legal
transition table is `models/task.py:LEGAL_TRANSITIONS` and is enforced by
`models/task.py:assert_transition`, which every write goes through
(`store/state.py:transition_task`).

```
PENDING ──► READY ──► RUNNING ──► SUCCEEDED
   │  ▲        │  ▲       │  │
   │  │        │  │       │  ├──► FAILED ──► READY | BLOCKED | CANCELLED
   │  │        │  │       │  ├──► TIMED_OUT ──► RETRY_SCHEDULED ──► READY
   │  │        │  │       │  ├──► RETRY_SCHEDULED ──► READY
   │  │        │  │       │  ├──► AWAITING_APPROVAL ──► RUNNING | FAILED | TIMED_OUT | CANCELLED
   │  └────────┘  │       │  └──► CANCELLED
   ├──► BLOCKED ◄─┴───────┘
   └──► CANCELLED
```

`SUCCEEDED` and `CANCELLED` are terminal (`models/task.py:TERMINAL_STATUSES`).
`FAILED` and `TIMED_OUT` are **not** terminal: `FAILED → READY` and
`TIMED_OUT → RETRY_SCHEDULED → READY` are legal, which is how retries are
expressed in the state machine.

### 2.1 Which failure leads to which state

| Failure | Path in the state machine | Code |
| --- | --- | --- |
| Worker returned `ok=False` (model error, tool error, budget, cancellation, unknown) | `RUNNING → FAILED`; then `FAILED → READY` if the error class is retryable and attempts remain | `scheduler/engine.py:338-375` |
| Task deadline elapsed | `RUNNING → TIMED_OUT`; then `TIMED_OUT → RETRY_SCHEDULED → READY` | `scheduler/engine.py:286-311` |
| An unexpected exception escaped the worker | `RUNNING → FAILED`; then `FAILED → READY` | `scheduler/engine.py:313-336` |
| Acceptance criteria failed | `RUNNING → FAILED` (error class `verification`); then `FAILED → READY` | `scheduler/engine.py:381-410` |
| A prerequisite task failed | `PENDING → BLOCKED` for every transitive dependent | `scheduler/engine.py:157-169` via `dag/graph.py:TaskGraph.blocked_by_failure` |
| Cancellation requested | `PENDING`/`READY`/`RUNNING`/`RETRY_SCHEDULED`/`FAILED`/`TIMED_OUT`/`BLOCKED` → `CANCELLED` | `scheduler/engine.py:150-154`, `scheduler/engine.py:485-490` |
| Attempt budget exhausted | The task remains `FAILED` (or `TIMED_OUT`); `start_attempt` raises `ValidationError` on the next iteration and `Scheduler._execute` returns `FAILED` with `error_class="validation"` | `store/state.py:365-370`, `scheduler/engine.py:241-254` |
| Recovery found an interrupted task with budget left | `RUNNING`/`READY` → `READY` | `recovery.py:recover_run` (lines 159-172) |
| Recovery found an interrupted task with no budget left | `RUNNING`/`READY` → `BLOCKED` with `error_class="recovery"` | `recovery.py:recover_run` (lines 144-158) |
| `AWAITING_APPROVAL` | **Never entered by the runtime.** It is a legal target from `RUNNING` but nothing transitions into it | — |

### 2.2 Success is conditional on verification

`Scheduler._verify` (`scheduler/engine.py:448-468`) returns `None` when the
task has no acceptance criteria or no worktree path. In that case the task
transitions straight to `SUCCEEDED` with no verification performed and
`verification=None` in the outcome. A task whose criteria are all advisory
(`llm_review`, `manual`) reaches `verification/gate.py:verify_task`, which
returns `passed=True` with `level="checks_passed"` only if no *required
deterministic* criterion failed; advisory criteria are reported as advisory
and never as passed (`models/task.py:ADVISORY_CHECKS`).

## 3. Retry and backoff

### 3.1 The backoff function

`config.py:RetryConfig.backoff_for(attempt, jitter_factor=1.0)`:

```
exponent = max(0, attempt - 1)
base     = min(initial_backoff_s * (backoff_multiplier ** exponent), max_backoff_s)
span     = base * jitter
delay    = max(0.0, base - span * jitter_factor)
```

Defaults (`config.py:RetryConfig`): `max_attempts=3`, `initial_backoff_s=0.5`,
`max_backoff_s=30.0`, `backoff_multiplier=2.0`, `jitter=0.25`.

With `jitter_factor = 1.0` the delay is `0.75 * base`; with `jitter_factor =
0.0` it is `base`. Concrete values:

| attempt | base | delay range |
| --- | --- | --- |
| 1 | 0.5 s | 0.375 – 0.5 s |
| 2 | 1.0 s | 0.75 – 1.0 s |
| 3 | 2.0 s | 1.5 – 2.0 s |
| 4 | 4.0 s | 3.0 – 4.0 s |
| 7 | 32 s → capped to 30 s | 22.5 – 30 s |

### 3.2 The jitter derivation

`scheduler/engine.py:Scheduler._backoff(attempt, task_id)`:

```python
factor = (hash((self.idempotency_prefix, task_id, attempt)) % 1000) / 1000.0
return self.config.retry.backoff_for(attempt, factor)
```

`idempotency_prefix` is the run id (`orchestrator.py:Orchestrator.run` sets
`idempotency_prefix=self.config.run_id`).

The intent is to de-synchronise concurrent workers while keeping the delay
reproducible. **The reproducibility does not hold across processes.** Python
randomises the hash seed for `str` per process (`PYTHONHASHSEED`), so
`hash(...)` for a tuple containing strings differs between the original run
and any later replay in a new process. The factor is stable within one
process only. A replay therefore reproduces the *decisions* but not the exact
sleep durations.

### 3.3 Where the sleep happens, and what it costs

`await asyncio.sleep(self._backoff(attempts, task.id))` appears at four
places, all inside `Scheduler._execute`:

- `scheduler/engine.py:301` — after a timeout;
- `scheduler/engine.py:326` — after an unexpected exception;
- `scheduler/engine.py:363` — after a worker failure;
- `scheduler/engine.py:394` — after a verification failure.

`_execute` runs inside an `asyncio.Task` that the scheduler keeps in its
`running` dictionary (`scheduler/engine.py:182-183`). A task that is sleeping
between attempts therefore still occupies one of the `max_workers` slots. If
every slot is occupied by a task in backoff, no new task starts until a
sleep finishes, even though nothing is executing.

### 3.4 Attempt accounting

- The scheduler's attempt cap is
  `min(task.budget.max_attempts, config.retry.max_attempts)`
  (`scheduler/engine.py:231`).
- The store's own cap is `TaskState.max_attempts`, set from
  `spec.budget.max_attempts` at `store/state.py:145` and enforced in
  `store/state.py:start_attempt` (raises `ValidationError` when
  `state.attempt >= state.max_attempts`).
- Each attempt increments the stored counter exactly once
  (`store/state.py:287`) and emits a `TASK_ATTEMPT_STARTED` event.
- There is no retry budget across tasks, no circuit breaker, and no
  exponential backoff on the *provider* call itself: `OpenAICompatProvider.
  complete` and `AnthropicProvider.complete` issue exactly one HTTP request
  each, and `ProviderRouter.complete` tries fallbacks without any delay
  between them.

### 3.5 The provider fallback rule

`providers/registry.py:ProviderRouter.complete` catches **only**
`TransientModelError` and moves to the next provider. A plain `ModelError`
(a 400, a 401, a non-JSON body) aborts the whole call chain immediately. The
docstring states the rationale: silently switching providers would hide a
misconfiguration. The consequence for reliability is that a misconfigured
credential is surfaced fast, but a transient failure consumes every fallback
in one attempt with no backoff between them.

## 4. Exactly-once: what is, and what is not

`scheduler/engine.py:7-27` and `store/db.py:8-24` both state the position.
The precise version follows.

### 4.1 What IS exactly-once

**Task status transitions, per (task, attempt).** `store/state.py:
transition_task` compares the caller's `attempt` argument with the stored
`state.attempt` and raises `StaleAttemptError` on a mismatch (lines 223-230);
`assert_transition` rejects illegal moves (`models/task.py:101`). Because the
attempt counter is monotonic and only incremented by `start_attempt`, a
superseded attempt cannot overwrite a newer result. The event append and the
projection update happen in one SQLite transaction, so the log and the
projection cannot diverge in the dangerous direction.

Two caveats:

- Fencing is **opt-in per call**. `transition_task` only fences when the
  caller passes `attempt=`. The scheduler omits it for the
  `RETRY_SCHEDULED`/`READY` transitions
  (`scheduler/engine.py:298-302`, `:327`, `:364`, `:395`), for the initial
  "all tasks → READY" sweep (`scheduler/engine.py:146`), and for
  `Scheduler._cancel_task` (`scheduler/engine.py:487`). `recovery.py:
  recover_run` also calls `transition_task(..., READY)` without an attempt.
- `assert_transition` returns without error when `current == target`, so a
  repeated identical transition is legal and appends a second
  `TASK_STATUS_CHANGED` event. The projection is idempotent; the log is not
  free of duplicates.

### 4.2 What is NOT exactly-once

- **Any command a worker ran.** A worker that executed `run_command` and
  then died before recording the result will be retried, and the command may
  run again. `scheduler/engine.py:19-24` says so explicitly.
- **Any model call.** A retried attempt re-issues the request and re-spends
  tokens. `providers/budget.py:BudgetManager` — which implements run, task and
  provider token/cost ceilings — is never instantiated outside its own
  module, so the `BudgetConfig` ceilings (`run_token_limit`,
  `run_cost_limit_usd`, `per_task_token_limit`, `per_provider_token_limit`)
  are not enforced anywhere. Only `task.budget.max_tokens` and
  `task.budget.max_cost_usd` are, in `agents/base.py:170-179`.
- **Worktree commits.** `agents/runner.py:180-186` commits the worktree
  before the patch artifact is written. A crash in between leaves a commit
  in the worktree with no artifact referencing it.
- **File writes.** `tools/fs.py:WriteFileTool.invoke` writes to
  `<name>.squad-tmp` and then `os.replace`s, so a partially written file is
  never visible — but the write is not deduplicated, and the temporary file
  is left behind if the process dies between `write_text` and `os.replace`.

### 4.3 Idempotency keys: where they are recorded

**There is no persisted idempotency key.** The schema
(`store/db.py:_MIGRATIONS`) has no idempotency table and no idempotency
column on any table.

What exists instead:

- **The attempt number is the de-facto key component.** The durable record
  of "attempt N started" and "attempt N finished" is the event pair
  `TASK_ATTEMPT_STARTED` / `TASK_ATTEMPT_FINISHED`, both carrying
  `attempt` (`store/state.py:306-314` and `scheduler/engine.py:349-360`,
  `:417-429`).
- `recovery.verify_side_effects` reconstructs a key at read time as
  `f"{task_id}#{attempt}"` and returns every attempt that started but never
  finished (`recovery.py:209-245`). It is a report, not a stored key.
- `Scheduler.idempotency_prefix` is used **only** to derive the backoff
  jitter. It is not attached to any operation.
- `Scheduler._task_heads` (task → head revision) is used to pick the base
  revision for dependent tasks; it is an in-memory dictionary and is lost on
  restart.

The `scheduler/engine.py:21-24` claim that "idempotency keys are recorded for
such operations" is therefore only true in the sense that the
(task, attempt) pair is durable and can be compared.

## 5. Durability model

### 5.1 SQLite configuration

`store/db.py:Database._configure` sets:

| PRAGMA | Value | Effect |
| --- | --- | --- |
| `journal_mode` | `WAL` | Concurrent readers alongside one writer; the log file is the `-wal` file |
| `synchronous` | `NORMAL` | Commits are not fsynced. Durable against process crash, **not** against host power loss |
| `foreign_keys` | `ON` | (No foreign keys are declared in the schema) |
| `busy_timeout` | `30000` | A contended writer waits up to 30 s |

The connection uses `isolation_level=None` (explicit transaction control) and
`check_same_thread=False`, with a process-wide `threading.RLock` guarding
every access.

### 5.2 Single writer

`store/db.py:Database.transaction` issues `BEGIN IMMEDIATE`, which takes the
SQLite write lock at the start of the transaction rather than on the first
write. Only one writer can be in flight at a time, across threads and across
processes. The `threading.RLock` serialises writers inside one process.

Nested use of `Database.transaction` is *not* savepoint-based: if a
transaction is already open, the inner block joins it and issues neither
`BEGIN` nor `COMMIT`/`ROLLBACK` (`store/db.py:190-201`). An inner failure
that is swallowed by the caller therefore leaves the outer transaction able
to commit partial work.

### 5.3 Transaction boundaries

**Atomic (event and projection in one transaction):**

| Operation | Location |
| --- | --- |
| Task status transition | `store/state.py:transition_task` (lines 216-270) |
| Attempt start (counter + owner + lease + event) | `store/state.py:start_attempt` (lines 275-315) |
| Run creation / status change | `store/state.py:create_run`, `set_run_status` |
| Task upsert | `store/state.py:upsert_task` |
| Approval creation / decision | `store/state.py:create_approval`, `decide_approval` |
| Checkpoint creation | `store/state.py:create_checkpoint` |

**Projection only, no event (not recoverable from the log):**

| Operation | Location | Consequence |
| --- | --- | --- |
| Token / cost accumulation | `store/state.py:update_usage` (lines 317-339) | `TaskState.tokens_used` and `cost_used_usd` exist only in the `tasks` table |
| Verification report | `store/state.py:set_verification` (lines 341-357) | `TaskState.verification` exists only in the `tasks` table |

**Table only, event appended elsewhere (a row can exist with no event):**

| Row | Written by | Event appended separately by |
| --- | --- | --- |
| `messages` | `store/state.py:record_message` (its own transaction) | `agents/messaging.py:MessageBus.publish` (a second transaction) |
| `artifacts` | `store/state.py:save_artifact` (its own transaction) | `agents/runner.py:199-211` (`PATCH_CAPTURED`) |

### 5.4 Crash between event append and projection update

For the atomic pairs above, **this cannot happen**: the event insert and the
projection update are in the same `BEGIN IMMEDIATE` transaction, so SQLite
commits both or neither. The event log is the source of truth and the
projection is derived; the reverse skew (projection ahead of log) is
impossible for those pairs.

The skew that *is* possible is the one described in §5.3: the `messages` and
`artifacts` tables can be ahead of the event log, and the projection-only
fields (`tokens_used`, `cost_used_usd`, `verification`) are not derivable from
the log at all.

`store/state.py:rebuild_projection` (lines 198-279) repairs the task
projection by replaying `TASK_CREATED`, `TASK_STATUS_CHANGED` and
`TASK_ATTEMPT_STARTED` events. It reconstructs the spec, status, error class
and message, `finished_at`, attempt number, owner and `started_at`. It does
**not** reconstruct `tokens_used`, `cost_used_usd`, `verification`,
`artifacts`, `lease_expires_at`, or the `messages` and `artifacts` tables.
A rebuild therefore silently discards the accounting.

### 5.5 Artifact durability

`artifacts/store.py:ArtifactStore.put` writes the blob to a temporary file in
the same directory, `fsync`s it, and then `os.replace`s it into place, so a
crash cannot leave a partially written blob visible under its final name.
Blobs are content-addressed by SHA-256 and `ArtifactStore.read` re-verifies
the digest on every read, raising `InfrastructureError` on a mismatch.

The *manifest* is weaker: `ArtifactStore._write_manifest` uses
`path.write_text` with no temp-file rename and no `fsync`, so a crash can
leave a truncated manifest. `ArtifactStore.load_manifest` returns `None` for
a manifest that does not parse.

## 6. Crash recovery as implemented

`src/agent_squad/recovery.py` implements recovery. It is invoked manually via
`agent-squad resume` (`cli/main.py:cmd_resume` → `recovery.recover_run`).

### 6.1 The mechanism

`recovery.py:recover_run(run_id, data_dir=, force=False)`:

1. Locates the run directory (`recovery.py:find_run`).
2. Repairs the projection: `state.rebuild_projection(run_id)`.
3. Appends `EventType.RECOVERY_STARTED`.
4. Classifies every task:
   - `SUCCEEDED` → recorded as completed, untouched;
   - `FAILED` / `CANCELLED` → skipped;
   - `RUNNING` / `READY` → *interrupted*. If `lease_expires_at` is still in
     the future and `force` is false, the task is left alone ("still holds a
     live lease; not resumed"). If `attempt >= max_attempts`, it is
     transitioned to `BLOCKED` with `error_class="recovery"`. Otherwise it is
     transitioned back to `READY` and recorded as resumable, with an entry in
     `unresolved_side_effects` warning that commands already executed "may
     have taken effect and will not be undone".
5. If any task is resumable and the run status was `cancelled` or `failed`,
   the run status is set back to `RUNNING`.
6. Appends `EventType.RECOVERY_COMPLETED`.

`recovery.py:verify_side_effects(run_id)` reports every
`(task_id, attempt)` that has a `TASK_ATTEMPT_STARTED` and no
`TASK_ATTEMPT_FINISHED`, with a derived `idempotency_key` of
`task_id#attempt`.

### 6.2 What is not automated

- **Recovery is operator-invoked only.** `Orchestrator.__init__` does not
  call `recover_run`, and nothing runs recovery at startup.
- **Recovery does not resume the scheduler.** It sets tasks back to `READY`
  in the store, but `Scheduler.run` always begins by transitioning *every*
  task to `READY` and then executing the whole graph
  (`scheduler/engine.py:145-146`); it never consults the persisted statuses
  or the leases. "Resumable" in the recovery report means "safe to re-run",
  not "will be picked up".
- **Re-running a run id does not resume it.** `Orchestrator.run` calls
  `state.create_run`, which is `INSERT OR REPLACE INTO runs`
  (`store/state.py:56-67`). Starting the orchestrator again with the same run
  id overwrites the run record, re-plans from scratch and re-runs every task
  — including tasks that already succeeded. The task rows survive (they are
  keyed by `task_id`), but nothing consults them.
- **The lease is never renewed.** `start_attempt` sets
  `lease_expires_at = now + lease_ttl_s` (`config.scheduler.lease_ttl_s`,
  default 300 s) and nothing extends it. A task that legitimately runs for
  more than 300 s looks abandoned to recovery.
- **There is no stall detector.** `config.SchedulerConfig.stall_detection_s`
  (default 300 s) is declared and never read.
- **`recover_run` is not strictly idempotent.** Running it twice appends a
  second `RECOVERY_STARTED`/`RECOVERY_COMPLETED` pair. The *state* converges
  (a `READY → READY` transition is a no-op under `assert_transition`), but
  the log grows and `rebuild_projection` is executed twice.
- **The recovery transition is unfenced.** `recover_run` calls
  `transition_task(task_id, READY)` without an `attempt` argument, so the
  attempt-fencing check is skipped.

## 7. Timeout and cancellation propagation

### 7.1 Timeouts

| Deadline | Value | Enforcement | Code |
| --- | --- | --- | --- |
| Per-task | `task.budget.deadline_s` or `config.scheduler.default_task_timeout_s` (900 s) | `asyncio.wait_for` around the whole worker execution | `scheduler/engine.py:103-106`, `:266-275` |
| Per-command | `config.sandbox.wall_time_s` (120 s) | `asyncio.wait_for(proc.communicate(...))`, then `os.killpg(proc.pid, 9)` and a 5 s grace to reap output | `sandbox/limits.py:204-222` |
| CPU time | `config.sandbox.cpu_time_s` (60 s) | `RLIMIT_CPU` → `SIGXCPU` | `sandbox/limits.py:_preexec` |
| Address space / file size | `memory_mb` (2048) / 64 MB | `RLIMIT_AS`, `RLIMIT_FSIZE` | `sandbox/limits.py:_preexec` |
| Verification command | `criterion.timeout_s` or `config.verification.command_timeout_s` (600 s) | `Sandbox.run(timeout_s=...)` | `verification/checks.py:run_criterion`, `:151` |
| Git subprocess | 120 s | `subprocess.run(timeout=120)` → `subprocess.TimeoutExpired` | `workspace/worktree.py:git` (line 51) |

On a task timeout, `asyncio.TimeoutError` is caught at
`scheduler/engine.py:276`, the task transitions to `TIMED_OUT`, and — if
attempts remain — to `RETRY_SCHEDULED` and back to `READY`.

`RLIMIT_CPU` delivers `SIGXCPU`, which a child process may catch or ignore.
There is no escalation to `SIGKILL` on the CPU limit; the only `SIGKILL` is
the wall-clock timeout's `killpg`.

### 7.2 Cancellation

`Scheduler.cancellation` is an `asyncio.Event`
(`scheduler/engine.py:92`). It is passed down
`Scheduler._execute` → `WorkerRunner.execute(cancellation=...)` →
`AgentLoop.run(cancellation=...)`.

`AgentLoop.run` checks `cancellation.is_set()` at two points: at the top of
each iteration (`agents/base.py:133-134`) and immediately before each tool
call (`:194-195`). Both raise `agent_squad.errors.CancelledError`, which is
caught at `:224-225` and turned into `error_class="cancelled"`.

`Scheduler.run` checks the same event before dispatching each batch
(`scheduler/engine.py:150-154`) and cancels any outstanding tasks when it
fires.

### 7.3 Gaps in propagation

- The token is **not** checked during a model call, during a `run_command`,
  during diff capture, or during the commit. A cancellation request takes
  effect only at the next tool boundary, so a long command is not
  interrupted.
- **`agent-squad cancel` does not stop a running orchestrator.**
  `cli/main.py:cmd_cancel` writes a `CANCELLATION_REQUESTED` event and sets
  the run status to `CANCELLED` in the database. The CLI is a separate
  process and there is no IPC, so the `asyncio.Event` in the orchestrator
  process is never set. The run continues and will overwrite the stored
  status when it finishes (`Scheduler.run` calls `state.set_run_status` at
  `:223`).
- **Cancelling `Sandbox.run` leaves the child process running.** The
  `except asyncio.TimeoutError` clause in `sandbox/limits.py:209` does not
  catch `asyncio.CancelledError`, so when the outer `asyncio.wait_for`
  cancels the worker, the `killpg` is never reached and the subprocess is
  orphaned, still holding the worktree and its resources.
- **Blocking calls stall the whole event loop.** `workspace/worktree.py:git`
  uses blocking `subprocess.run`, and every SQLite call is blocking. While
  either runs, `asyncio.wait_for` cannot fire, the cancellation token cannot
  be observed, and no other task makes progress. A single `git worktree add`
  can block the entire scheduler for up to 120 s.

## 8. Concurrency and race analysis

### 8.1 Attempt fencing

`store/state.py:start_attempt` increments `state.attempt` inside the
transaction and records `owner` and `lease_expires_at`.
`store/state.py:transition_task` compares the caller's `attempt` with the
stored value and raises `StaleAttemptError` on a mismatch. The scheduler
passes `attempt=` on the RUNNING, FAILED, TIMED_OUT and SUCCEEDED
transitions.

### 8.2 Stale worker writes

When a terminal transition is rejected as stale, the scheduler **swallows**
the error and still returns the status it intended
(`scheduler/engine.py:295`, `:323`, `:347`, `:390`, `:414`). The store keeps
the newer value; the in-memory `TaskOutcome` reports the older intent. The
orchestrator then overwrites only the counters from the store
(`orchestrator.py:Orchestrator.run`, the `outcomes[state.id].tokens_used`
merge), keeping the scheduler's status. So
`RunOutcome.outcomes[task].status` can disagree with the persisted
`TaskState.status`. The same divergence exists at
`scheduler/engine.py:259-260`, where a stale RUNNING transition makes the
scheduler return its default `FAILED` outcome for a task that is in fact
running elsewhere.

### 8.3 Unfenced writes

`transition_task` without an `attempt` argument performs no fencing. The
scheduler uses unfenced calls for:

- the initial sweep of all tasks to `READY` (`scheduler/engine.py:146`);
- the `RETRY_SCHEDULED` and `READY` transitions on the retry path
  (`:298-302`, `:327`, `:364`, `:395`);
- `Scheduler._cancel_task` (`:487`).

`recovery.recover_run` also calls it unfenced. Two schedulers running
against the same database — which `BEGIN IMMEDIATE` permits, since it
serialises writers but does not prevent two *processes* from driving the same
run — would interleave unfenced status writes.

### 8.4 Duplicate messages

`agents/messaging.py:MessageBus._validate` rejects a duplicate message id and
rejects a stale or duplicate `version` for a given
`(sender, correlation_id or task_id or id)` key. Two guarantees are missing:

- `_seen_ids` and `_versions` are **in-memory**. After a restart the
  duplicate-id and version checks no longer have any history, so a
  re-delivered message is accepted.
- `store/state.py:record_message` uses `INSERT OR REPLACE INTO messages`
  keyed on `id`, so a duplicate id silently overwrites the stored row rather
  than raising.

Dispatch happens after the lock is released
(`agents/messaging.py:83-95`), so two messages published concurrently are
validated serially but their handlers run concurrently and can interleave.

`agents/runner.py:WorkerRunner._publish_artifact` writes an
`ARTIFACT_PUBLISHED` message with `state.record_message` directly, bypassing
`MessageBus.publish` entirely. The message is therefore never validated, never
dispatched, and produces no `MESSAGE_SENT` event. `AgentLoop.bus` is
constructed and passed but never used; `agents/base.py:publish_progress` is
never called.

### 8.5 Worktree collisions

- Each attempt gets its own directory, named by `worktree_id()`
  (`workspace/worktree.py:119-120`), so two attempts of the same task never
  share a directory.
- All worktrees belong to one git repository and mutate its shared
  `.git/worktrees` metadata. `git worktree add` and `git worktree prune` are
  not safe to run concurrently: `WorktreeManager.remove` calls
  `git worktree prune` (`worktree.py:143`) with no coordination, and prune
  can remove the administrative entry of a worktree another task is in the
  middle of creating, if that directory does not yet exist.
- **Failed worktrees are kept.** `WorkerRunner._cleanup` keeps the worktree
  when the attempt failed or when `config.workspace.keep_worktrees_on_failure`
  is true (the default), so repeated failures accumulate directories and
  stale `git worktree list` entries.
- `WorktreeManager.git` inherits the full parent environment
  (`worktree.py:42-44`), so every git subprocess — including any remote
  helper or credential helper it spawns — sees the orchestrator's secrets.

### 8.6 The store is single-writer but the runtime is not single-threaded

`Database` shares one `sqlite3.Connection` across threads with
`check_same_thread=False`, guarded by a `threading.RLock`. In the current
runtime every state call happens on the event loop thread, so the lock is
uncontended. Any move to `asyncio.to_thread` would serialise on that lock,
and a contended `BEGIN IMMEDIATE` would block the calling thread for up to
`busy_timeout` (30 s) — while holding the event loop.

## 9. Known reliability gaps

Each item names the scenario that breaks it.

1. **The event loop is blocked by synchronous calls.**
   `workspace/worktree.py:git` (blocking `subprocess.run`, up to 120 s) and
   every SQLite call run on the event loop thread.
   *Scenario:* two tasks start at the same time; the second task's
   `git worktree add` blocks the whole scheduler for the duration, so the
   first task's deadline cannot fire and no other task progresses.
2. **The `retryable` attribute on exceptions is ignored.**
   The scheduler uses a hard-coded error-class set
   (`scheduler/engine.py:361`).
   *Scenario:* the provider returns 401 Unauthorized. `ModelError` (not
   retryable) is raised, but the scheduler retries the task up to
   `max_attempts` times, multiplying latency and token spend on a
   permanently broken credential. The same applies to every `ToolError`,
   including "executable not in the allow-list", which cannot succeed on a
   retry.
3. **A permanent provider error aborts fallback entirely.**
   `providers/registry.py:ProviderRouter.complete` continues only on
   `TransientModelError`.
   *Scenario:* the primary returns 400 (a malformed request that a fallback
   would accept). The whole call fails even though a working fallback is
   configured.
4. **A cancelled sandboxed command is orphaned.**
   `sandbox/limits.py:Sandbox.run` catches `asyncio.TimeoutError` but not
   `asyncio.CancelledError`.
   *Scenario:* a task deadline expires while a `pytest` run is in progress.
   The `wait_for` cancels the coroutine, the `killpg` never executes, and
   the test process keeps running, holding the worktree and the CPU.
5. **Backoff occupies a worker slot.**
   `await asyncio.sleep(...)` runs inside the task handle the scheduler
   counts against `max_workers`.
   *Scenario:* all four workers fail with a retryable error at the same
   moment. All four slots sleep for up to 30 s while nothing executes, and a
   fifth ready task cannot start.
6. **The run summary can disagree with the store.**
   Stale transitions are swallowed and the intended status is still returned
   (`scheduler/engine.py:295`, `:323`, `:347`, `:390`, `:414`).
   *Scenario:* a superseded attempt finishes after a newer attempt succeeded.
   The scheduler reports the old intent; the store reports the newer value.
   `RunOutcome.outcomes` and `StateStore.list_task_states` disagree, and the
   orchestrator's merge keeps the scheduler's status.
7. **Context trimming can orphan tool results.**
   `agents/base.py:AgentLoop._enforce_context_budget` pops `messages[2]`
   repeatedly. Index 2 is normally the assistant turn that carries
   `tool_calls`; if the token budget is satisfied by that single pop, the
   matching `tool` message survives while the assistant turn that declared it
   is gone, producing a transcript with a tool result whose `tool_call_id`
   has no matching assistant call. This is reproducible directly against
   `AgentLoop._enforce_context_budget`: trimming a six-message transcript
   removes the assistant turn for `tc1` and leaves the `tool` message with
   `tool_call_id="tc1"` in place.
   *Scenario:* a long task exceeds `max_turn_tokens` (60 000 by default).
   The trimmed transcript is sent to an OpenAI-compatible endpoint, which
   rejects it with 400. The agent fails with `error_class="model"`, which is
   retried, and fails again the same way on every attempt.
8. **Recovery does not resume anything.**
   `recovery.recover_run` marks tasks `READY`; `Scheduler.run` ignores the
   persisted statuses, and `Orchestrator.run` overwrites the run record with
   `INSERT OR REPLACE`.
   *Scenario:* a run crashes after three of five tasks succeeded.
   `agent-squad resume` reports them as completed and sets the interrupted
   tasks to `READY`, but starting the orchestrator again with the same run id
   re-plans and re-runs all five tasks, duplicating the completed work and
   re-spending its tokens.
9. **`rebuild_projection` loses accounting.**
   It reconstructs status, errors, attempt, owner and timestamps, but not
   `tokens_used`, `cost_used_usd` or `verification`.
   *Scenario:* recovery is run after a crash. The run reports zero tokens
   and zero cost for every task, and the verification results are gone.
10. **The `messages` and `artifacts` tables can be ahead of the log.**
    `record_message` and `save_artifact` commit independently of the events
    that describe them.
    *Scenario:* the process dies between the two commits. The row exists but
    the event does not, and `rebuild_projection` does not reconcile either
    table.
11. **`PRAGMA synchronous=NORMAL` is not power-loss durable.**
    *Scenario:* the host loses power. Recently committed transactions in the
    WAL may be lost, so the event log can be shorter than what the run
    actually did — the opposite of the "log is the source of truth" claim
    for the events that were committed.
12. **Failed attempts leak worktrees.**
    `WorkerRunner._cleanup` keeps the worktree on failure and
    `keep_worktrees_on_failure` defaults to true.
    *Scenario:* a task fails 3 times across 50 tasks. 50 directories and 50
    stale `git worktree list` entries accumulate, and `git worktree prune`
    runs against an increasingly large administrative directory.
13. **`git worktree prune` races with concurrent `git worktree add`.**
    *Scenario:* task A is being created while task B's cleanup prunes; the
    administrative entry for A is removed and the worktree becomes
    unmanageable by git.
14. **Integration is attempted once.**
    `Orchestrator._integrate` calls `IntegrationEngine.integrate` a single
    time; there is no retry and no re-planning on conflict.
    *Scenario:* two tasks touch the same file. The integration detects the
    overlap, records a conflict and the run ends as
    `COMPLETED_WITH_FAILURES`, even though both tasks individually passed
    their own verification.
15. **Task success can be unverified.**
    `Scheduler._verify` returns `None` when the task has no acceptance
    criteria, and the task then transitions to `SUCCEEDED` with no
    verification performed.
    *Scenario:* a plan produced without deterministic criteria (the
    rule-based planner's default) reports every task as succeeded and
    verified, with `verification=None`.
16. **Message validation state does not survive a restart.**
    `MessageBus._seen_ids` and `_versions` are in-memory.
    *Scenario:* a message is re-delivered after a restart. It is accepted and
    `INSERT OR REPLACE` overwrites the stored row.
17. **Task-level `FAILED` is not terminal but reads as one.**
    `models/task.py:TERMINAL_STATUSES` contains only `SUCCEEDED` and
    `CANCELLED`, but `Scheduler._summarise` counts `FAILED` and `TIMED_OUT`
    as failures and `Scheduler._final_status` maps any failure to
    `COMPLETED_WITH_FAILURES`.
    *Scenario:* an operator inspects the state machine, sees a task in
    `FAILED`, and assumes it will not be retried, while the store would in
    fact accept `FAILED → READY`.
18. **`AWAITING_APPROVAL` and the approval flow are unreachable.**
    `create_approval` is never called and the scheduler never enters the
    state.
    *Scenario:* an operator expects a push to require approval and finds
    that the run completes without any approval request.
19. **Budget ceilings are not enforced at run or provider scope.**
    `providers/budget.py:BudgetManager` is never instantiated;
    `BudgetConfig.run_token_limit`, `run_cost_limit_usd`,
    `per_task_token_limit` and `per_provider_token_limit` have no effect.
    *Scenario:* a run configured with a run-level cost ceiling spends far
    beyond it, because only the per-task ceilings
    (`task.budget.max_tokens`, `task.budget.max_cost_usd`) are checked, and
    only after the model call that incurred the cost.
20. **A child's `CancelledError` can abort the whole run.**
    `Scheduler.run` handles a task handle whose `result()` raises
    `CancelledError` with `except Exception` (line 195), which does not catch
    `asyncio.CancelledError` in Python 3.11; the exception then reaches the
    `except asyncio.CancelledError` handler at line 209, which cancels every
    running handle and re-raises.
    *Scenario:* one worker coroutine is cancelled independently of the run;
    the entire run is torn down rather than that one task failing.
21. **Masked error classes.**
    `agents/runner.py:167-169` sets `result.error_class = result.error_class
    or "tool"` when the diff capture fails.
    *Scenario:* an attempt fails with `error_class="model"` and the diff
    capture then also fails; the recorded class stays `"model"` and the
    diff-capture failure is not visible in the state.
