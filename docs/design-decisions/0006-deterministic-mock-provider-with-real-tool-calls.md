# 0006. Deterministic mock provider that emits real tool calls

- Status: Accepted
- Date: 2026-11-10

## Context

The system's behaviour lives in the parts that are hardest to observe: a
worker editing files in a worktree, running a test suite, producing a diff
that then has to be integrated with another worker's diff, and passing a
verification gate that re-runs the checks. These are precisely the parts
that fail in practice — wrong base revisions, untracked files missing from a
diff, patches that conflict, tests that pass in isolation and fail together.

To develop, test and benchmark any of that, the system needs a model
provider that can be driven without network access, without API keys, without
spending tokens, and — for benchmarking — *identically on every run*. A
provider that returns canned text satisfies none of the real requirements:

* A provider that returns a fixed string produces no tool calls, so no files
  change, no diff is produced, no patch artifact exists, the integration
  stage has nothing to integrate, and the verification gate has nothing to
  verify. The entire downstream half of the system is untested.
* A provider whose output varies per run makes it impossible to tell a real
  regression from provider nondeterminism.
* A provider that talks but does not act forces the test suite to assert on
  text, which is exactly the assertion style this project is trying to move
  away from.

There is also a research requirement. `strategy/heuristics.py` exists to
estimate the coordination cost of parallelism, and `strategy/selector.py`
uses it to choose a worker count. To know whether the heuristic is right, the
benchmark harness must run the same task set under several strategies and
compare outcomes. That requires a provider whose output does not change
between trials — otherwise the measurement is dominated by model variance and
cannot isolate the effect of the strategy. This is the concrete form of the
orchestrator's stated position: "Two runs with the same configuration and
the same inputs produce the same *control-flow decisions*; they do not
produce the same model outputs, and the run records the difference." The
mock provider is what makes the control plane measurable.

At the same time, a mock that is too simple stops exercising the code. If
the mock only writes files, the diff-capture path, the patch artifact, the
evidence package, the integration engine and the conflict detection are all
left untested. If it only writes files and never runs a command, the
`pytest_selector` and `command_exits_zero` check kinds are never exercised
in an end-to-end run.

Finally, failure has to be injectable. The scheduler's retry policy, the
provider fallback router, the recovery path and the timeout path all need to
be testable, and none of them can be driven by a well-behaved provider.

## Decision

**`providers/mock.py` implements the same `ModelProvider` protocol as the
HTTP adapters, is fully deterministic, parses the structured task brief out
of the prompt, and emits genuine tool calls — `write_file`, `run_command`,
`git_diff`, `git_status` — that the real tool layer really executes.**

### It is a provider, not a test double

`MockProvider` subclasses `BaseProvider` and implements
`async def complete(self, request: ModelRequest) -> ModelResponse` and
`aclose`. It is constructed by `providers/registry.py`'s `build_provider`
when `ProviderConfig.name == "mock"`, exactly like the OpenAI-compatible and
Anthropic providers. It advertises
`ProviderCapabilities(supports_tools=True, supports_system_prompt=True,
supports_seed=True, supports_parallel_tool_calls=False,
supports_json_schema=True)`.

### It reads the brief, not a script

The prompt the worker receives contains a structured block delimited by
`BRIEF_START` / `BRIEF_END`, rendered by `TaskBrief.to_prompt_block()` as
JSON. `providers/mock.py` defines the same delimiters and
`extract_brief(text)` parses that block back out. `MockProvider._find_brief`
scans the request's `user` and `system` messages from the end and returns
the first brief it finds.

The brief is a first-class structured contract, not a prompt: `planning/
brief.py` defines `TaskBrief` with `task_id`, `role`, `objective`,
`description`, `base_revision`, `scope`, `acceptance_criteria`, `edits`
(a tuple of `Edit(path, content, mode)`), `commands`, `notes`, `depends_on`
and `context_refs`. `Edit` validates that the path is relative and
contained. This is the mechanism that lets the mock produce genuine file
changes rather than vague text.

