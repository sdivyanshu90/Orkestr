# Structured example requests

[Documentation home](../docs/README.md) · [Quickstart](../docs/getting-started.md)
· [Request concepts](../docs/concepts.md)

These examples use the deterministic mock provider to exercise real tool calls,
worktrees, verification and integration without downloading a model. They are
synthetic fixtures, not a realistic model coding benchmark.

| File | Shape | What to inspect |
|---|---|---|
| [request-rounding.json](request-rounding.json) | Implementation followed by dependent tests | Dependency-aware bases, new/preserved criteria and final candidate |
| [request-parallel.json](request-parallel.json) | Two independent tasks | Ready-set execution, actual provider concurrency and integration |

Use the [quickstart](../docs/getting-started.md) to create a disposable fixture
repository. From an activated environment and the Orkestr clone:

```bash
agent-squad plan "Add a rounding helper" --repo /path/to/disposable/repo \
  --spec /absolute/path/to/Orkestr/examples/request-rounding.json --json
agent-squad run "Add a rounding helper" --repo /path/to/disposable/repo \
  --spec /absolute/path/to/Orkestr/examples/request-rounding.json \
  --data-dir /path/to/disposable/run-data --json
```

These are placeholders, not machine-specific paths. Inspect the returned
`contract.outcome` as well as execution status. The CLI exit code is not a
substitute for request acceptance. The resulting patch bundle is reviewable;
the normal example does not push a branch.

## Request fields

The JSON object contains an objective and a task list. Each task has a unique
`id`, title and objective. Optional fields include role, dependencies, scope,
priority, complexity, estimated duration and resource/attempt limits. Typed
validation and graph checks reject invalid fields/dependencies before work.

| Field | Meaning |
|---|---|
| `depends_on` | Prerequisite task IDs; cycles and missing IDs are invalid |
| `include_paths` / `exclude_paths` | Intended allowed/excluded file scope |
| `anticipated_files` | Scheduling/conflict-estimation input, not proof of all actual edits |
| `acceptance_criteria` | Owner-supplied required/supplementary checks |
| `edits` | Concrete mock edit hints; real providers generate their own calls |
| `max_attempts`, `token_budget`, `cost_budget_usd` | Task limits subject to run policy |

Criteria IDs must not silently name different requirements in different tasks.
The contract builder validates semantic identity. A model may propose additional
checks but cannot authorize weakening an original required check.

## Check kinds

| Kind | Key specification | Meaning |
|---|---|---|
| `file_exists` | `path` | A path exists |
| `file_contains` | `path`, `pattern`, optional `regex` | Required content matches |
| `file_not_contains` | `path`, `pattern`, optional `regex` | Forbidden content is absent |
| `command_exits_zero` | `command` list | The executed command succeeds |
| `command_output_matches` | `command`, `pattern`, optional `regex` | Exit success **and** output predicate success |
| `pytest_selector` | `selector`, optional `command` | Selected tests provide passing required evidence |
| `diff_nonempty` / `diff_empty` | Empty spec | Candidate changes are present/absent under the check context |
| `llm_review` / `manual` | Advisory context | Opinion/manual requirement, not deterministic passing evidence |

A criterion's required flag affects task checks; understand how required request
outcomes are built before relying on supplementary checks for acceptance.
Selected tests that collect no evidence or skip required evidence must not be
interpreted as success merely because a command exits zero.

## Strengthen these examples for real work

The rounding fixture is not an exhaustive specification for numerical behavior.
Declare input domain, precision, overflow, negative values and expected results
with an independently owned oracle. Keep baseline regression material protected,
review candidate changes to tests/configuration, and test the final union of
patches. A worker authoring both code and a trivial test is not independent
verification.
