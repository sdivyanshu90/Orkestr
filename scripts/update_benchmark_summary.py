"""Update the main benchmark docs from completed local measurements."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path


def replace_section(path: Path, marker: str, content: str) -> None:
    start, end = f"<!-- {marker}:start -->", f"<!-- {marker}:end -->"
    text = path.read_text()
    if start in text:
        before, tail = text.split(start, 1)
        _, after = tail.split(end, 1)
        text = before.rstrip() + "\n\n" + start + "\n" + content + "\n" + end + after
    else:
        text = text.rstrip() + "\n\n" + start + "\n" + content + "\n" + end + "\n"
    path.write_text(text)


def main() -> None:
    root = Path("benchmarks/results")
    workflow = json.loads((root / "local-sweep.json").read_text())
    inference = json.loads((root / "local-inference.json").read_text())
    cleanup = json.loads((root / "local-cleanup.json").read_text())
    assert len(workflow["trials"]) == 64
    assert len(inference["records"]) == 45
    assert cleanup["complete"], "Finish model cleanup before publishing completion metrics"
    groups = defaultdict(list)
    for row in inference["records"]:
        groups[(row["model"], row["device"])].append(row)
    lines = [
        "## Local models on RTX 3050 and CPU",
        "",
        f"Measured on 2026-11-10: eight Q4_K_M checkpoints, {workflow['completed_trials']} completed agent-workflow trials and {workflow['cancelled_trials']} user-requested cancellations, "
        "40 fixed-score GPU requests, and five CPU-baseline requests. Hardware: "
        "RTX 3050 Laptop (4 GiB VRAM), six CPU threads and 8.7 GiB RAM. Serving: CUDA llama.cpp.",
        "",
        "| Checkpoint | Runtime gate / completed | Fixed GPU tasks / 5 | Mean GPU response s |",
        "|---|---:|---:|---:|",
    ]
    for spec in workflow["models"]:
        model = spec["id"]
        trials = [
            t
            for t in workflow["trials"]
            if t["model"] == model and t["status"] != "cancelled_for_time_budget"
        ]
        direct = groups[(model, "gpu")]
        lines.append(
            f"| `{model}` | {sum(t['verified'] for t in trials)} / {len(trials)} | {sum(t['passed'] for t in direct)} / {len(direct)} | {statistics.mean(t['elapsed_s'] for t in direct):.3f} |"
        )
    cpu = groups[("qwen2.5-coder-1.5b-instruct", "cpu")]
    lines.extend(
        [
            "",
            f"Qwen2.5-Coder CPU baseline: **{sum(t['passed'] for t in cpu)} / {len(cpu)}** fixed tasks passed, "
            f"**{statistics.mean(t['elapsed_s'] for t in cpu):.3f} s** mean response time. "
            "Direct requests use one foreground configuration, one request slot, 4096 context tokens, "
            "temperature zero, seed 42, and a 256-token output cap after an untimed warmup.",
            "",
            "**Interpretation:** runtime-gate flags use model-generated plans and criteria, "
            "so they do not establish the correctness of the original requested code. "
            "User-requested cancellations are excluded from the denominators. "
            f"{sum(t['verified'] and t['tool_calls'] == 0 for t in workflow['trials'])} runtime passes "
            "recorded no worker tool calls. Fixed tasks use independent checks, but five tiny "
            "tasks with one trial each do not establish general model rankings. Response "
            "latencies include differing output lengths and possible contention from a residual "
            "server found and stopped during final cleanup; they are not fully isolated hardware comparisons. MiniMax and Kimi entries are third-party "
            "Qwen-based distillations, not official vendor checkpoints.",
            "",
            "Detailed metrics: [agent-workflow results](local-benchmark-results.md), "
            "[fixed-score inference and CPU results](local-inference-results.md), and "
            "[local methodology](local-benchmark-methodology.md). Raw JSON, per-request "
            "responses, hashes, server logs and telemetry are under `benchmarks/results/`.",
            "",
            f"**Cleanup verified:** {sum(r['bytes_before'] for r in cleanup['roots']):,} bytes "
            "of benchmark-owned model caches and inference-runtime downloads were removed. "
            "[Deletion receipt](../benchmarks/results/local-cleanup.json).",
        ]
    )
    replace_section(Path("docs/benchmark-results.md"), "local-model-metrics", "\n".join(lines))
    methodology = """## Complementary local-model measurements

The deterministic results above measure orchestration with a mock provider.
The local experiment exercises eight actual GGUF checkpoints through CUDA
llama.cpp, the production OpenAI-compatible adapter, the LLM planner and the
worker loop. It comprises four scenario requests x two strategies x eight
checkpoints, with one trial per combination. Planner-generated criteria can
weaken the original request, and worker accounting excludes planning. Runtime
success is therefore a compatibility measure, not a fixed-test accuracy score.

A separate companion experiment sends five fixed prompts per checkpoint and
repeats them on Qwen2.5-Coder with GPU offload disabled. It retains all API
responses and scores arithmetic, extraction, sorting, a restricted Python
function and a structured tool call independently. Concurrency, output caps,
warmup, timing definitions and model provenance are documented in the
[local methodology](local-benchmark-methodology.md). See the
[local workflow metrics](local-benchmark-results.md) and
[fixed-score inference metrics](local-inference-results.md) for the measurements.
"""
    replace_section(
        Path("docs/benchmark-methodology.md"), "local-model-methodology", methodology.rstrip()
    )
    print("Updated main benchmark results and methodology from completed measurements")


if __name__ == "__main__":
    main()
