# agent-squad

**Adaptive, verifiable multi-agent software engineering runtime.**

`agent-squad` takes a repository engineering request, decomposes it into a
validated task DAG, schedules specialised agents against that DAG, isolates
their work in separate git worktrees, integrates the resulting patches under
a real verification boundary, and records enough evidence for a human to
audit what happened.

The distinguishing property of this system is not that "the agents can talk to
each other". It is that the system can **explain, enforce, recover and
empirically validate** how multiple autonomous agents cooperate on a real
engineering problem — and that it is willing to report that in some cases
they do not cooperate well at all.

---

## Why it exists

Multi-agent systems are usually sold on intuition: more agents, more
parallelism, better outcomes. That intuition is not supported by evidence,
and the overhead of coordination is real. This project treats the question
as a research problem:

> **Under which conditions can adaptive multi-agent orchestration improve
> verified task completion, latency, or cost efficiency compared with a
> well-engineered single-agent system?**

The runtime is built so that this question can be *answered with recorded
data* rather than asserted. See [`docs/benchmark-methodology.md`](docs/benchmark-methodology.md)
and [`docs/benchmark-results.md`](docs/benchmark-results.md) — including the
negative results.

---

## The verification ladder

The system distinguishes six states. They are strictly ordered and are never
conflated:

| Level | Meaning | What it does **not** prove |
|---|---|---|
| `worker_reported` | An agent said it finished | Anything. It is a claim. |
| `patch_produced` | A diff exists and its hash is known | That the diff is correct |
| `validation_executed` | The orchestrator ran the specified checks | That the checks passed |
| `checks_passed` | Those checks ran and exited zero | That the change is correct overall |
| `integration_validated` | The *combined* result of all patches was re-verified | That the system behaviour is correct |
| `human_approved` | A person authorised a protected action | — |

A test runner producing exit code 0 is a **recorded result for the specific
selector that was run**. It is not a proof of correctness, and the runtime
never describes it as one.

---

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.11+ is required. `git` must be on `PATH`.

Optional extras:

```bash
pip install -e ".[otel]"    # OpenTelemetry export
pip install -e ".[api]"     # FastAPI service + uvicorn  (also installs httpx)
```

---

## Quick start (no API key required)

The deterministic mock provider drives *real* tool calls, so the whole
pipeline — planning, worktree isolation, patching, verification, integration
— runs offline and produces identical results on every machine.

```bash
# 1. Create a small repository to work on
mkdir demo && cd demo
git init -b main
cat > calc.py <<'PY'
def add(a, b):
    return a + b
PY
git add -A && git commit -m "initial"

# 2. Describe the work
cat > request.json <<'JSON'
{
  "objective": "Add a round_half_up helper and test it",
  "tasks": [
    {
      "id": "impl",
      "title": "Implement round_half_up",
      "objective": "Add a round_half_up(value, digits=0) helper to calc.py",
      "role": "implementer",
      "depends_on": [],
      "anticipated_files": ["calc.py"],
      "edits": [{"path": "calc.py", "content": "def round_half_up(v, d=0):\n    ...\n"}],
      "acceptance_criteria": [
        {"id": "impl-file", "description": "calc.py exists",
         "kind": "file_exists", "spec": {"path": "calc.py"}},
        {"id": "impl-symbol", "description": "round_half_up is defined",
         "kind": "file_contains",
         "spec": {"path": "calc.py", "pattern": "def round_half_up"}}
      ]
    },
    {
      "id": "tests",
      "title": "Test round_half_up",
      "objective": "Cover round_half_up with unit tests",
      "role": "test_engineer",
      "depends_on": ["impl"],
      "edits": [{"path": "tests/test_rounding.py", "content": "..."}],
      "acceptance_criteria": [
        {"id": "tests-pass", "description": "the rounding tests pass",
         "kind": "pytest_selector", "spec": {"selector": "tests/test_rounding.py"}}
      ]
    }
  ]
}
JSON

# 3. Plan it (no side effects)
agent-squad plan "Add a round_half_up helper and test it" \
    --repo . --spec request.json

# 4. Run it
agent-squad run "Add a round_half_up helper and test it" \
    --repo . --spec request.json --json | jq '.summary'
```

