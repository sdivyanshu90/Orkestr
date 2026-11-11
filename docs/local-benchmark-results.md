# Local-model benchmark: results

[Documentation home](README.md) · [Evidence guide](evaluation.md) · [Reproduction](reproduction.md)

**Historical workflow snapshot: 2026-11-10.** The tables retain the then-current runtime gates and accounting. They must not be reinterpreted as independently accepted code under the newer request contract. Cancellation and missing-usage treatment are stated below; later code requires a new experiment.

**Status: measured.** Completed-trial measurements were produced by `agent-squad benchmark --local` on the development host (6 vCPU, 8.7 GiB RAM, RTX 3050 with 4 GB VRAM). The tables can be regenerated from the raw trial records.

Read [`docs/local-benchmark-methodology.md`](local-benchmark-methodology.md) for workload limits, concurrency and accounting caveats.

Raw records: [`local-sweep.json`](../benchmarks/results/local-sweep.json) and [`local-sweep.jsonl`](../benchmarks/results/local-sweep.jsonl). MiniMax and Kimi labels identify third-party Qwen-based distillations, not official vendor models.

**Interpretation:** verified means the runtime reported `succeeded` with at least one successful task. The LLM planner can rewrite tasks and acceptance criteria; these are not fixed hidden correctness tests. An error can occur after inference, but the current harness then records zero tokens/calls; those zeros mean unavailable accounting, not zero usage. Usage averages exclude error rows whose counters are unavailable. Planner calls and planner tokens are excluded from these counters. The parse-failure counter is a placeholder and is not measured.

**Completion:** 62 trials completed; 2 were cancelled after the user requested faster completion. Cancellations are excluded from all rates and averages below; the raw matrix retains all 64 slots.

| Cancelled model | Scenario | Strategy |
|---|---|---|
| `kimi-k3-distill-qwen3.5-9b` | `two_independent_files` | single |
| `kimi-k3-distill-qwen3.5-9b` | `dependent_chain` | static_two |

## 1. Results by model

| Model | Family | Runs | Usage runs | Verified | Verified rate | Mean worker calls | Mean worker tokens | Mean tool calls | Salvaged calls | Protected-action requests |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `qwen2.5-coder-1.5b-instruct` | Qwen | 8 | 8 | 4 | 0.500 | 5.2 | 6461 | 7.6 | 40 | 0 |
| `qwen3-1.7b` | Qwen | 8 | 7 | 1 | 0.125 | 6.4 | 7592 | 41.0 | 0 | 4 |
| `deepseek-r1-distill-qwen-1.5b` | DeepSeek | 8 | 7 | 2 | 0.250 | 2.6 | 1542 | 0.0 | 0 | 0 |
| `gemma-3-1b-it` | Gemma | 8 | 8 | 2 | 0.250 | 3.0 | 1560 | 0.0 | 0 | 0 |
| `llama-3.2-1b-instruct` | Llama | 8 | 8 | 0 | 0.000 | 1.4 | 1897 | 1.4 | 0 | 6 |
| `phi-4-mini-instruct` | Phi | 8 | 7 | 7 | 0.875 | 4.7 | 2612 | 0.0 | 0 | 0 |
| `minimax-m1-distill-qwen3-4b` | MiniMax | 8 | 1 | 0 | 0.000 | 2.0 | 1800 | 0.0 | 0 | 0 |
| `kimi-k3-distill-qwen3.5-9b` | Kimi | 6 | 4 | 0 | 0.000 | 7.2 | 9025 | 52.5 | 0 | 4 |

**Gate limitation:** 11 reported verified runs recorded zero worker tool calls. These success flags do not establish that the requested new files were written; the planner-generated criteria and existing fixture state can be weaker than the request.

## 2. Results by scenario

| Scenario | Runs | Verified | Verified rate | Mean worker tokens | Mean worker calls | Mean tool calls |
|---|---:|---:|---:|---:|---:|---:|
| `dependent_chain` | 15 | 2 | 0.133 | 4223 | 5.0 | 16.6 |
| `two_independent_files` | 15 | 4 | 0.267 | 6223 | 5.3 | 14.7 |
| `write_a_helper` | 16 | 4 | 0.250 | 3132 | 3.4 | 7.8 |
| `write_a_test` | 16 | 6 | 0.375 | 2368 | 2.5 | 6.2 |

## 3. Single-agent versus multi-agent, per model

The interesting comparison is *within* a model: the same model, the same scenario requests, one worker or two. Plans and shared GPU contention can differ, so this does not establish a causal speedup.

