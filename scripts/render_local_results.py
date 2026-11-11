"""Render the local-model benchmark results as Markdown.

The report is generated from the raw trial records rather than written by
hand, so the numbers in the document cannot drift from the numbers that were
measured.

    python scripts/render_local_results.py \
        ~/.cache/agent-squad-benchmark/sweep.jsonl \
        --output docs/local-benchmark-results.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

FAMILY_ORDER = [
    "Qwen",
    "DeepSeek",
    "Gemma",
    "Llama",
    "Phi",
    "MiniMax",
    "Kimi",
]


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def render(rows: list[dict]) -> str:
    cancelled = [row for row in rows if row.get("status") == "cancelled_for_time_budget"]
    rows = [row for row in rows if row.get("status") != "cancelled_for_time_budget"]
    by_model: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)

    def sort_key(model: str) -> tuple[int, str]:
        family = by_model[model][0]["family"] if by_model[model] else ""
        index = FAMILY_ORDER.index(family) if family in FAMILY_ORDER else 99
        return (index, model)

    out: list[str] = []
    out.append("# Local-model benchmark: results\n")
    out.append(
        "**Status: measured.** Completed-trial measurements were produced by "
        "`agent-squad benchmark --local` on the development host "
        "(6 vCPU, 8.7 GiB RAM, RTX 3050 with 4 GB VRAM). "
        "The tables can be regenerated from the raw trial records.\n"
    )
    out.append(
        "Read [`docs/local-benchmark-methodology.md`](local-benchmark-methodology.md) "
        "for workload limits, concurrency and accounting caveats.\n"
    )

    out.append(
        "Raw records: [`local-sweep.json`](../benchmarks/results/local-sweep.json) "
        "and [`local-sweep.jsonl`](../benchmarks/results/local-sweep.jsonl). "
        "MiniMax and Kimi labels identify third-party Qwen-based distillations, "
        "not official vendor models.\n"
    )
    out.append(
        "**Interpretation:** verified means the runtime reported `succeeded` "
        "with at least one successful task. The LLM planner can rewrite tasks "
        "and acceptance criteria; these are not fixed hidden correctness tests. "
        "An error can occur after inference, but the current harness then records "
        "zero tokens/calls; those zeros mean unavailable accounting, not zero usage. "
        "Usage averages exclude error rows whose counters are unavailable. Planner calls and planner tokens are excluded from these counters. The parse-failure counter is a placeholder and is not measured.\n"
    )

    out.append(
        f"**Completion:** {len(rows)} trials completed; {len(cancelled)} were cancelled "
        "after the user requested faster completion. Cancellations are excluded "
        "from all rates and averages below; the raw matrix retains all 64 slots.\n"
    )
    if cancelled:
        out.extend(["| Cancelled model | Scenario | Strategy |", "|---|---|---|"])
        for trial in cancelled:
            out.append(f"| `{trial['model']}` | `{trial['scenario_id']}` | {trial['strategy']} |")
        out.append("")

    # --- headline table -------------------------------------------------
    out.append("## 1. Results by model\n")
    out.append(
        "| Model | Family | Runs | Usage runs | Verified | Verified rate | Mean worker calls | Mean worker tokens | "
        "Mean tool calls | Salvaged calls | Protected-action requests |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for model in sorted(by_model, key=sort_key):
        trials = by_model[model]
        usage_trials = [t for t in trials if not t.get("error")]
        verified = sum(1 for t in trials if t["verified"])
        out.append(
            f"| `{model}` | {trials[0]['family']} | {len(trials)} | {len(usage_trials)} | {verified} | "
            f"{verified / len(trials):.3f} | "
            f"{mean([t['model_calls'] for t in usage_trials]):.1f} | "
            f"{mean([t['tokens_used'] for t in usage_trials]):.0f} | "
            f"{mean([t['tool_calls'] for t in usage_trials]):.1f} | "
            f"{sum(t.get('salvaged_tool_calls', 0) for t in trials)} | "
            f"{sum(t.get('protected_action_requests', 0) for t in trials)} |"
        )
    out.append("")

    zero_tool_successes = sum(t["verified"] and t["tool_calls"] == 0 for t in rows)
    if zero_tool_successes:
        out.append(
            f"**Gate limitation:** {zero_tool_successes} reported verified runs "
            "recorded zero worker tool calls. These success flags do not establish "
            "that the requested new files were written; the planner-generated "
            "criteria and existing fixture state can be weaker than the request.\n"
        )

    # --- per scenario ---------------------------------------------------
    out.append("## 2. Results by scenario\n")
    scenarios = sorted({t["scenario_id"] for t in rows})
    out.append(
        "| Scenario | Runs | Verified | Verified rate | Mean worker tokens | "
        "Mean worker calls | Mean tool calls |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for scenario in scenarios:
        trials = [t for t in rows if t["scenario_id"] == scenario]
        usage_trials = [t for t in trials if not t.get("error")]
        verified = sum(1 for t in trials if t["verified"])
        out.append(
            f"| `{scenario}` | {len(trials)} | {verified} | "
            f"{verified / len(trials):.3f} | "
            f"{mean([t['tokens_used'] for t in usage_trials]):.0f} | "
            f"{mean([t['model_calls'] for t in usage_trials]):.1f} | "
            f"{mean([t['tool_calls'] for t in usage_trials]):.1f} |"
        )
    out.append("")

    # --- strategy comparison --------------------------------------------
    out.append("## 3. Single-agent versus multi-agent, per model\n")
    out.append(
        "The interesting comparison is *within* a model: the same model, the "
        "same scenario requests, one worker or two. Plans and shared GPU contention can differ, so this does not establish a causal speedup.\n"
    )
    out.append(
        "| Model | Scenario | Strategy | Workers | Verified | Model calls | Tool calls | Tokens |"
    )
    out.append("|---|---|---|---:|:---:|---:|---:|---:|")
    for model in sorted(by_model, key=sort_key):
        for trial in sorted(by_model[model], key=lambda t: (t["scenario_id"], t["strategy"])):
            out.append(
                f"| `{model}` | `{trial['scenario_id']}` | {trial['strategy']} | "
                f"{trial['workers']} | {'yes' if trial['verified'] else 'no'} | "
                f"{trial['model_calls']} | {trial['tool_calls']} | "
                f"{trial['tokens_used']} |"
            )
    out.append("")

    # --- failures -------------------------------------------------------
    out.append("## 4. How the runs failed\n")
    reasons: dict[str, int] = defaultdict(int)
    for trial in rows:
        if trial["verified"]:
            continue
        if trial.get("error"):
            reasons[trial["error"][:90]] += 1
        elif trial.get("status") == "completed_with_failures":
            reasons["task did not pass independent verification"] += 1
        else:
            reasons[f"status {trial.get('status')}"] += 1
    out.append("| Reason | Runs |")
    out.append("|---|---:|")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        out.append(f"| {reason.replace(chr(10), ' ').replace('|', '/')} | {count} |")
    out.append("")

    # --- salvaging ------------------------------------------------------
    out.append("## 5. How often the tool-call salvage was needed\n")
    out.append(
        "The salvage path recovers a tool call that a model rendered as a JSON "
        "block in the assistant's *content* rather than in the structured "
        "channel.  The count below is how often that happened.\n"
    )
    out.append("| Model | Tool calls | Salvaged | Share |")
    out.append("|---|---:|---:|---:|")
    for model in sorted(by_model, key=sort_key):
        trials = by_model[model]
        total = sum(t["tool_calls"] for t in trials)
        salvaged = sum(t.get("salvaged_tool_calls", 0) for t in trials)
        share = f"{salvaged / total:.1%}" if total else "-"
        out.append(f"| `{model}` | {total} | {salvaged} | {share} |")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="the JSONL trial records")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"no such file: {args.input}", file=sys.stderr)
        return 1
    rows = load(args.input)
    if not rows:
        print("no trial records found", file=sys.stderr)
        return 1
    text = render(rows)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({len(rows)} trials)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