The run writes everything under `.agent-squad/runs/<run-id>/`:

```
.agent-squad/runs/<run-id>/
├── state.db            # append-only event log + projections (SQLite, WAL)
├── run-config.json     # the exact configuration the run used
├── artifacts/          # content-addressed patches, evidence, exchanges
├── worktrees/          # per-task git worktrees
├── logs/
└── patch-bundle.diff   # a reviewable bundle; nothing is committed or pushed
```

---

## Using a real model

```bash
export OPENAI_API_KEY=...          # the key is read from the environment only
agent-squad run "Refactor the parser" \
    --repo . \
    --provider openai_compat \
    --model gpt-4o-mini \
    --base-url https://api.openai.com/v1
```

Any OpenAI-compatible endpoint works — vLLM, llama.cpp's server, Ollama,
LM Studio:

```bash
agent-squad run "..." --repo . \
    --provider openai_compat --model qwen2.5-coder \
    --base-url http://localhost:8000/v1
```

Anthropic is supported through the `anthropic` provider (see
`src/agent_squad/providers/anthropic.py`). Configuration files in TOML are
supported via `--config`; see [`configs/`](configs/).

---

## CLI

| Command | Purpose |
|---|---|
| `agent-squad run` | Execute an orchestration run |
| `agent-squad plan` | Produce and print a plan without executing it |
| `agent-squad status <run>` | Show task states, token use and cost |
| `agent-squad inspect <run>` | Inspect tasks, events, artifacts, messages, decisions |
| `agent-squad approve <id>` | Answer a pending human-approval request |
| `agent-squad cancel <run>` | Cancel a run |
| `agent-squad resume <run>` | Recover and continue an interrupted run |
| `agent-squad replay <run>` | Replay a recorded run from its event log |
| `agent-squad benchmark` | Run the orchestration efficiency laboratory |

Every command accepts `--json` for scripting.

```bash
agent-squad status  run_01JQ... --json | jq '.tasks[] | {id, status, attempts}'
agent-squad inspect run_01JQ... --events --decisions --json > trace.json
```

---

## Architecture at a glance

```
                     ┌──────────────────────────────────────┐
   request ─────────▶│  RepositoryInspector → Planner       │
                     │  (read-only)      (rule-based | LLM)  │
                     └───────────────┬──────────────────────┘
                                     │ validated TaskDAG
                     ┌───────────────▼──────────────────────┐
                     │  StrategySelector                    │
                     │  (deterministic, explainable)        │
                     └───────────────┬──────────────────────┘
                                     │ worker count
                     ┌───────────────▼──────────────────────┐
                     │  Scheduler (asyncio, bounded)        │
                     │  ready queue · retries · deadlines   │
                     └───┬───────────────┬──────────────────┘
                         │               │
            ┌────────────▼───┐      ┌────▼─────────────┐
            │  WorkerRunner  │      │  WorkerRunner    │   each in its own
            │  + AgentLoop   │      │  + AgentLoop     │   git worktree
            │  + ToolRegistry│      │  + ToolRegistry  │
            │  + PolicyEngine│      │  + PolicyEngine  │
            └────────────┬───┘      └────┬─────────────┘
                         │ patch artifact │ patch artifact
                         └───────┬────────┘
                     ┌───────────▼──────────────┐
                     │  IntegrationEngine        │
                     │  apply · detect conflicts │
                     │  re-run the test suite    │
                     └───────────┬──────────────┘
                                 │
                     ┌───────────▼──────────────┐
                     │  VerificationGate         │
                     │  re-executes criteria      │
                     └───────────┬──────────────┘
                                 │
                     ┌───────────▼──────────────┐
                     │  Event log (SQLite/WAL)   │──▶ replay · benchmark
                     │  + content-addressed      │    · dashboard · audit
                     │    artifact store          │
                     └──────────────────────────┘
```

