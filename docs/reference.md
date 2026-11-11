# CLI, API and artifact reference

[Documentation home](README.md) · [Configuration](configuration.md) · [Operations](operations.md)

This reference is based on the CLI parser and API source at the documentation
baseline. Run `agent-squad COMMAND --help` for executable argument validation.
Uppercase tokens below are placeholders. Global-looking flags belong to each
subcommand; place them after its name.

## CLI commands

| Command | Function |
|---|---|
| `run OBJECTIVE` | Execute planning, workers and optional integration |
| `plan OBJECTIVE` | Inspect and propose tasks without executing worker changes |
| `status RUN_ID` | Read durable execution/task state |
| `inspect RUN_ID` | Select events, tasks, artifacts, messages, checkpoints and decisions |
| `approve APPROVAL_ID --decision yes` | Answer a pending protected-action request |
| `cancel RUN_ID` | Record/request cancellation |
| `resume RUN_ID` | Recover and continue eligible work |
| `replay RUN_ID` | Reconstruct recorded control-plane behavior |
| `benchmark` | Execute deterministic or local-model experiments |

Commands support JSON output where their parser exposes `--json`. Shared run
lookups need the same `--data-dir` used by the producer.

### `run` and `plan`

`run` accepts `--repo`, `--spec`, `--config`, `--provider`, `--model`,
`--base-url`, `--max-workers`, `--strategy`, `--description`, `--data-dir`,
`--no-integrate` and `--json`. Strategies are `single`, `static_two`,
`static_four`, and `adaptive`. The plan command has its own help output; do not
assume every run-only option applies to it.

Top-level deadline, protected material and isolation profile are configured
through TOML/RunConfig rather than invented flags such as `--deadline` or
`--hardened`. The run JSON's `status` and `contract.outcome` answer different
questions. `--no-integrate` does not establish final combined-tree acceptance.

### Inspection and replay

```bash
agent-squad inspect RUN_ID --events --artifacts --decisions --json
agent-squad inspect RUN_ID --messages --checkpoints --json
agent-squad inspect RUN_ID --export --json
agent-squad replay RUN_ID --checkpoint CHECKPOINT_ID --json
```

`--export` includes the event log as newline-delimited data within the inspection
payload. Replay needs appropriate recorded exchanges and the recorded plan; it
is not a live model benchmark or an automatic counterfactual experiment.

### Local benchmark arguments

Use `benchmark --local` with `--local-models`, `--local-scenarios`,
`--local-strategies`, `--llama-server`, `--model-dir`, `--local-base-url`,
`--local-context`, `--local-threads`, `--local-parallel-models`,
`--local-parallel-runs`, `--local-progress`, `--trials` and `--json`.
A provided base URL means an externally managed server. A server/model path
means local serving and possibly downloads; establish ownership before cleanup.
Arguments such as requested context are not proof of the effective server
configuration. Read the resulting logs and [evaluation guide](evaluation.md).

## Trusted local HTTP API

The service defaults to `127.0.0.1:8080`. It is not an authenticated public API.
Optional dependencies are explained in [configuration](configuration.md).

| Method and path | Purpose |
|---|---|
| `GET /healthz` | Service liveness |
| `GET /api/runs` | Discover stored runs |
| `POST /api/runs` | Start a background run from the supported request fields |
| `GET /api/runs/{run_id}` | Run and task details |
| `GET /api/runs/{run_id}/events` | Event history; supports incremental querying |
| `GET /api/runs/{run_id}/graph` | DAG nodes/edges |
| `GET /api/runs/{run_id}/contract` | Owner contract, latest contract verdict and outcome descriptions |
| `GET /api/runs/{run_id}/artifacts` | Artifact metadata |
| `GET /api/runs/{run_id}/messages` | Typed inter-agent messages |
| `POST /api/runs/{run_id}/cancel` | Request cancellation |
| `GET /api/approvals` | Pending approval information |
| `POST /api/approvals/{approval_id}` | Approve or deny a protected action |

The create-run payload supports `objective`, `description`, `repo_path`, `spec`,
`max_workers`, `strategy`, `provider` and `model`. It does not expose every
RunConfig option. The approval decision payload contains `approved`,
`decided_by` and `reason`; the trusted-local threat model applies to that actor
label. Inspect the runtime's generated OpenAPI description for exact schemas.

## Run data

Under `DATA_DIR/runs/RUN_ID/`, inspect the files that actually exist for that run:

| Path | Purpose |
|---|---|
| `state.db` and active SQLite WAL files | Events, projections, ownership and approvals |
| `run-config.json` | Effective serialized configuration |
| `artifacts/` | Content-addressed evidence, patches and recorded model exchanges |
| `worktrees/` | Active or explicitly retained task/staging checkouts |
| `resources.json` | Durable owned-resource registry |
| `retained-workspaces.json` when produced | Explicit retention information |
| `sandbox/home/` while active | Run-private HOME and caches |
| `patch-bundle.diff` when produced | Reviewable combined change bundle |

A data-directory listing is not proof that the run was accepted. A recorded
hash establishes content identity, not the truth of the requirements or
cryptographic protection from a host user who can edit the database.
