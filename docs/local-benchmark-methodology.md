# Local-model benchmark: methodology

The deterministic benchmark in
[`docs/benchmark-methodology.md`](benchmark-methodology.md) measures the
*orchestration*.  It cannot measure whether a real model can drive the agent
loop at all, because the mock provider emits perfectly formed tool calls
and never fails on content.

This document describes the complementary experiment: the same orchestrator,
the same policy engine, the same verification gate and the same integration
boundary, driven by **eight real, locally served models**.

---

## 1. The question this experiment answers

> Which local models can actually drive a bounded agent loop — producing
> tool calls, making the right file changes, and passing independent
> verification — and what does each one cost in tokens and tool calls?

It is deliberately *not* a claim about model quality in general. These are
1–9 billion parameter models on a laptop; the interesting result is that
**the orchestration behaves differently with real models than with the mock**,
and that three specific mechanisms turn out to be necessary.

---

## 2. Hardware, and what it rules out

| Resource | Value |
|---|---|
| CPU | 6 vCPU (AMD Ryzen 7 8845HS) |
| RAM | 8 GB total, ~7 GB available |
| GPU | NVIDIA RTX 3050 Laptop, **4 GB VRAM** |
| Disk | 881 GB free (host filesystem) |

Two consequences follow, and both are recorded rather than glossed over.

**The flagships do not fit.** Measured sizes:

| Model | Smallest available quantisation | Fits? |
|---|---|---|
| Kimi-K3 | ~48 GB *per shard* (≈750 GB total) | No |
| Kimi-K2 / K2.5 / Kimi-Linear-48B-A3B | 10.9 GB (IQ1_M) | No — exceeds 8 GB RAM |
| MiniMax-M1 / MiniMax-Text-01 / MiniMax-M2 | 230B–456B | No |
| GLM-4.7-Flash | 11.3 GB (Q2_K) | No |
| Qwen2.5-Coder-7B | 4.7 GB | Only with heavy CPU offload |

`EXCLUDED_MODELS` in `src/agent_squad/benchmark/local.py` records every
exclusion with its size and the reason, so the exclusion is auditable
rather than implicit.

**The smallest available artefact from each family is used instead.** For
Kimi and MiniMax that means a *third-party distillation* rather than the
flagship: `Qwen3.5-9B-Kimi-k3-Distilled` and
`Qwen3-4B-2507-Thinking-Minimax-M1-Distill`. That is a real limitation of
this experiment, and it is stated on the model entry itself.

---

## 3. The model set

Eight models, seven families, all GGUF at Q4_K_M from the Unsloth, Qwen,
ggml-org and bartowski conversions.

| Model | Family | Parameters | Size | GPU layers |
|---|---|---:|---:|---:|
| `qwen2.5-coder-1.5b-instruct` | Qwen | 1.5 B | 1.13 GB | all |
| `qwen3-1.7b` | Qwen | 1.7 B | 1.11 GB | all |
| `deepseek-r1-distill-qwen-1.5b` | DeepSeek | 1.5 B | 1.12 GB | all |
| `gemma-3-1b-it` | Gemma | 1 B | 0.82 GB | all |
| `llama-3.2-1b-instruct` | Llama | 1 B | 0.81 GB | all |
| `phi-4-mini-instruct` | Phi | 3.8 B | 2.41 GB | partial |
| `minimax-m1-distill-qwen3-4b` | MiniMax | 4 B | 2.50 GB | partial |
| `kimi-k3-distill-qwen3.5-9b` | Kimi | 9 B | 5.78 GB | partial |

**GPU layer planning.** `plan_gpu_layers` divides the free VRAM (85 % of
it) between the models being served concurrently, then places as many
layers on the GPU as fit, leaving the remainder on the CPU. The chosen
layer count is recorded per model in the report's `server_stats`.

---

## 4. The serving stack

`llama.cpp`'s `llama-server` (build b12246, CUDA 13.5), which exposes an
OpenAI-compatible `/v1/chat/completions` endpoint. This means the adapter
under test is the *same* `openai_compat` provider used against a hosted
API — the experiment exercises the production code path, not a special
local-only shim.

Each model is served once; all of that model's runs reuse the loaded
weights.

---

## 5. What is measured

| Metric | Definition |
|---|---|
| **Verified rate** | Runs in which the task was completed *and* passed independent verification |
| Model calls | Requests issued to the provider |
| Tool calls | Tool invocations that reached the tool layer |
| Salvaged calls | Tool calls recovered from the assistant's *text* rather than the structured channel |
| Protected-action requests | Times the model asked for an action needing human approval |
| Verification failures | Gate runs that rejected the work |
| Tokens | Provider-reported usage |
| Wall-clock | Secondary; see §7 |

---

## 6. What is deliberately different from the mock benchmark

The local scenarios omit the mock provider's `edits` affordance. In the
deterministic benchmark, the brief carries the file content and the mock
writes it. Here, the brief carries only the *objective*, and the model has
to call `write_file` itself with the content it invents.

That is the whole point: it measures whether a model can drive the tool
loop, not whether the orchestrator can move bytes.

---

## 7. Concurrency, and what it invalidates

Models are served concurrently (default 3) and a model's scenarios run
concurrently (default 2). This cuts the sweep from roughly an hour to
roughly a quarter of an hour.

The consequence is stated in the report itself: **wall-clock times are not
comparable between models**, because a model served alongside two others
sees different contention from one served alone. The *verified rate*, the
token counts and the tool-call counts are unaffected — they are properties
of the model, not of the machine's load.

Within a model, every strategy sees the same contention, so the
single-versus-multi comparison inside a model remains fair.

---

## 8. Reproducing

```bash
# One model, one scenario
agent-squad benchmark --local \
    --local-models qwen2.5-coder-1.5b-instruct \
    --local-scenarios write_a_helper \
    --local-strategies single --trials 1 --json

# The full sweep
agent-squad benchmark --local \
    --local-scenarios write_a_helper,write_a_test,two_independent_files,dependent_chain \
    --local-strategies single,static_two \
    --local-parallel-models 3 --local-parallel-runs 2 \
    --trials 1 --json > sweep.json
```

The weights live in `~/.cache/agent-squad-benchmark/models` and the server
binary in `~/.cache/agent-squad-benchmark/llamacpp`; both paths are
overridable with `AGENT_SQUAD_LOCAL_MODELS` and `AGENT_SQUAD_LLAMA_SERVER`.

---

## 9. Known limitations of this experiment

1. **The models are small.** Nothing here predicts the behaviour of a
   frontier model.
2. **The Kimi and MiniMax entries are third-party distillations**, not the
   vendors' own weights.
3. **One trial per combination.** Wall-clock variance on a laptop is large;
   the verified rate is deterministic enough at this size, but a claim about
   small differences in it would need more trials.
4. **The scenarios are small and synthetic.** They exercise the loop, not
   software engineering.
5. **The benchmark is not a model leaderboard.** It measures *orchestration
   compatibility* under this runtime's prompt, tool set and iteration
   budget — a different prompt would produce a different ranking.
