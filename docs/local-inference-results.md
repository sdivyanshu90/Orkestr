# Fixed-score local inference benchmark

[Documentation home](README.md) · [Evidence guide](evaluation.md) · [Reproduction](reproduction.md)

**Historical direct-probe snapshot: 2026-11-10.** Fixed-task scores remain auditable, but the residual-server timing caveat remains in force. A later clean sample demonstrates resource ownership separately; it does not replace these measurements or establish general model rankings.

Eight local GGUF checkpoints, five fixed tasks per checkpoint, plus the same five tasks on Qwen2.5-Coder using CPU only. One foreground model configuration and one benchmark request run at a time. All responses, including failures, are retained in [local-inference.json](../benchmarks/results/local-inference.json).

MiniMax and Kimi entries are third-party Qwen-based distillations; results do not describe official MiniMax or Moonshot models.

| Model | Device | Passed / 5 | Mean response s | Output tokens | Mean decode tok/s |
|---|---|---:|---:|---:|---:|
| `qwen2.5-coder-1.5b-instruct` | gpu | 2 / 5 | 0.310 | 117 | 113.25 |
| `qwen3-1.7b` | gpu | 4 / 5 | 0.694 | 314 | 111.88 |
| `deepseek-r1-distill-qwen-1.5b` | gpu | 1 / 5 | 2.155 | 1223 | 118.22 |
| `gemma-3-1b-it` | gpu | 2 / 5 | 0.301 | 122 | 119.06 |
| `llama-3.2-1b-instruct` | gpu | 3 / 5 | 0.228 | 116 | 157.06 |
| `phi-4-mini-instruct` | gpu | 2 / 5 | 0.481 | 90 | 60.13 |
| `minimax-m1-distill-qwen3-4b` | gpu | 2 / 5 | 3.640 | 978 | 55.87 |
| `kimi-k3-distill-qwen3.5-9b` | gpu | 4 / 5 | 33.841 | 717 | 4.50 |
| `qwen2.5-coder-1.5b-instruct` | cpu | 2 / 5 | 1.770 | 117 | 16.13 |

**Timing limitation:** final cleanup found and stopped a residual server from the interrupted workflow sweep. Foreground requests were sequential, but the run was not fully process-isolated; GPU and host-memory pressure could affect latency and throughput. These timings are observations under that resource state, not clean hardware comparisons. Task scoring still uses the preserved responses and fixed checks.

## Task outcomes

| Model | Device | Arithmetic | Extraction | Sorting | Python square | Tool call |
|---|---|:---:|:---:|:---:|:---:|:---:|
| `qwen2.5-coder-1.5b-instruct` | gpu | pass | fail | fail | pass | fail |
| `qwen3-1.7b` | gpu | pass | pass | fail | pass | pass |
| `deepseek-r1-distill-qwen-1.5b` | gpu | fail | pass | fail | fail | fail |
| `gemma-3-1b-it` | gpu | pass | pass | fail | fail | fail |
| `llama-3.2-1b-instruct` | gpu | pass | pass | fail | pass | fail |
| `phi-4-mini-instruct` | gpu | pass | fail | fail | pass | fail |
| `minimax-m1-distill-qwen3-4b` | gpu | pass | fail | fail | fail | pass |
| `kimi-k3-distill-qwen3.5-9b` | gpu | pass | pass | pass | fail | pass |
| `qwen2.5-coder-1.5b-instruct` | cpu | pass | fail | fail | pass | fail |

## Conditions and limits

- Five tasks: integer arithmetic, entity extraction, sorting, a Python square function, and one structured tool call. JSON answers must equal the expected values. The Python task executes only an AST-whitelisted function and tests four inputs; tool calls must have the exact name and arguments.
- Common seed 42, temperature 0, 256 output tokens, 4096 context tokens, one server slot, six CPU threads, and a separate untimed 16-token warmup. The largest checkpoint requests 12 GPU layers; others request full offload. The CPU baseline requests zero GPU layers.
- The scorer accepts a complete leading `<think>...</think>` block and a surrounding Markdown JSON fence. It does not recover truncated reasoning, arbitrary JSON substrings, or text-rendered tool calls.
- Reasoning tokens consume the output budget. A model that exhausts the budget without a final answer fails this bounded task; that is not a claim about its unrestricted capability.
- Response time is end-to-end HTTP latency after warmup, excluding model loading and scoring. Decode tokens/second comes only from server-reported `timings.predicted_per_second` when available. Output-token counts include any reasoning tokens reported by the server.
- One trial on five tiny prompts is a smoke comparison. It cannot establish general model rankings, production throughput, or statistical confidence. GPU rows may include CPU layers or shared memory. See the preserved server logs and telemetry for placement and utilization.
- The CPU comparison changes offload only; short outputs and prompt caching make any observed timing ratio specific to this workload.

Regenerate with `.venv/bin/python scripts/render_local_inference.py`.
