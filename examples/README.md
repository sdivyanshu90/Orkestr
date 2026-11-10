# Example requests

Two structured request files that the deterministic mock provider can
execute end to end, with no API key and no network access.

| File | Shape | Demonstrates |
|---|---|---|
| `request-rounding.json` | Two tasks, the second depends on the first | Ordered execution, stacked worktrees, dependency-ordered patch integration, real pytest verification |
| `request-parallel.json` | Two independent tasks, disjoint files | Parallel fan-out, the case where the strategy selector should choose more than one worker |

## Running them

```bash
# Create a repository to work on
mkdir -p /tmp/demo && cd /tmp/demo && git init -b main
cat > calc.py <<'PY'
def add(a, b):
    return a + b


def subtract(a, b):
    return a - b
PY
mkdir -p tests
cat > conftest.py <<'PY'
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
PY
cat > tests/test_calc.py <<'PY'
from calc import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 2) == 3
PY
git add -A && git commit -m "initial"

# Plan without executing
agent-squad plan "Add a rounding helper" --repo . \
    --spec /home/somu/Orkestr/examples/request-rounding.json

# Execute
agent-squad run "Add a rounding helper" --repo . \
    --spec /home/somu/Orkestr/examples/request-rounding.json
```

## The request format

A request is a JSON object with an `objective` and a list of `tasks`. Each
task declares:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique within the request; used as the DAG node id |
| `title`, `objective` | yes | What the worker is being asked to do |
| `role` | no | `implementer` (default), `test_engineer`, `reviewer`, `supervisor` |
| `depends_on` | no | Task ids that must complete first |
| `edits` | no | Concrete file changes: `{path, content, mode}` |
| `acceptance_criteria` | yes | Deterministic checks the verifier re-executes |
| `anticipated_files` | no | Files this task is expected to touch; used for conflict prediction |
| `include_paths` / `exclude_paths` | no | Scope restriction enforced by the capability layer |
| `priority`, `complexity`, `estimated_duration_s` | no | Scheduling inputs |
| `max_attempts`, `token_budget`, `cost_budget_usd` | no | Per-task limits |

### Acceptance criterion kinds

| Kind | Spec | Meaning |
|---|---|---|
| `file_exists` | `path` | The file exists |
| `file_contains` | `path`, `pattern`, optional `regex` | The pattern appears in the file |
| `file_not_contains` | `path`, `pattern` | The pattern does not appear |
| `command_exits_zero` | `command` (list) | The command exits zero |
| `command_output_matches` | `command`, `pattern`, optional `regex` | The command exits zero *and* its output matches |
| `pytest_selector` | `selector`, optional `command` | pytest with the selector exits zero |
| `diff_nonempty` | — | The task produced changes |
| `diff_empty` | — | The task produced no changes |
| `llm_review` | — | Advisory only; recorded as advisory, never as passed |
| `manual` | — | Advisory only; requires a human |

A criterion with `"required": true` (the default) blocks the task when it
fails. A criterion with `"required": false` is recorded but does not block.

### Notes

- `edits` is consumed by the deterministic mock provider. A real model
  provider receives the brief and produces its own tool calls; the `edits`
  field is ignored by it.
- `depends_on` ids must exist. A cycle is rejected before execution starts.
- The `include_paths` / `exclude_paths` scope is enforced by the policy
  engine, which the agent loop consults before every tool invocation.