| Model | Scenario | Strategy | Workers | Verified | Model calls | Tool calls | Tokens |
|---|---|---|---:|:---:|---:|---:|---:|
| `qwen2.5-coder-1.5b-instruct` | `dependent_chain` | single | 1 | no | 9 | 13 | 10323 |
| `qwen2.5-coder-1.5b-instruct` | `dependent_chain` | static_two | 2 | no | 4 | 3 | 3902 |
| `qwen2.5-coder-1.5b-instruct` | `two_independent_files` | single | 1 | yes | 5 | 7 | 6979 |
| `qwen2.5-coder-1.5b-instruct` | `two_independent_files` | static_two | 2 | yes | 6 | 9 | 9352 |
| `qwen2.5-coder-1.5b-instruct` | `write_a_helper` | single | 1 | yes | 3 | 4 | 5277 |
| `qwen2.5-coder-1.5b-instruct` | `write_a_helper` | static_two | 2 | yes | 3 | 5 | 5433 |
| `qwen2.5-coder-1.5b-instruct` | `write_a_test` | single | 1 | no | 6 | 10 | 5219 |
| `qwen2.5-coder-1.5b-instruct` | `write_a_test` | static_two | 2 | no | 6 | 10 | 5201 |
| `qwen3-1.7b` | `dependent_chain` | single | 1 | no | 7 | 67 | 8518 |
| `qwen3-1.7b` | `dependent_chain` | static_two | 2 | no | 5 | 65 | 3354 |
| `qwen3-1.7b` | `two_independent_files` | single | 1 | no | 5 | 97 | 4344 |
| `qwen3-1.7b` | `two_independent_files` | static_two | 2 | no | 13 | 10 | 18628 |
| `qwen3-1.7b` | `write_a_helper` | single | 1 | no | 9 | 47 | 10373 |
| `qwen3-1.7b` | `write_a_helper` | static_two | 2 | no | 2 | 0 | 1759 |
| `qwen3-1.7b` | `write_a_test` | single | 1 | no | 0 | 0 | 0 |
| `qwen3-1.7b` | `write_a_test` | static_two | 2 | yes | 4 | 1 | 6169 |
| `deepseek-r1-distill-qwen-1.5b` | `dependent_chain` | single | 1 | no | 4 | 0 | 2176 |
| `deepseek-r1-distill-qwen-1.5b` | `dependent_chain` | static_two | 2 | no | 4 | 0 | 2139 |
| `deepseek-r1-distill-qwen-1.5b` | `two_independent_files` | single | 1 | no | 4 | 0 | 1920 |
| `deepseek-r1-distill-qwen-1.5b` | `two_independent_files` | static_two | 2 | no | 0 | 0 | 0 |
| `deepseek-r1-distill-qwen-1.5b` | `write_a_helper` | single | 1 | no | 2 | 0 | 1129 |
| `deepseek-r1-distill-qwen-1.5b` | `write_a_helper` | static_two | 2 | no | 2 | 0 | 1050 |
| `deepseek-r1-distill-qwen-1.5b` | `write_a_test` | single | 1 | yes | 1 | 0 | 1191 |
| `deepseek-r1-distill-qwen-1.5b` | `write_a_test` | static_two | 2 | yes | 1 | 0 | 1191 |
| `gemma-3-1b-it` | `dependent_chain` | single | 1 | no | 4 | 0 | 1814 |
| `gemma-3-1b-it` | `dependent_chain` | static_two | 2 | no | 4 | 0 | 1491 |
| `gemma-3-1b-it` | `two_independent_files` | single | 1 | no | 4 | 0 | 1877 |
| `gemma-3-1b-it` | `two_independent_files` | static_two | 2 | no | 2 | 0 | 1834 |
| `gemma-3-1b-it` | `write_a_helper` | single | 1 | no | 4 | 0 | 1607 |
| `gemma-3-1b-it` | `write_a_helper` | static_two | 2 | no | 3 | 0 | 1759 |
| `gemma-3-1b-it` | `write_a_test` | single | 1 | yes | 2 | 0 | 1383 |
| `gemma-3-1b-it` | `write_a_test` | static_two | 2 | yes | 1 | 0 | 714 |
| `llama-3.2-1b-instruct` | `dependent_chain` | single | 1 | no | 2 | 2 | 2426 |
| `llama-3.2-1b-instruct` | `dependent_chain` | static_two | 2 | no | 7 | 7 | 10348 |
| `llama-3.2-1b-instruct` | `two_independent_files` | single | 1 | no | 0 | 0 | 0 |
| `llama-3.2-1b-instruct` | `two_independent_files` | static_two | 2 | no | 0 | 0 | 0 |
| `llama-3.2-1b-instruct` | `write_a_helper` | single | 1 | no | 1 | 1 | 1205 |
| `llama-3.2-1b-instruct` | `write_a_helper` | static_two | 2 | no | 1 | 1 | 1198 |
| `llama-3.2-1b-instruct` | `write_a_test` | single | 1 | no | 0 | 0 | 0 |
| `llama-3.2-1b-instruct` | `write_a_test` | static_two | 2 | no | 0 | 0 | 0 |
| `phi-4-mini-instruct` | `dependent_chain` | single | 1 | yes | 4 | 0 | 2756 |
| `phi-4-mini-instruct` | `dependent_chain` | static_two | 2 | yes | 4 | 0 | 2263 |
| `phi-4-mini-instruct` | `two_independent_files` | single | 1 | yes | 6 | 0 | 3141 |
| `phi-4-mini-instruct` | `two_independent_files` | static_two | 2 | yes | 6 | 0 | 3091 |
| `phi-4-mini-instruct` | `write_a_helper` | single | 1 | yes | 5 | 0 | 2501 |
| `phi-4-mini-instruct` | `write_a_helper` | static_two | 2 | yes | 5 | 0 | 2748 |
| `phi-4-mini-instruct` | `write_a_test` | single | 1 | no | 0 | 0 | 0 |
| `phi-4-mini-instruct` | `write_a_test` | static_two | 2 | yes | 3 | 0 | 1781 |
| `minimax-m1-distill-qwen3-4b` | `dependent_chain` | single | 1 | no | 0 | 0 | 0 |
| `minimax-m1-distill-qwen3-4b` | `dependent_chain` | static_two | 2 | no | 0 | 0 | 0 |
| `minimax-m1-distill-qwen3-4b` | `two_independent_files` | single | 1 | no | 0 | 0 | 0 |
| `minimax-m1-distill-qwen3-4b` | `two_independent_files` | static_two | 2 | no | 0 | 0 | 0 |
| `minimax-m1-distill-qwen3-4b` | `write_a_helper` | single | 1 | no | 2 | 0 | 1800 |
| `minimax-m1-distill-qwen3-4b` | `write_a_helper` | static_two | 2 | no | 0 | 0 | 0 |
| `minimax-m1-distill-qwen3-4b` | `write_a_test` | single | 1 | no | 0 | 0 | 0 |
| `minimax-m1-distill-qwen3-4b` | `write_a_test` | static_two | 2 | no | 0 | 0 | 0 |
| `kimi-k3-distill-qwen3.5-9b` | `dependent_chain` | single | 1 | no | 7 | 59 | 3389 |
| `kimi-k3-distill-qwen3.5-9b` | `two_independent_files` | static_two | 2 | no | 13 | 53 | 23507 |
| `kimi-k3-distill-qwen3.5-9b` | `write_a_helper` | single | 1 | no | 0 | 0 | 0 |
| `kimi-k3-distill-qwen3.5-9b` | `write_a_helper` | static_two | 2 | no | 6 | 51 | 6003 |
| `kimi-k3-distill-qwen3.5-9b` | `write_a_test` | single | 1 | no | 3 | 47 | 3200 |
| `kimi-k3-distill-qwen3.5-9b` | `write_a_test` | static_two | 2 | no | 0 | 0 | 0 |

## 4. How the runs failed

| Reason | Runs |
|---|---:|
| task did not pass independent verification | 34 |
| model request failed:  | 4 |
| planner response did not contain a JSON object | 4 |
| model request timed out after 600.0s | 4 |

## 5. How often the tool-call salvage was needed

The salvage path recovers a tool call that a model rendered as a JSON block in the assistant's *content* rather than in the structured channel.  The count below is how often that happened.

| Model | Tool calls | Salvaged | Share |
|---|---:|---:|---:|
| `qwen2.5-coder-1.5b-instruct` | 61 | 40 | 65.6% |
| `qwen3-1.7b` | 287 | 0 | 0.0% |
| `deepseek-r1-distill-qwen-1.5b` | 0 | 0 | - |
| `gemma-3-1b-it` | 0 | 0 | - |
| `llama-3.2-1b-instruct` | 11 | 0 | 0.0% |
| `phi-4-mini-instruct` | 0 | 0 | - |
| `minimax-m1-distill-qwen3-4b` | 0 | 0 | - |
| `kimi-k3-distill-qwen3.5-9b` | 210 | 0 | 0.0% |
