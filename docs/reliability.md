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
- The scheduler decides with a hard-coded set of error-class *strings*.
  `Scheduler._is_retryable` (`src/agent_squad/scheduler/engine.py:488-502`)
  treats `"policy"`, `"validation"`, `"budget"` and `"conflict"` as
  non-retryable, and `"model"`, `"tool"`, `"timeout"`, `"infrastructure"`,
  `"unknown"` and `""` as retryable. **An error class named in neither list
  is retried**, so an unrecognised class is treated as transient.

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
| `BudgetExceeded` | `budget` | False | `agents/base.py:AgentLoop.run` (lines 146-149) *before* each model call, via `providers/budget.py:BudgetManager.check` on the `task:<id>` and `run` scopes; and (lines 185-195) after each model response, when the accumulated tokens or cost exceed the task ceiling | **Not retried** (`"budget"` is in the non-retryable set) | `FAILED` with `error_class="budget"` (`agents/base.py:241-242`) |
| `ModelError` | `model` | False | `providers/openai_compat.py:OpenAICompatProvider.complete` (non-408/409/425/429 4xx, non-JSON body, empty `choices`), `providers/anthropic.py:AnthropicProvider.complete` (same), `providers/mock.py:MockProvider.complete`, `replay/recorder.py` | **Retried** — `"model"` is not in the non-retryable set, so a permanent 401 is retried like a 429 | `FAILED` → `READY` → `RUNNING` while attempts remain |
| `TransientModelError` | `model` | True | `providers/openai_compat.py:complete` (`httpx.TimeoutException`, `httpx.HTTPError`, status 408/409/425/429/5xx), `providers/anthropic.py:complete` (same), `providers/mock.py:complete` (injected failures) | Provider fallback first (`providers/registry.py:ProviderRouter.complete` tries the next provider only for `TransientModelError`), then task-level retry | `FAILED` → `READY` → `RUNNING` while attempts remain |
| `ToolError` | `tool` | False | `sandbox/limits.py:Sandbox.check_executable` (executable not on the allow-list), `sandbox/limits.py:Sandbox.run` (empty command, missing cwd, spawn failure), `tools/base.py:ToolContext.resolve` (path escapes the workspace), `verification/checks.py:_resolve` | **Retried** (the class says not retryable; the scheduler ignores that) | `FAILED` → `READY` → `RUNNING` while attempts remain |
| `ConflictError` | `conflict` | False | **Never raised.** Conflicts are recorded as `ConflictRecord` objects in `IntegrationResult` by `integration/engine.py:IntegrationEngine.detect_overlaps` and `IntegrationEngine.integrate` | n/a | The *integration* fails; individual tasks keep their own statuses |
| `VerificationError` | `verification` | False | **Never raised.** `verification/gate.py:VerificationGate.verify_task` returns a `VerificationReport(passed=False, ...)` instead of raising | Verification failures are **always** retried while attempts remain (`scheduler/engine.py:400` sets `retryable = True` unconditionally) | `FAILED` with `error_class="verification"` → `READY` → `RUNNING` |
| `CancelledError` | `cancelled` | False | `agents/base.py:AgentLoop.run` (lines 138-139 and 209-210) when the cancellation token is set; converted to `error_class="cancelled"` at lines 239-240 | The scheduler's cancellation check at the top of the retry loop returns `CANCELLED` before another attempt starts, so it is not retried *provided the same token stays set* | `CANCELLED` (via `Scheduler._cancel_task`); if the token were cleared, `"cancelled"` is not in the non-retryable set and the attempt would be retried as a plain failure |
| `TimeoutError_` | `timeout` | True | **Never raised.** The timeout paths use the builtin `TimeoutError`, which `asyncio.TimeoutError` aliases since Python 3.11 | Handled by the scheduler's dedicated timeout branch | `TIMED_OUT` → `RETRY_SCHEDULED` → `READY` while attempts remain, else `TIMED_OUT` |
| `StaleAttemptError` | `stale` | False | `store/state.py:StateStore.transition_task` (lines 332-339) when the caller's `attempt` differs from the stored `state.attempt` | Not retried; the scheduler catches it at five call sites | The rejected write does not change the stored state. At `scheduler/engine.py:271-274` the scheduler returns its default `FAILED` outcome; at lines 301, 327, 349, 392 and 421 it suppresses the error with `contextlib.suppress(StaleAttemptError)` and still returns the status it intended |
| `InfrastructureError` | `infrastructure` | True | `store/db.py:Database.execute` (sqlite error), `artifacts/store.py:ArtifactStore.put` (write failure), `artifacts/store.py:ArtifactStore.read` (missing blob or failed integrity check), `workspace/worktree.py:git` → `GitError` (git command failed, repository has no commits) | **Retried** (`"infrastructure"` is not in the non-retryable set) | `FAILED` with `error_class="infrastructure"` (`agents/runner.py:90-98`) → `READY` → `RUNNING` |
| `AgentSquadError` subclasses raised outside the taxonomy | varies | varies | e.g. a bare `Exception` escaping the agent loop is caught by `agents/runner.py:165-172` and recorded as `error_class="unknown"` | Retried | `FAILED` → `READY` → `RUNNING` |

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

