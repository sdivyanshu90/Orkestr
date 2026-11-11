"""Render independently scored model probes, including response timing."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path


def render(report: dict) -> str:
    rows = report["records"]
    groups = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["device"])].append(row)
    lines = [
        "# Fixed-score local inference benchmark",
        "",
        "[Documentation home](README.md) · [Evidence guide](evaluation.md) · [Reproduction](reproduction.md)",
        "",
        "**Historical direct-probe snapshot: 2026-11-10.** Fixed-task scores remain auditable, but the residual-server timing caveat remains in force. A later clean sample demonstrates resource ownership separately; it does not replace these measurements or establish general model rankings.",
        "",
        "Eight local GGUF checkpoints, five fixed tasks per checkpoint, plus the same five "
        "tasks on Qwen2.5-Coder using CPU only. One foreground model configuration and one benchmark request run at a time. "
        "All responses, including failures, are retained in "
        "[local-inference.json](../benchmarks/results/local-inference.json).",
        "",
        "MiniMax and Kimi entries are third-party Qwen-based distillations; results do not "
        "describe official MiniMax or Moonshot models.",
        "",
        "| Model | Device | Passed / 5 | Mean response s | Output tokens | Mean decode tok/s |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for (model, device), trials in groups.items():
        generated = sum(
            t.get("response", {}).get("usage", {}).get("completion_tokens", 0) for t in trials
        )
        # Use the server's decode-only timing; do not divide by end-to-end latency.
        speeds = [
            t["response"]["timings"]["predicted_per_second"]
            for t in trials
            if t.get("response", {}).get("timings", {}).get("predicted_per_second", 0) > 0
        ]
        speed = f"{statistics.mean(speeds):.2f}" if speeds else "unavailable"
        lines.append(
            f"| `{model}` | {device} | {sum(t['passed'] for t in trials)} / {len(trials)} | {statistics.mean(t['elapsed_s'] for t in trials):.3f} | {generated} | {speed} |"
        )
    lines.extend(
        [
            "",
            "**Timing limitation:** final cleanup found and stopped a residual server "
            "from the interrupted workflow sweep. Foreground requests were sequential, "
            "but the run was not fully process-isolated; GPU and host-memory pressure "
            "could affect latency and throughput. These timings are observations under "
            "that resource state, not clean hardware comparisons. Task scoring still "
            "uses the preserved responses and fixed checks.",
        ]
    )
    lines.extend(
        [
            "",
            "## Task outcomes",
            "",
            "| Model | Device | Arithmetic | Extraction | Sorting | Python square | Tool call |",
            "|---|---|:---:|:---:|:---:|:---:|:---:|",
        ]
    )
    for (model, device), trials in groups.items():
        outcomes = {t["task"]: "pass" if t["passed"] else "fail" for t in trials}
        cells = " | ".join(
            outcomes[name]
            for name in ("arithmetic", "extraction", "sorting", "python_square", "tool_call")
        )
        lines.append(f"| `{model}` | {device} | {cells} |")
    lines.extend(
        [
            "",
            "## Conditions and limits",
            "",
            "- Five tasks: integer arithmetic, entity extraction, sorting, a Python square function, and one structured tool call. JSON answers must equal the expected values. The Python task executes only an AST-whitelisted function and tests four inputs; tool calls must have the exact name and arguments.",
            "- Common seed 42, temperature 0, 256 output tokens, 4096 context tokens, one server slot, six CPU threads, and a separate untimed 16-token warmup. The largest checkpoint requests 12 GPU layers; others request full offload. The CPU baseline requests zero GPU layers.",
            "- The scorer accepts a complete leading `<think>...</think>` block and a surrounding Markdown JSON fence. It does not recover truncated reasoning, arbitrary JSON substrings, or text-rendered tool calls.",
            "- Reasoning tokens consume the output budget. A model that exhausts the budget without a final answer fails this bounded task; that is not a claim about its unrestricted capability.",
            "- Response time is end-to-end HTTP latency after warmup, excluding model loading and scoring. Decode tokens/second comes only from server-reported `timings.predicted_per_second` when available. Output-token counts include any reasoning tokens reported by the server.",
            "- One trial on five tiny prompts is a smoke comparison. It cannot establish general model rankings, production throughput, or statistical confidence. GPU rows may include CPU layers or shared memory. See the preserved server logs and telemetry for placement and utilization.",
            "- The CPU comparison changes offload only; short outputs and prompt caching make any observed timing ratio specific to this workload.",
            "",
            "Regenerate with `.venv/bin/python scripts/render_local_inference.py`.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    source = Path("benchmarks/results/local-inference.json")
    Path("docs/local-inference-results.md").write_text(render(json.loads(source.read_text())))
