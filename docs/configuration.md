# Configuration and execution profiles

[Documentation home](README.md) · [Quickstart](getting-started.md) · [Security](security-threat-model.md)

The effective configuration is validated by
[`RunConfig`](../src/agent_squad/config.py) and written to each run's
`run-config.json`. CLI overrides are applied by the CLI loader. Inspect the
saved configuration when debugging instead of assuming a sample TOML is the
configuration that actually ran.

## A bounded offline example

```toml
# Top-level RunConfig fields come before the TOML tables.
deadline_s = 120.0
teardown_reserve_s = 5.0
isolation_profile = "development"
protected_material_paths = ["tests/test_calc.py"]

[provider]
name = "mock"
model = "mock-deterministic-v1"
max_concurrency = 1
max_output_tokens = 512

[scheduler]
max_workers = 2
max_replans = 0

[retry]
max_attempts = 2

[verification]
integration_test_command = ["python3", "-m", "pytest", "-q"]
```

Use it with `agent-squad run OBJECTIVE --repo TARGET --spec REQUEST.json
--config CONFIG.toml --json`. A deadline is a desired whole-run bound, distinct
from individual request/task timeouts. The runtime has phase-aware enforcement
and teardown handling; the scoring policy separately requires adversarial,
end-to-end proof of the declared bound.

Do not nest top-level options accidentally beneath `[provider]` or `[scheduler]`.
TOML tables continue until another table begins. Unknown or invalid configuration
should be corrected against the current Pydantic models rather than guessed.

## Configuration ownership

| Area | Controls | Important distinction |
|---|---|---|
| Top-level run fields | Deadline, isolation profile, protected material, data directory, strategy override | Not all have dedicated CLI flags |
| `provider` / `fallback_providers` | Protocol, endpoint, model, credentials, limits, pricing | Endpoint compatibility and model capability are separate |
| `roles` | Role-specific enablement, iterations, model overrides | Role presence does not prove independent review |
| `scheduler` | Workers, attempt timeout, leases, bounded replanning | Worker count is not provider concurrency |
| `retry` | Attempt/backoff limits | Retrying can repeat external effects |
| `budget` | Token, hosted-cost and context ceilings | Estimates and unavailable usage need provenance |
| `sandbox` | Worker resource/environment/containment settings | Resource limits alone are not filesystem containment |
| `verification` | Check interpreter, timeouts, integration command | A command must actually check the required behavior |
| `approval` | Protected actions/paths and approval wait | Authorization is not acceptance |
| `workspace` / `observability` | Retention, locations, logs and tracing | Retention affects artifact sensitivity and disk use |

Read the source schema for exact defaults. The files in [configs](../configs/)
are examples, not verified current prices or recommended production limits.

## Providers

The default mock consumes structured briefs and emits deterministic tool calls.
It is useful for verifying orchestration without downloading weights. It does
not infer new algorithms or supply model-quality evidence.

For a locally served OpenAI-compatible model:

```toml
[provider]
name = "openai_compat"
model = "YOUR_SERVED_MODEL"
base_url = "http://127.0.0.1:8081/v1"
max_concurrency = 1
timeout_s = 180.0
max_output_tokens = 512
temperature = 0.0
```

For an authenticated endpoint, set `api_key_env` to the name of an environment
variable, not a literal credential. The Anthropic provider speaks its own API
shape; do not point it at an OpenAI-only server and assume compatibility.
Provider aliases accepted by the registry do not establish every feature of
every service using that name.

Models can differ in structured planning, tool schemas, reasoning fields and
context limits. Start with one bounded task. A reasoning model that spends its
output budget without producing a final tool call can fail a short-turn protocol
without being incapable of the underlying task. Report the cap and template.

## Parallelism and hardware

Three limits are independent: task workers, provider request concurrency, and
simultaneously loaded models. A small GPU can be slower under multiple loaded
models because of memory pressure. Include weights, KV cache, execution buffers,
other occupants and host RAM in planning. Requested GPU layer counts are not
measurements of actual offload; inspect the server logs and telemetry.

The local benchmark's resource heuristic and its sample serving choices are
experiment configuration, not a general VRAM fit guarantee. Explicit small-model,
one-model-at-a-time configurations are easier to interpret than overcommitted
concurrent runs. Do not infer clean performance from a successful load alone.

## Development and hardened modes

`development` is best effort and appropriate for trusted fixtures. It does not
promise host filesystem isolation. A private HOME and stripped environment reduce
ambient authority, but do not by themselves stop arbitrary host-file access.

`hardened` requires the supported containment backend and boundary checks. The
implementation uses bubblewrap for namespaces, a restricted filesystem image,
a private HOME and denied network access. It must refuse unsupported boundaries.
The host must permit the required namespace operations; installing a binary alone
is insufficient. See [security](security-threat-model.md) for trust assumptions.

The model adapter runs in the orchestrator's host process, separately from worker
command containment. A worker network policy is not a promise that provider
requests never leave the machine.

## Optional API and dashboard

There is no `[api]` extra in the current project metadata. Install service
packages explicitly in an appropriate environment:

```bash
python -m pip install fastapi uvicorn
python -m agent_squad.api.server --host 127.0.0.1 --port 8080 --data-dir .agent-squad
```

This optional install is not automatically the same as the hash-locked release
procedure. Verify dependency consistency and run the optional API tests before
claiming API support. See [dashboard setup](../dashboard/README.md).
The API is a trusted local control surface. The create-run form/API is not a
complete TOML configuration editor; its default development profile is not a
substitute for a CLI configuration requiring hardened execution.