Two further edges were added to `models/task.py:LEGAL_TRANSITIONS` and are
not drawn above:

```
RUNNING ──► READY    (recovery re-queues an interrupted task)
RUNNING ──► BLOCKED  (a prerequisite failed)
```

`TaskStatus.READY` and `TaskStatus.BLOCKED` are now both reachable from
`TaskStatus.RUNNING`. The first is what makes recovery able to return an
interrupted task to the ready queue at all — without it, the
`RUNNING → READY` move that `recovery.py:recover_run` attempts would have
been rejected by `assert_transition` and the task would have been left
`RUNNING`. The second allows a running task to be blocked.

`SUCCEEDED` and `CANCELLED` are terminal (`models/task.py:TERMINAL_STATUSES`).
`FAILED` and `TIMED_OUT` are **not** terminal: `FAILED → READY` and
`TIMED_OUT → RETRY_SCHEDULED → READY` are legal, which is how retries are
expressed in the state machine.

### 2.1 Which failure leads to which state

| Failure | Path in the state machine | Code |
| --- | --- | --- |
| Worker returned `ok=False` (model error, tool error, budget, cancellation, unknown) | `RUNNING → FAILED`; then `FAILED → READY` if the error class is retryable and attempts remain | `scheduler/engine.py:348-384` |
| Task deadline elapsed | `RUNNING → TIMED_OUT`; then `TIMED_OUT → RETRY_SCHEDULED → READY` | `scheduler/engine.py:300-323` |
| An unexpected exception escaped the worker | `RUNNING → FAILED`; then `FAILED → READY` | `scheduler/engine.py:325-346` |
| Acceptance criteria failed | `RUNNING → FAILED` (error class `verification`); then `FAILED → READY` | `scheduler/engine.py:386-419` |
| A prerequisite task failed | `PENDING → BLOCKED` for every transitive dependent; `RUNNING → BLOCKED` is also legal but the scheduler does not use it | `scheduler/engine.py:170-183` via `dag/graph.py:TaskGraph.blocked_by_failure` |
| Cancellation requested | `PENDING`/`READY`/`RUNNING`/`RETRY_SCHEDULED`/`FAILED`/`TIMED_OUT`/`BLOCKED` → `CANCELLED` | `scheduler/engine.py:158-168`, `scheduler/engine.py:532-535` |
| Attempt budget exhausted | The task remains `FAILED` (or `TIMED_OUT`); `start_attempt` raises `ValidationError` on the next iteration and `Scheduler._execute` returns `FAILED` with `error_class="validation"` | `store/state.py:391-395`, `scheduler/engine.py:255-268` |
| Recovery found an interrupted task with budget left | `RUNNING`/`READY` → `READY` | `recovery.py:recover_run` (lines 161-168) |
| Recovery found an interrupted task with no budget left | `RUNNING`/`READY` → `BLOCKED` with `error_class="recovery"` | `recovery.py:recover_run` (lines 145-160) |
| `AWAITING_APPROVAL` | **Never entered by the runtime.** It is a legal target from `RUNNING` but nothing transitions into it: the approval gate blocks inside the agent loop and surfaces as a tool error with `error_class="policy"` | — |