Full detail: [`docs/architecture.md`](docs/architecture.md).

---

## Design commitments

These are enforced in code, not in prompts:

1. **Deterministic control lives outside the model.** Models propose plans,
   edits and schedules. Deterministic code decides graph validity,
   authorisation, budgets, transitions, concurrency, cancellation and
   acceptance.
2. **Every model output is untrusted.** Structured outputs are validated
   against schemas and business rules. No generated text can widen a
   permission, change a policy, or bypass an approval.
3. **Failures are first-class.** There is a typed failure taxonomy
   (`agent_squad/errors.py`); expected failures are never hidden behind a
   generic `except`.
4. **Verification is independent.** The orchestrator re-executes acceptance
   criteria itself. A reviewer's opinion is advisory and is recorded as
   advisory.
5. **Isolation is layered.** Git worktrees isolate *code*, the capability
   layer isolates *authority*, the sandbox isolates *resources*. None of the
   three alone is a security boundary; the threat model says so explicitly.
6. **Human approval is real.** Commits, pushes, deletions, protected paths
   and network access require an explicit recorded decision.

---

## Repository layout

```
src/agent_squad/
├── orchestrator.py      # composition root: one run, start to finish
├── config.py            # typed, validated, serialisable configuration
├── errors.py            # the failure taxonomy
├── ids.py               # sortable identifiers
├── models/              # task, message, artifact, evidence, run schemas
├── dag/                 # task graph: validation, ordering, scopes
├── planning/            # repository inspection, planners, task briefs
├── scheduler/           # the asyncio DAG scheduler
├── strategy/            # coordination cost model and strategy selector
├── agents/              # agent runtime, worker runner, message bus, approvals
├── tools/               # tool protocol and implementations
├── security/            # capabilities, policy engine, injection defences
├── sandbox/             # resource-limited process execution
├── workspace/           # git worktree isolation
├── integration/         # patch combination and conflict detection
├── verification/        # the independent verification gate
├── store/               # SQLite event log and projections
├── providers/           # model adapters, budgets, pricing
├── artifacts/           # content-addressed artifact store
├── observe/             # structured logs, tracing, metrics
├── replay/              # recording and replay
├── benchmark/           # the orchestration efficiency laboratory
├── api/                 # FastAPI service
└── cli/                 # the command line interface

tests/                   # unit · integration · concurrency · recovery · security · evaluation
docs/                    # architecture, ADRs, threat model, reliability, benchmarks
dashboard/               # optional React + React Flow web dashboard
```

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Components, lifecycle, state machine, persistence, trust boundaries |
| [`docs/design-decisions/`](docs/design-decisions/) | Six ADRs with the alternatives that were rejected |
| [`docs/security-threat-model.md`](docs/security-threat-model.md) | Adversaries, mitigations with code references, known weaknesses |
| [`docs/reliability.md`](docs/reliability.md) | Failure taxonomy, retry policy, durability, race analysis, known gaps |
| [`docs/benchmark-methodology.md`](docs/benchmark-methodology.md) | How the experiment is designed and what it can and cannot show |
| [`docs/benchmark-results.md`](docs/benchmark-results.md) | Measured results, including negative results |
| [`docs/limitations.md`](docs/limitations.md) | What this system does not do |
| [`docs/implementation-walkthrough.md`](docs/implementation-walkthrough.md) | A guided tour of the code, end to end |
| [`docs/failure-analysis.md`](docs/failure-analysis.md) | Failures encountered while building this, and what they taught |
| [`PROJECT_STATUS.md`](PROJECT_STATUS.md) | What is done, what is pending, what is blocked |

---

## Development

```bash
pip install -e ".[dev]"
pytest                       # the full suite
pytest -m "not slow"         # skip the slow scenarios
pytest tests/security -v     # the security regression suite
ruff check src tests
```

The suite is hermetic: no network access, no paid model API, no reliance on
wall-clock timing for correctness. Tests that measure timing assert on
*ordering of recorded events*, not on elapsed milliseconds.

---

## License

Apache-2.0.
