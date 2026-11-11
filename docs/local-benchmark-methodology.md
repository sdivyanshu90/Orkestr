# Local-model benchmark methodology

[Documentation home](README.md) · [Evidence guide](evaluation.md) · [Reproduction](reproduction.md)

**Historical protocol: 2026-11-10.** This page describes the measured eight-checkpoint workflow sweep and companion probes, including their cancellation, accounting and residual-server limitations. New contract/resource code does not retroactively strengthen those results. Later ownership-checked timing has separate artifacts.

This experiment runs real local GGUF models through Orkestr's production
OpenAI-compatible provider, LLM planner, worker tool loop, policy engine and
verification gate. It measures compatibility with this bounded agent workflow.
It does not establish general model quality or coding accuracy.

## Hardware and serving

The development host exposes six CPU threads, 8.7 GiB RAM, 4 GiB swap and an
NVIDIA RTX 3050 Laptop GPU with 4096 MiB VRAM. Models are served with the
existing CUDA llama.cpp `llama-server` build b12246. No hosted inference API
is used. Some GGUF conversions come from Unsloth; Unsloth training is not used.

Eight distinct model checkpoints were downloaded at Q4_K_M:

| Local ID | Parameters | GGUF source repository |
|---|---:|---|
| qwen2.5-coder-1.5b-instruct | 1.5B | Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF |
| qwen3-1.7b | 1.7B | unsloth/Qwen3-1.7B-GGUF |
| deepseek-r1-distill-qwen-1.5b | 1.5B | unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF |
| gemma-3-1b-it | 1B | ggml-org/gemma-3-1b-it-GGUF |
| llama-3.2-1b-instruct | 1B | unsloth/Llama-3.2-1B-Instruct-GGUF |
| phi-4-mini-instruct | 3.8B | unsloth/Phi-4-mini-instruct-GGUF |
| minimax-m1-distill-qwen3-4b | 4B | mradermacher/Qwen3-4B-2507-Thinking-Minimax-M1-Distill-Uncensored-GGUF |
| kimi-k3-distill-qwen3.5-9b | 9B | mradermacher/Qwen3.5-9B-Kimi-k3-Distilled-GGUF |

These IDs and parameter labels come from the existing model catalog and the
selected artifacts. The MiniMax and Kimi entries are **third-party Qwen-based
distillations**, not official MiniMax or Moonshot checkpoints. Repository names
do not independently verify training provenance or teacher quality. The selected
set represents distinct checkpoints, not eight independent base architectures.

The runtime chooses a requested GPU-layer count from free VRAM and model size.
That count is an estimate, not the number actually offloaded: for example,
`999` means request all layers. The estimate can exceed the model's layer count.
Large models may use system or shared memory; do not infer placement from this
flag alone. GPU telemetry and server logs are retained with the results.

## Workload

The sweep uses four synthetic scenarios, each with `single` and `static_two`
strategies, one trial per combination: **8 models × 4 scenarios × 2 strategies
= 64 planned trials**. The measured run completed **62 trials**; after the
user requested faster completion, two remaining Kimi workflow trials were
cancelled. Cancellations are retained in raw records but excluded from rates
and averages. All eight checkpoints complete the separate fixed-score probes.

`static_two` configures at most two workers; dependencies or the model's
chosen plan may leave only one runnable task.

| Scenario | Requested work | Criteria supplied in the request |
|---|---|---|
| write_a_helper | Write a greet function | File exists and contains `def greet` |
| write_a_test | Test the existing calculator | File exists and selected pytest passes |
| two_independent_files | Write alpha and beta modules | Both files exist |
| dependent_chain | Write square, then test it | Module exists and selected pytest passes |

The production LLM planner consumes these requests and can change the tasks,
dependencies and criteria. A result marked verified means the runtime reported
`succeeded` with at least one successful task. It does **not** mean the output
passed an immutable external test suite. File-presence criteria do not establish
functional correctness. Some baseline fixture tests can pass without new work.
Integration is disabled (`integrate=False`), so no merge outcome is measured.

Worker calls are capped at 512 output tokens with six iterations per role.
The planner has its own configured output budget. Retry settings allow two
attempts, and unattended protected actions have a ten-second approval timeout.
No mock-provider `edits` are supplied; models must generate their own tool calls.
The production adapter can recover supported JSON tool calls from assistant text;
the harness records the number recovered.

## Concurrency and limits

