# Implementation walkthrough

[Documentation home](README.md) · [Architecture](architecture.md) · [Deep dive](deep-dive.md)

Read this alongside the source. This is a path through the current implementation,
not a promise that every helper is exercised by every product surface. Package
paths are relative to `src/agent_squad/`.

## 1. Start at the entry points

`cli/main.py` parses commands, loads the request JSON and merges configuration
overrides. `cmd_run` constructs an orchestrator, calls its asynchronous run, and
closes it in `finally`. JSON and human-readable output expose related information,
but the exit code currently uses execution status; inspect the contract verdict
separately for acceptance.

`api/server.py` builds an optional FastAPI application. Its create-run request has
a narrower schema than RunConfig and starts work in the background. Read paths
open the same durable databases. The service is a local control surface, not an
authentication or multi-tenant authority layer.

`plan` builds provider/planner/inspection context and returns a proposal without
running worker edits. A real planner can make paid or remote model requests.

## 2. Understand configuration and persistent paths

`config.py` contains validated run, provider, scheduler, sandbox, approval and
verification options. Sample TOML is illustrative. `orchestrator.py` establishes
the run paths, saves configuration, creates private HOME/cache directories,
opens SQLite and loads/reconciles the resource registry.

Initialization has effects. Use the dedicated store/inspection interfaces when
only reading a run. Review constructor/reconciliation order when changing run
ownership so another owner's resources are not treated as abandoned prematurely.

## 3. Read the request and plan models

`models/task.py` defines tasks, scope, budgets, criteria and lifecycle rules.
`models/contract.py` defines owner outcomes and the explicit verdict vocabulary.
`planning/contract.py` derives requirements from owner input, not model output.
Semantic criterion identity prevents a reused ID from hiding a different check.

`planning/planner.py` provides deterministic and model-backed planners.
`planning/brief.py` carries task-specific context, including the mock's concrete
edit hints. `planning/replan.py` validates bounded revisions while preserving the
contract and completed work.

The distinction to trace is request → contract versus request → proposed plan.
The same model must not be allowed to weaken the former while producing the latter.

## 4. Follow the graph into scheduling

`dag/graph.py` validates dependencies and computes graph properties. The strategy
selector evaluates estimated durations and coordination/file-overlap costs.
`scheduler/engine.py` drives ready tasks, concurrency, retries, deadlines and
blocked dependents. State ownership and attempt fences protect against stale
results and competing execution.

Find where timeout and cancellation become an explicit outcome. Then find where
that outcome reaches the persisted state and downstream dependency handling.
A correctly timed-out coroutine is insufficient if its process group survives.

## 5. Trace one worker call

`agents/runner.py` creates the task workspace, registers actors/tools and builds
the role context. `agents/base.py` runs bounded model/tool turns. The loop consults
policy before executing tools, records model and tool observations, and returns
a worker result for verification and patch capture.

`tools/` contains file, command and Git operations. `security/policy.py` and
capability checks gate their use. `agents/approval.py` waits for durable human
decisions; `security/approval.py` describes bound grants. Track the exact action
and arguments rather than treating a previous “yes” as general permission.

## 6. Cross the provider boundary

`providers/registry.py` constructs adapters and routes fallbacks under per-provider
semaphores. `openai_compat.py` and `anthropic.py` translate the internal request
into their protocol shapes. `mock.py` emits deterministic calls from briefs.
`ledger.py` records attempts, phase, timing, error and usage provenance.

Follow a planner call, a worker retry and a fallback to confirm they share the
run's accounting context. Recorded model exchanges for replay are a separate
artifact responsibility; a wrapper must not disconnect the real provider ledger.

## 7. Read containment with the command runner

`sandbox/limits.py` builds the command environment, applies resource limits,
executes commands and reports timeout/isolation information.
`sandbox/containment.py` constructs the bubblewrap boundary and probes observed
properties. `security/profiles.py` chooses required capabilities and refuses
unsupported hardened execution.

Do not infer filesystem isolation from a private HOME variable or Git worktree.
Check actual bind paths, process/network namespaces, command execution and the
host's capability to enforce them. The development profile has weaker promises.

## 8. Follow evidence through both gates

`verification/checks.py` executes predicates and builds full check records.
`verification/gate.py` judges task evidence. `integration/engine.py` applies and
checks patches in staging. `verification/contract_gate.py` judges the final
candidate against the owner contract, base observations and candidate identities.
`verification/protected.py` supplies protected-material capture/tamper checking.

Start with the exit-zero output-mismatch regression: the check can fail despite
exit code zero. Trace how its error survives into the final verdict. Then read
the preserved-invariant and valid-no-op cases, where base/candidate semantics
matter more than a simple “changed file” boolean.

## 9. Inspect persistence and recovery

`store/db.py` owns schema/transactions. `store/events.py` defines the event
vocabulary and append operations. `store/state.py` exposes projections, task
transitions, ownership, approvals and message state. Artifact stores bind data
to content identities and provenance.

`recovery.py` reconstructs state and resumes eligible work under ownership.
`runtime/effects.py` classifies effects whose replay/retry may be ambiguous.
`replay/replayer.py` reconstructs recorded control execution in scratch state.
Changing these paths requires crash-boundary and schema compatibility evidence.

## 10. Close the lifecycle

`runtime/deadline.py` binds phase/deadline/cancellation scope. `runtime/registry.py`
persists owned processes and artifacts. Orchestrator teardown closes providers,
reclaims processes/workspaces and removes private HOME material. Explicit debug
retention has a different lifecycle from abandoned resources.

Read normal completion, startup failure, cancellation, signal and resume paths.
A lifecycle guarantee is a property of this whole composition, not just the
cleanup helper or a unit test that explicitly calls it.

## Tests to read next

| Concern | Starting point |
|---|---|
| Acceptance predicates and no-op/invariant semantics | `tests/evaluation/test_adversarial_contracts.py` |
| Immutable contract semantics | `tests/unit/test_contract_immutability.py` |
| Provider accounting | `tests/unit/test_provider_ledger.py`, `tests/integration/test_request_ledger.py` |
| Deadline phases | `tests/unit/test_deadline_phases.py`, `tests/integration/test_deadline_phases.py` |
| Owned resources and crash authority | `tests/recovery/test_resource_bounds.py`, `tests/recovery/test_authority_and_ownership.py` |
| Integrated teardown | `tests/lifecycle/test_lifecycle_matrix.py` |
| Containment | `tests/integration/test_hardened_containment.py` |
| Replay | `tests/integration/test_replay.py` |

Paths in this table are repository-relative. A reading list is not a claim that
the current release passed the full suite. Use [reproduction](reproduction.md)
and record the exact commands/environment when producing evidence.