### It emits real tool calls

`MockProvider._worker_response` translates the brief into tool calls:

* each entry in `brief["edits"]` becomes a `write_file` call with the
  requested `path`, `content` and `mode` (`write` or `append`);
* each entry in `brief["commands"]` becomes a `run_command` call with the
  argument list.

Those calls travel through the ordinary path: `AgentLoop._invoke_tool`
looks the tool up in the `ToolRegistry` built by `WorkerRunner.build_tools`,
the `ToolContext` resolves paths inside the worktree, `WriteFileTool` writes
the file atomically (temp file plus `os.replace`), and `RunCommandTool`
executes through the `Sandbox` with its rlimits, timeout, output cap and
executable allow-list.

The mock also behaves like a well-mannered agent: `_worker_response` checks
`already_ran = any(m.role == "tool" for m in request.messages)` and, once a
tool result is in the transcript, stops proposing edits and returns a
summary instead. That produces the two-turn shape a real agent produces.

Role-specific behaviour exists: `_reviewer_response` proposes
`git diff --stat` and `git status --porcelain` (and no writes, matching the
reviewer's read-only tool registry), and `_supervisor_response` returns a
serialised plan. The mock's role comes from `brief["role"]`.

### It is deterministic in output and honest about usage

`_usage` computes `Usage` from `estimate_tokens` over the request and the
response text and sets `estimated=True`. The `Usage.estimated` flag
distinguishes locally-estimated numbers from provider-reported ones, so the
mock participates in budgeting without pretending to be exact.

### Failure injection is a first-class feature

`MockProvider.__init__` takes `failures`, `failure_error`, `unavailable`,
`latency_s` and `scripted`. `complete` raises the configured error on the
Nth call, raises `ModelError` when `unavailable` is set, sleeps for
`latency_s`, and otherwise pops from `scripted` if a script was supplied.
`set_failures` and `set_unavailable` allow the conditions to change
mid-run. This is what makes the retry path, the provider fallback router
(`ProviderRouter` only falls back on `TransientModelError`), the timeout
path and the recovery path testable without a real endpoint.

### The consequences ripple through the whole system, by design

Because the mock's tool calls are real, an offline run exercises:
`RuleBasedPlanner` (selected automatically when
`config.provider.name == "mock"`), the scheduler's ready queue and
concurrency limit, worktree creation and disposal, `git add -A` plus
`git diff --cached` (so new files are captured), the patch artifact and its
SHA-256, the `EvidencePackage` construction, the verification gate
re-executing `file_exists`, `file_contains`, `diff_nonempty` and
`pytest_selector` criteria, the integration engine applying two patches in
dependency order with `git apply --3way` and running the combined test
suite, and the patch bundle. The end-to-end test asserts exactly this: two
tasks succeeded at level `checks_passed`, the `pytest_selector` check really
ran with exit code 0, the integration applied two patches with zero
conflicts, and the main repository's file was left unmodified.

## Alternatives considered

- **A provider that returns canned text.** Rejected outright: it exercises
  none of the downstream machinery and would let the integration and
  verification layers ship untested. This is the failure mode this decision
  exists to prevent.
- **A scripted provider (a fixed list of responses per call index).**
  Rejected as the primary mode: it does not scale with the number of tasks,
  it must be rewritten for every new scenario, and it produces the same
  behaviour for every task in the run rather than behaviour derived from
  the task. `scripted` is retained as an *additional* injection mechanism
  for tests that need an exact transcript.
- **Recording real provider traffic and replaying it.** Rejected as the
  default because it requires network access to produce, is expensive to
  refresh, is bound to one model's behaviour at one moment, and cannot
  express a *new* task specification. The project does have the machinery
  for this — `replay/recorder.py` records exchanges as artifacts and
  `replay/replayer.py` replays the control plane against them — but it is a
  different feature with a different purpose.
- **Faking the tool layer too (the provider returns "the file was written"
  and the runtime pretends).** Rejected: it would leave the diff capture,
  the artifact hashing, the patch application and the verification checks
  untested, which are exactly the parts most likely to be wrong.
- **Using a real local model (a small GGUF model via llama.cpp, or a
  vLLM-served model) as the offline provider.** Rejected as the *default*
  offline path for two reasons: it is not deterministic in the sense the
  benchmark needs (even with a fixed seed, inference stacks are not bit-
  reproducible across batch sizes and hardware), and it makes the test suite
  depend on a model being present and running. It remains supported through
  the `openai_compat` provider, which covers "OpenAI itself, vLLM,
  llama.cpp's server, Ollama's compatibility layer, LM Studio, or a local
  gateway" — so a local model is a configuration choice, not a different
  code path.
- **Running the real provider against a recorded fixture repository and
  accepting non-determinism.** Rejected for the benchmark use case: with a
  stochastic provider, a difference between two strategies cannot be
  attributed to the strategy rather than to the sample. The mock provider
  makes the strategy comparison a controlled experiment.
- **Injecting the mock at the `WorkerRunner` level (a fake runner that
  writes files directly).** Rejected: it would bypass the agent loop, the
  tool registry, the sandbox and the capability plumbing — the layers the
  test is supposed to cover.
- **Making the mock a test-only fixture outside the package.** Rejected: it
  needs to be a first-class provider so that `provider.name == "mock"` can
  select `RuleBasedPlanner` automatically and so that the CLI's `run`
  command works offline with no extra flags.

## Consequences

**What this buys:**

- **Offline end-to-end runs.** The whole pipeline is exercisable with no
  API key, no network and no cost. The end-to-end test is a full run:
  planning, scheduling, worktree isolation, patch capture, independent
  verification, integration and the evidence chain.
- **Deterministic benchmarks.** Two trials of the same task set under
  different strategies differ only in the strategy, which is what makes the
  coordination-cost heuristic falsifiable rather than merely plausible.
- **Fast, focused tests.** The mock answers in microseconds and produces
  no side effects outside the temporary repository.
- **Failure paths become testable.** Retry counts, provider fallback,
  timeouts, unavailability and injected errors are all first-class
  constructor arguments.
- **The brief is forced to be structured.** Because the mock reads the
  brief, `TaskBrief` has to be a real, validated, machine-readable object.
  That constraint is a feature: it is what makes the planner/worker
  contract checkable, and it is why `TaskBrief.to_prompt_block()` renders
  sorted, indented JSON rather than prose.
- **The mock is a reference implementation of the provider protocol.** Any
  new provider must satisfy the same contract, and the mock proves the
  contract is implementable without a network.

**What it costs:**

- **The mock is a simulation, not a model.** It executes exactly the edits
  and commands named in the brief. It cannot discover that a task needs
  something the brief did not anticipate, cannot iterate on a failing test
  in any interesting way, and cannot produce a plausible wrong answer.
  Behaviour observed against the mock is behaviour of the *orchestration*,
  not of the *model*.
- **The mock's fidelity is a maintenance obligation.** If the brief schema
  changes, the mock must change with it, and any drift shows up as a
  silently degraded offline run rather than as an error.
- **Determinism is the mock's, not the system's.** Two offline runs are
  identical; two real runs are not. The design is explicit that the
  guarantee is over *control-flow decisions*, and `Usage.estimated=True`
  keeps the mock's token numbers labelled as estimates.
- **Benchmark results measured with the mock do not transfer directly to
  real models.** They measure the orchestration overhead and the coordination
  model under a controlled workload. The wall-clock numbers are those of a
  synthetic provider, not of a real one; the token numbers are estimates.
  Any transfer to a real provider has to be measured separately.
- **A subtler risk.** A mock that is too convenient can let the system look
  healthy in CI while failing against a real model, because the mock never
  produces malformed tool arguments, never asks for a capability it should
  not have, and never goes off-script. The `scripted` parameter and the
  failure-injection controls exist partly to close that gap.