The measured sweep serves two models concurrently and permits three scenario
runs concurrently across the suite. Small models have eight server slots with
3072 tokens per slot; medium models have six, and the largest has three.
Each trial also sets provider `max_concurrency=1`, so two workers in a trial
can overlap tool activity but their model requests are serialized.
This is a constrained configuration: longer conversations can exceed 3072 tokens.
Such failures count against workflow compatibility under these settings and
must not be interpreted as proof that the model cannot solve the task.

Wall-clock times include GPU contention and orchestration, and cannot rank model
inference speed. Concurrency, different generated plans, sampling and context
failures can also affect success rates and token counts. One trial provides no
estimate of variance. Within-model strategy comparisons are descriptive, not a
controlled causal test of parallelism.

## Metrics and accounting caveats

The raw records include status, verified flag, elapsed wall time, provider-reported
worker tokens, runtime worker model-call and tool-call events, verification failures, recovered
tool calls and protected-action requests. Token usage is summed from reported
usage in the successful outcome path; it is not an independent tokenizer count.
Planner calls are not emitted as `MODEL_CALL` events, and planner token usage is
not included in the scheduler summary. The tables therefore undercount total
inference work even for runs that return normally.

When a run raises, the current harness leaves tokens and most event counters at
zero even if inference already occurred. Treat these as **unavailable accounting**.
The `tool_call_parse_failures` field is always initialized to zero and is not a
measured metric. Zero API cost means no hosted inference bill; electricity and
hardware costs are excluded. Server `size_mb` comes from catalog metadata;
actual downloaded byte sizes are recorded separately in the hardware manifest.

## Reproduction and artifacts

```bash
.venv/bin/agent-squad benchmark --local \
  --local-scenarios write_a_helper,write_a_test,two_independent_files,dependent_chain \
  --local-strategies single,static_two \
  --local-parallel-models 2 --local-parallel-runs 3 \
  --local-progress benchmarks/results/local-sweep.jsonl \
  --trials 1 --json > benchmarks/results/local-sweep.json

.venv/bin/python scripts/render_local_results.py \
  benchmarks/results/local-sweep.jsonl \
  --output docs/local-benchmark-results.md
```

The server binary must exist at `AGENT_SQUAD_LLAMA_SERVER` or the default cache
path, and `AGENT_SQUAD_LOCAL_MODELS` can override the model directory. Reproduction
requires downloading the weights again because the measured run's model weights
are removed afterward. Raw results, logs, hardware metadata and cleanup evidence
are retained under `benchmarks/results/`.

## Fixed-score companion probes

A second experiment in `scripts/benchmark_local_inference.py` runs after the
foreground agent sweep stops. It sends the same five short prompts directly to
each of the eight models, then repeats them on Qwen2.5-Coder with GPU offload
disabled. Its scoring rules and expected outputs are fixed in the script rather
than generated by the model. Arithmetic, extraction and sorting use exact JSON
equality. The Python square function must pass restricted AST validation and
four concrete input checks. The tool task requires exactly one structured call
with the expected name and arguments.

All configurations use temperature zero, seed 42, a 256-token output cap, one
request slot and 4096 context tokens. Each receives an untimed 16-token warmup.
Only complete leading reasoning tags and surrounding JSON code fences are
stripped before scoring. Exhausting the token cap without a final answer fails.
Model loading is excluded from per-request latency. GPU placement is full
offload for smaller models and 12 requested layers for the largest checkpoint.
The CPU baseline requests zero GPU layers. Per-second GPU telemetry and verbose
server logs are saved. These are five smoke tasks with one trial each, not a
statistically powered leaderboard. Results are in
[`local-inference-results.md`](local-inference-results.md).

```bash
.venv/bin/python scripts/benchmark_local_inference.py
.venv/bin/python scripts/render_local_inference.py
```

## Residual-process timing limitation

Final cleanup found and stopped one residual llama-server process from the
interrupted workflow sweep (PID 270787, recorded in the deletion receipt).
The companion probes ran one foreground configuration and one benchmark request
at a time, but were not fully isolated from background model resources. GPU and
host-memory pressure may have affected latency and throughput. Their fixed
task scores remain directly auditable from the saved responses; timing results
are observational and must not be presented as clean GPU/CPU hardware rankings.
The CPU baseline requested zero offloaded layers; that does not imply the GPU
was otherwise idle. No model servers remain after cleanup.