### 2.2 Success is conditional on verification

`Scheduler._verify` (`scheduler/engine.py:495-515`) returns `None` when the
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
digest = hashlib.sha256(
    f"{self.idempotency_prefix}:{task_id}:{attempt}".encode()
).digest()
factor = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
return self.config.retry.backoff_for(attempt, factor)
```

`idempotency_prefix` is the run id (`orchestrator.py:Orchestrator.run` sets
`idempotency_prefix=self.config.run_id`).

The intent is to de-synchronise concurrent workers while keeping the delay
reproducible. **The reproducibility now holds across processes.** The
earlier revision of this document stated that the factor came from Python's
built-in `hash()` and was therefore *not* reproducible across processes,
because the hash seed for `str` is randomised per process
(`PYTHONHASHSEED`). The implementation now derives the factor from
`hashlib.sha256` over `"{prefix}:{task_id}:{attempt}"`, which is stable
across processes and across replays: the same run id, task id and attempt
always produce the same factor, in this process and in any later one.

*Remediation status: closed.* The jitter is still deterministic rather
than random — concurrent workers are de-synchronised by content, not by
luck — so the delay range in §3.1 is unchanged.

### 3.3 Where the sleep happens, and what it costs

`await asyncio.sleep(self._backoff(attempts, task.id))` appears at four
places, all inside `Scheduler._execute`:

- `scheduler/engine.py:313` — after a timeout;
- `scheduler/engine.py:336` — after an unexpected exception;
- `scheduler/engine.py:371` — after a worker failure;
- `scheduler/engine.py:402` — after a verification failure.

`_execute` runs inside an `asyncio.Task` that the scheduler keeps in its
`running` dictionary (`scheduler/engine.py:196-197`). A task that is sleeping
between attempts therefore still occupies one of the `max_workers` slots. If
every slot is occupied by a task in backoff, no new task starts until a
sleep finishes, even though nothing is executing.

### 3.4 Attempt accounting

- The scheduler's attempt cap is
  `min(task.budget.max_attempts, config.retry.max_attempts)`
  (`scheduler/engine.py:245`).
- The store's own cap is `TaskState.max_attempts`, set from
  `spec.budget.max_attempts` at `store/state.py:164` and enforced in
  `store/state.py:start_attempt` (raises `ValidationError` when
  `state.attempt >= state.max_attempts`).
- Each attempt increments the stored counter exactly once
  (`store/state.py:396`) and emits a `TASK_ATTEMPT_STARTED` event.
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
`state.attempt` and raises `StaleAttemptError` on a mismatch (lines 332-339);
`assert_transition` rejects illegal moves (`models/task.py:108`). Because the
attempt counter is monotonic and only incremented by `start_attempt`, a
superseded attempt cannot overwrite a newer result. The event append and the
projection update happen in one SQLite transaction, so the log and the
projection cannot diverge in the dangerous direction.

Two caveats:

- Fencing is **opt-in per call**. `transition_task` only fences when the
  caller passes `attempt=`. The scheduler now passes it on every transition
  it makes inside `_execute` — `RUNNING`, `TIMED_OUT`, `RETRY_SCHEDULED`,
  `READY`, `FAILED` and `SUCCEEDED` — but it omits it for the initial
  "all tasks → READY" sweep (`scheduler/engine.py:153-154`), for the
  `BLOCKED` sweep that follows a failed dependency
  (`scheduler/engine.py:172-175`), and for `Scheduler._cancel_task`
  (`scheduler/engine.py:532-535`). `recovery.py:recover_run` also calls
  `transition_task(..., READY)` without an attempt.
- `assert_transition` returns without error when `current == target`, so a
  repeated identical transition is legal and appends a second
  `TASK_STATUS_CHANGED` event. The projection is idempotent; the log is not
  free of duplicates.

### 4.2 What is NOT exactly-once

- **Any command a worker ran.** A worker that executed `run_command` and
  then died before recording the result will be retried, and the command may
  run again. `scheduler/engine.py:19-24` says so explicitly.
- **Any model call.** A retried attempt re-issues the request and re-spends
  tokens. `providers/budget.py:BudgetManager` is now instantiated by
  `orchestrator.py:Orchestrator.__init__` with the run's `BudgetConfig`, and
  it is enforced: `agents/base.py:AgentLoop.run` calls
  `budget.check(scope, extra_tokens=projected)` before every model call,
  for the per-task scope `task:<id>` and for the `"run"` scope, so a call
  that would cross a ceiling is refused rather than issued and then
  aborted. The ceilings that remain unenforced are
  `BudgetConfig.per_task_token_limit`, `per_task_cost_limit_usd` and
  `per_provider_token_limit`, which no code reads; the per-task ceilings
  that *are* applied are `TaskSpec.budget.max_tokens` and
  `max_cost_usd`, set on the task scope by
  `agents/runner.py:WorkerRunner.execute`. See §9 item 19.
- **Worktree commits.** `agents/runner.py:186-201` commits the worktree
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
  `attempt` (`store/state.py:415-423` and `scheduler/engine.py:357-368`,
  `:424-436`).
- `recovery.verify_side_effects` reconstructs a key at read time as
  `f"{task_id}#{attempt}"` and returns every attempt that started but never
  finished (`recovery.py:212-249`). It is a report, not a stored key.
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
`BEGIN` nor `COMMIT`/`ROLLBACK` (`store/db.py:186-201`). An inner failure
that is swallowed by the caller therefore leaves the outer transaction able
to commit partial work.

### 5.3 Transaction boundaries

**Atomic (event and projection in one transaction):**

| Operation | Location |
| --- | --- |
| Task status transition | `store/state.py:transition_task` (lines 307-379) |
| Attempt start (counter + owner + lease + event) | `store/state.py:start_attempt` (lines 381-424) |
| Run creation / status change | `store/state.py:create_run`, `set_run_status` |
| Task upsert | `store/state.py:upsert_task` |
| Approval creation / decision | `store/state.py:create_approval`, `decide_approval` |
| Checkpoint creation | `store/state.py:create_checkpoint` |

**Projection only, no event (not recoverable from the log):**

| Operation | Location | Consequence |
| --- | --- | --- |
| Token / cost accumulation | `store/state.py:update_usage` (lines 426-448) | `TaskState.tokens_used` and `cost_used_usd` exist only in the `tasks` table |
| Verification report | `store/state.py:set_verification` (lines 450-464) | `TaskState.verification` exists only in the `tasks` table |

**Table only, event appended elsewhere (a row can exist with no event):**

| Row | Written by | Event appended separately by |
| --- | --- | --- |
| `messages` | `store/state.py:record_message` (its own transaction) | `agents/messaging.py:MessageBus.publish` (a second transaction) |
| `artifacts` | `store/state.py:save_artifact` (its own transaction) | `agents/runner.py:211-225` (`PATCH_CAPTURED`) |

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

`store/state.py:rebuild_projection` (lines 218-305) repairs the task
projection by replaying `TASK_CREATED`, `TASK_STATUS_CHANGED` and
`TASK_ATTEMPT_STARTED` events. It reconstructs the spec, status, error class
and message, `finished_at`, attempt number, owner and `started_at`. It now
also restores `lease_expires_at` from the `lease_expires_at` payload of the
`task.attempt_started` event, so a rebuild no longer loses the lease — a
rebuilt task is therefore still recognised as holding a live lease by
`recovery.py:recover_run`. It still does **not** reconstruct `tokens_used`,
`cost_used_usd`, `verification`, `artifacts`, or the `messages` and
`artifacts` tables. A rebuild therefore still silently discards the
accounting.

*Remediation status: the lease is restored; the token, cost and
verification accounting is not.*

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
`agent-squad resume` (`cli/main.py:cmd_resume` → `recovery.resume_run`,
which calls `recovery.recover_run` first and then, if any task turned out to
be resumable, restarts the scheduler).

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

`recovery.py:resume_run(run_id, data_dir=, force=False)` is the outer
operation the CLI invokes:

1. calls `recover_run`;
2. returns `{"resumed": False, ...}` if no task is resumable, if
   `run-config.json` is missing, or if the store holds no task specs;
3. re-reads `<run>/run-config.json` into a `RunConfig` and constructs an
   `Orchestrator` over the run's data directory;
4. rebuilds the task graph from the specs in the store
   (`TaskGraph.add_all(specs)` + `graph.link()`);
5. constructs a `WorkerRunner` and a `Scheduler` over that graph — with the
   orchestrator's policy engine, approval broker and budget manager — and
   runs it;
6. returns the resulting `RunSummary`.

### 6.2 What is not automated

- **Recovery is operator-invoked only.** `Orchestrator.__init__` does not
  call `recover_run`, and nothing runs recovery at startup. `recover_run`
  itself still does not restart the scheduler; only `resume_run` does, and
  only when an operator asks for it.
- **`resume_run` only works on a run in which nothing completed.**
  `Scheduler.run` begins by transitioning *every* task in the graph to
  `READY` (`scheduler/engine.py:153-154`), and `SUCCEEDED → READY` is not a
  legal transition, so resuming a run in which one task already succeeded
  raises `ValidationError: illegal task transition succeeded -> ready`.
  The `resume_run` path is therefore exercised only by runs whose tasks all
  failed or were interrupted.
- **Re-running a run id does not resume it.** `Orchestrator.run` calls
  `state.create_run`, which now inserts the run row when it does not exist
  and updates `config_json`, `metadata` and `updated_at` when it does
  (`store/state.py:46-86`) — the run record is no longer replaced
  wholesale by an `INSERT OR REPLACE`. The run *status* is still reset
  (`Orchestrator.run` sets `RunStatus.PLANNING` immediately afterwards), and
  the run still re-plans from scratch and re-runs every task, including
  tasks that already succeeded. The task rows survive (they are keyed by
  `task_id`), but nothing consults them.
- **Recovery does not undo side effects.** A command that ran before the
  worker died is not rolled back; `verify_side_effects` only reports it.
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
| Per-task | `task.budget.deadline_s` or `config.scheduler.default_task_timeout_s` (900 s) | `asyncio.wait_for` around the whole worker execution | `scheduler/engine.py:104-106`, `:279-289` |
| Per-command | `config.sandbox.wall_time_s` (120 s) | `asyncio.wait_for(proc.communicate(...))`, then `os.killpg(proc.pid, 9)` and a 5 s grace to reap output | `sandbox/limits.py:229-247` |
| CPU time | `config.sandbox.cpu_time_s` (60 s) | `RLIMIT_CPU` → `SIGXCPU` | `sandbox/limits.py:_preexec` |
| Address space / file size | `memory_mb` (2048) / 64 MB | `RLIMIT_AS`, `RLIMIT_FSIZE` | `sandbox/limits.py:_preexec` |
| Verification command | `criterion.timeout_s` or `config.verification.command_timeout_s` (600 s) | `Sandbox.run(timeout_s=...)` | `verification/checks.py:run_criterion`, `:185` |
| Git subprocess | 120 s | `subprocess.run(timeout=120)` → `subprocess.TimeoutExpired` | `workspace/worktree.py:git` (line 51) |

On a task timeout, `TimeoutError` (`asyncio.TimeoutError` is the same class
in Python 3.11 and later) is caught at `scheduler/engine.py:290`, the task
transitions to `TIMED_OUT`, and — if attempts remain — to `RETRY_SCHEDULED`
and back to `READY`.

`RLIMIT_CPU` delivers `SIGXCPU`, which a child process may catch or ignore.
There is no escalation to `SIGKILL` on the CPU limit; the only `SIGKILL` is
the wall-clock timeout's `killpg`.

### 7.2 Cancellation

`Scheduler.cancellation` is an `asyncio.Event`
(`scheduler/engine.py:93`). It is passed down
`Scheduler._execute` → `WorkerRunner.execute(cancellation=...)` →
`AgentLoop.run(cancellation=...)`.

`AgentLoop.run` checks `cancellation.is_set()` at two points: at the top of
each iteration (`agents/base.py:138-139`) and immediately before each tool
call (`:209-210`). Both raise `agent_squad.errors.CancelledError`, which is
caught at `:239-240` and turned into `error_class="cancelled"`.

`Scheduler.run` checks the same event before dispatching each batch
(`scheduler/engine.py:158-168`) and cancels any outstanding tasks when it
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
  `:237`).
- **Cancelling `Sandbox.run` leaves the child process running.** The
  `except TimeoutError` clause in `sandbox/limits.py:234` does not catch
  `asyncio.CancelledError` (they are distinct classes), so when the outer
  `asyncio.wait_for` cancels the worker, the `killpg` is never reached and
  the subprocess is orphaned, still holding the worktree and its resources.
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
passes `attempt=` on the RUNNING, TIMED_OUT, RETRY_SCHEDULED, READY, FAILED
and SUCCEEDED transitions.

### 8.2 Stale worker writes

When a terminal transition is rejected as stale, the scheduler **suppresses**
the error and still returns the status it intended
(`scheduler/engine.py:301`, `:327`, `:349`, `:392`, `:421`). The store keeps
the newer value; the in-memory `TaskOutcome` reports the older intent. The
orchestrator then overwrites only the counters from the store
(`orchestrator.py:Orchestrator.run`, the `outcomes[state.id].tokens_used`
merge), keeping the scheduler's status. So
`RunOutcome.outcomes[task].status` can disagree with the persisted
`TaskState.status`. The same divergence exists at
`scheduler/engine.py:271-274`, where a stale RUNNING transition makes the
scheduler return its default `FAILED` outcome for a task that is in fact
running elsewhere.

### 8.3 Unfenced writes

`transition_task` without an `attempt` argument performs no fencing. The
scheduler uses unfenced calls for:

- the initial sweep of all tasks to `READY` (`scheduler/engine.py:153-154`);
- the `BLOCKED` sweep that follows a failed dependency
  (`scheduler/engine.py:172-175`);
- `Scheduler._cancel_task` (`scheduler/engine.py:532-535`).

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
(`agents/messaging.py:83-96`), so two messages published concurrently are
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
   (`scheduler/engine.py:478-493`).
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
   `sandbox/limits.py:Sandbox.run` catches `TimeoutError` (which is
   `asyncio.TimeoutError` since Python 3.11) but not
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
   Stale transitions are suppressed and the intended status is still returned
   (`scheduler/engine.py:301`, `:327`, `:349`, `:392`, `:421`).
   *Scenario:* a superseded attempt finishes after a newer attempt succeeded.
   The scheduler reports the old intent; the store reports the newer value.
   `RunOutcome.outcomes` and `StateStore.list_task_states` disagree, and the
   orchestrator's merge keeps the scheduler's status.
7. **Context trimming no longer orphans tool results.**
   *Remediation status: closed.* The earlier revision of this document
   described `AgentLoop._enforce_context_budget` popping `messages[2]`
   repeatedly and leaving the matching `tool` message behind when the
   assistant turn that declared it was removed — a transcript whose
   `tool_call_id` had no matching assistant call, which an
   OpenAI-compatible endpoint rejects with 400.
   `agents/base.py:AgentLoop._enforce_context_budget` now removes an
   assistant turn together with the tool results that answer it, via
   `AgentLoop._drop_orphan_tool_results`. Trimming a six-message transcript
   that carries `tc1` and `tc2` leaves the `tool` message for `tc1` removed
   along with its assistant turn.
   *Remaining gap:* the trimmer drops whole turns, so it can still cut a
   long tool result in the middle of a multi-turn exchange; it only
   guarantees that the transcript stays internally consistent.
8. **Recovery resumes a run, but only a run in which nothing completed.**
   `recovery.resume_run` re-reads `run-config.json` and the task specs from
   the store, rebuilds the graph and restarts the scheduler — but
   `Scheduler.run` transitions *every* task to `READY`
   (`scheduler/engine.py:153-154`), and `SUCCEEDED → READY` is not a legal
   transition, so resuming a partially completed run raises
   `ValidationError: illegal task transition succeeded -> ready`.
   `recovery.recover_run` on its own still does not restart the scheduler,
   and `Orchestrator.run` still re-plans from scratch and re-runs every
   task, including tasks that already succeeded.
   *Scenario:* a run crashes after three of five tasks succeeded.
   `agent-squad resume` reports the three as completed and sets the
   interrupted tasks to `READY`, but the resume then fails on the first
   already-succeeded task; and starting the orchestrator again with the same
   run id re-plans and re-runs all five tasks, duplicating the completed work
   and re-spending its tokens.
9. **`rebuild_projection` loses accounting.**
   It reconstructs status, errors, attempt, owner and timestamps, and now
   also the lease, but not `tokens_used`, `cost_used_usd` or `verification`.
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
18. **The approval flow reaches one action; `AWAITING_APPROVAL` is still
    unreachable as a task state.**
    *Remediation status: partly closed.* `StateStore.create_approval` is now
    called by `agents/approval.py:ApprovalBroker.request`, which creates the
    approval row, publishes an `APPROVAL_REQUEST` message and polls for a
    decision until `ApprovalConfig.approval_timeout_s` elapses.
    `TaskStatus.AWAITING_APPROVAL` is still never entered: the gate blocks
    inside `agents/base.py:AgentLoop._invoke_tool` and surfaces as a tool
    error with `error_class="policy"`, so the scheduler never sees the task
    as awaiting approval.
    The reachable action is narrow: `PolicyEngine._requires_approval`
    remaps `run_command` to `git.push`/`git.commit` only when `argv[0]` is
    literally `"git"`, so `["/usr/bin/git", "push", ...]` is not gated, and
    the four approval-gated tools (`commit_changes`, `push_changes`,
    `delete_path`, `http_request`) are not registered at all.
    *Scenario:* an operator expects a push to require approval and finds
    that a `run_command` spelled with an absolute path to `git` completes
    without any approval request.
19. **Budget ceilings are not enforced at provider scope.**
    *Remediation status: partly closed.* `providers/budget.py:BudgetManager`
    is now instantiated by `orchestrator.py:Orchestrator.__init__` with the
    run's `BudgetConfig`, and `agents/base.py:AgentLoop.run` calls
    `budget.check(scope, extra_tokens=projected)` before every model call,
    for the `task:<id>` scope and for the `"run"` scope. So
    `BudgetConfig.run_token_limit` and `run_cost_limit_usd` now have an
    effect, and the per-task ceilings come from `TaskSpec.budget`.
    `BudgetConfig.per_task_token_limit`, `per_task_cost_limit_usd` and
    `per_provider_token_limit` are still never read by any code, so there
    is no provider-level ceiling at all.
    *Scenario:* a run is configured with a run-level cost ceiling and two
    providers, one of which is expensive. The run-level ceiling stops the
    run once the projection exceeds it, but nothing stops a single provider
    from being used without limit inside that budget.
20. **A child's `CancelledError` can abort the whole run.**
    `Scheduler.run` handles a task handle whose `result()` raises
    `CancelledError` with `except Exception` (line 209), which does not catch
    `asyncio.CancelledError` in Python 3.11; the exception then reaches the
    `except asyncio.CancelledError` handler at line 223, which cancels every
    running handle and re-raises.
    *Scenario:* one worker coroutine is cancelled independently of the run;
    the entire run is torn down rather than that one task failing.
21. **Masked error classes.**
    `agents/runner.py:180-183` sets `result.error_class = result.error_class
    or "tool"` when the diff capture fails.
    *Scenario:* an attempt fails with `error_class="model"` and the diff
    capture then also fails; the recorded class stays `"model"` and the
    diff-capture failure is not visible in the state.

## 10. Fault injection

`src/agent_squad/benchmark/chaos.py` is a fault-injection harness, with
tests in `tests/chaos/test_fault_injection.py` (pytest marker `chaos`). The
point is not to "test that errors happen" but to assert the *recovery
contract*: after a given fault the run must reach a specific, named state,
and it must reach it without corrupting state or silently dropping work.

### 10.1 The catalogue

`benchmark/chaos.py:Fault` enumerates the injectable faults:

| Fault | How it is produced | Expected outcome (from the module's own catalogue) |
| --- | --- | --- |
| `provider_unavailable` | `InjectedFaultProvider` raises `ModelError` | The task fails after exhausting its retries; the run reports the failure; no state is corrupted. |
| `provider_flaky` | `InjectedFaultProvider` raises `TransientModelError` | The first N calls fail transiently; the retry policy completes the work. |
| `model_garbage` | `InjectedFaultProvider` returns a `ModelResponse` whose text is not valid JSON | The provider returns output that fails schema validation; the run fails cleanly rather than acting on malformed data. |
| `worker_crash` | `CrashyRunner` raises a bare `RuntimeError` | The worker raises an unexpected exception; the scheduler records the failure and the dependent task is blocked, not silently skipped. |
| `slow_worker` | `InjectedFaultProvider` sleeps past the task deadline | The worker exceeds its deadline; the task times out and is retried or reported as timed out. |
| `lost_message` | `ChaosHarness.drop_next_message` records a marker | A message is dropped; the run continues because no message is load-bearing for correctness. |
| `duplicate_message` | asserted directly against `agents/messaging.py:MessageBus.publish` | A message is delivered twice; the bus rejects the second copy. |
| `stale_ownership` | asserted directly against `store/state.py:StateStore.transition_task` | A late worker tries to write state for a superseded attempt; the write is fenced out. |
| `conflicting_patches` | two tasks editing the same file (`benchmark/repo_fixture.py:conflicting_spec`) | Two workers edit the same file; integration detects the conflict. |
| `stalled_task` | `InjectedFaultProvider` sleeps | A task makes no progress; the stall detector fires. |

`benchmark/chaos.py:assert_safe_failure` is the shared assertion: a failed
run must not report success, must record at least one failed task, and must
not report a task as both failed and succeeded.

### 10.2 What the harness does not cover

- **`stalled_task` has no detector to fire.** The catalogue says "the stall
  detector fires", but `config.SchedulerConfig.stall_detection_s` is
  declared and never read, and the injected fault is implemented as the same
  sleep as `slow_worker`. The fault is therefore indistinguishable from a
  slow worker: the task times out rather than being detected as stalled.
- **`lost_message` does not actually drop a message.**
  `ChaosHarness.drop_next_message` appends a marker to a list; it is not
  wired into `agents/messaging.py:MessageBus`, so no message is ever lost.
- **The faults are injected at the seams, not inside the runtime.** The
  provider wrapper and the runner wrapper stand in for the real provider and
  the real worker. A fault that can only occur inside `StateStore`,
  `EventLog` or the sandbox — a torn transaction, a half-written artifact,
  a `killpg` that does not happen — is not injectable today.
- **The approval path is not exercised.** A `REQUIRE_APPROVAL` decision
  blocks in `agents/base.py:AgentLoop._invoke_tool` until an operator
  answers; there is no fault that simulates an absent, delayed or
  conflicting decision, so the timeout behaviour of
  `ApprovalConfig.approval_timeout_s` is untested.
- **The chaos tests use the mock provider.** They exercise the scheduler,
  the store and the retry policy, not a real provider, a real sandbox or a
  real git remote.
