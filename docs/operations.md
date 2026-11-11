# Operating, inspecting and recovering runs

[Documentation home](README.md) · [Reference](reference.md) · [Reliability](reliability.md)

## Before starting

Use a repository state you can identify and recover. Review the owner criteria,
allowed paths, endpoint, budgets and isolation profile. Keep the run-data location
explicit so the CLI, API and dashboard read the same database. A clean model
server/process baseline is necessary for a meaningful timing measurement.

Normal runs produce artifacts and a patch bundle for review. They may create
internal commits in detached worktrees. They do not automatically publish the
result to a remote branch. Do not assume a target worktree is a security sandbox.

## Read a completed result

The JSON from `run` contains execution status, task outcomes, the plan, strategy,
integration information, a contract verdict when judged, and request accounting.
Check `contract.outcome` explicitly for acceptance. The CLI exit code currently
follows execution status, so exit zero is not an acceptance API.

```bash
agent-squad status RUN_ID --data-dir /path/to/run-data --json
agent-squad inspect RUN_ID --data-dir /path/to/run-data \
  --tasks --events --artifacts --decisions --json
```

Read the individual errors, failed criteria and attempt histories before retrying.
A budget exhaustion, invalid plan, policy refusal and rejected candidate need
different remedies. Repeating the same brief without changing a faulty assumption
can spend more tokens without changing the outcome.

The local API exposes `GET /api/runs/RUN_ID/contract` to inspect the original
contract and latest recorded verdict. Execution/task status and acceptance must
remain distinguishable in any automation built on these endpoints.

## Approvals and cancellation

```bash
agent-squad approve APPROVAL_ID --decision yes --data-dir /path/to/run-data
agent-squad cancel RUN_ID --data-dir /path/to/run-data
```

Check `agent-squad approve --help` for the current actor/reason flags. An approval
is bound to the requested action and context. Do not reuse a previously approved
command as a blanket grant for changed arguments, workspaces or request versions.

Cancellation is cooperative at runtime boundaries and propagated through the run
scope. The runtime also handles signals and owns worker resources. Strong timing
and cleanup guarantees still depend on the supported host and qualified failure
coverage. A process killed with SIGKILL cannot execute its own finalizers; durable
registry reconciliation is the recovery path for abandoned resources.

## Resume versus replay

```bash
agent-squad resume RUN_ID --data-dir /path/to/run-data --json
agent-squad replay RUN_ID --data-dir /path/to/run-data --json
```

**Resume** continues eligible work after recovery and run ownership checks.
Review ambiguous effects first: a command might have performed an external write
before interruption. Task-state fencing does not reverse that write or prove it
is safe to repeat. Ownership leases and heartbeat failure must be investigated
before forcing another writer into the same run.

**Replay** uses recorded model exchanges and the recorded plan/control history.
It is for reconstructing decisions and checking control-plane behavior, not for
asking today's model to generate a new solution. Missing exchanges or an
incompatible recording should be reported rather than replaced with live calls.

## Retention, backup and cleanup

Preserve the database, WAL-consistent backup, artifacts, effective configuration
and evidence required for review. Copying only `state.db` while a writer is
active can omit committed WAL content. Follow the tested backup/restore procedure
in [reproduction](reproduction.md) rather than treating an arbitrary file copy
as a consistent snapshot.

`resources.json` records owned processes/artifacts. Workspaces may be removed
or explicitly retained for debugging; a retained directory should have an owner
choice and manifest. Model caches are large and need an explicit retention
policy. Clean only paths and process groups the run owns. A global process-name
kill or a broad user cache deletion can damage unrelated work.

The supplied `scripts/cleanup_local_models.py` is specific to benchmark caches.
Run its dry-run mode first, preserve measurements, and request a receipt when
removing those downloads. An overridden/shared cache needs review. It is not a
general machine-cleaning utility. Never delete an evaluator or accepted artifact
merely to make a teardown test look clean.

## Troubleshooting by evidence

| Symptom | Inspect first | Correct next question |
|---|---|---|
| Inconclusive contract | Required outcomes, base/candidate comparisons, oracle coverage | Was success independently specified and established? |
| Output predicate failure with exit zero | Full check record and error | Did the command print what was required? |
| Context exhaustion | Effective context, prompt growth, tool output, template | Can the task fit without dropping required context? |
| Invalid structured plan/tool call | Raw response, schema and parser diagnostics | Is this a protocol or task failure? |
| Slow local inference | Process ownership, actual offload, RAM/VRAM and request queue | Is the benchmark measuring contention? |
| Refused hardened profile | Containment probe and host capabilities | Can this host enforce the promised boundaries? |
| Stale ownership | Lease, heartbeat and fencing records | Is an old owner still active or are effects ambiguous? |
| Repeated verification failure | Original contract and unchanged brief | Is a bounded plan revision appropriate? |
| Mismatched UI state | Shared data directory and API response | Is the display reading the same run? |

## Operational scope

Bind the local API to loopback. Public exposure, multi-user approval authority
and distributed execution need a different authorization/deployment design.
Store sensitive artifacts appropriately: recorded code, prompts, outputs and
paths may reveal proprietary data even when credentials are redacted.
