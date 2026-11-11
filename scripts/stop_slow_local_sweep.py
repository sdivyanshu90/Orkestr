"""Preserve completed trials and cancel slow remaining work on user request."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from agent_squad.benchmark.local import LOCAL_MODEL_CATALOG, LOCAL_SCENARIOS, LocalTrial, _aggregate


def main():
    cache = Path.home() / ".cache" / "agent-squad-benchmark"
    out = Path("benchmarks/results")
    source = cache / "sweep.jsonl"
    try:
        finalized = json.loads((cache / "sweep.json").read_text())
    except (OSError, json.JSONDecodeError):
        finalized = None
    if finalized and len(finalized.get("trials", [])) == 64:
        print("The sweep is already finalized; no processes or results changed")
        return
    started = (cache / "sweep.json").stat().st_mtime
    processes = subprocess.run(
        ["ps", "-eo", "pid,ppid,comm,args"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    targets = []
    for line in processes:
        parts = line.split(None, 3)
        if (
            len(parts) == 4
            and parts[2] == "agent-squad"
            and " benchmark --local " in parts[3]
            and Path(f"/proc/{parts[0]}/cwd").resolve() == Path.cwd()
        ):
            targets.append(int(parts[0]))
    if len(targets) > 1:
        raise RuntimeError(f"Expected at most one benchmark process; found {targets}")
    pid = targets[0] if targets else None
    live = []
    for config in Path("/tmp").glob("agent-squad-local-*/data/runs/*/run-config.json"):
        if config.stat().st_mtime >= started:
            payload = json.loads(config.read_text())
            payload["_created_at"] = config.stat().st_mtime
            live.append(payload)
    if pid:
        os.kill(pid, signal.SIGINT)
        deadline = time.monotonic() + 20
        while Path(f"/proc/{pid}/exe").exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        if Path(f"/proc/{pid}/exe").exists():
            os.kill(pid, signal.SIGTERM)
    rows = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    existing = {(r["model"], r["scenario_id"], r["strategy"], r["trial"]) for r in rows}
    now = time.time()
    cancelled = [r for r in rows if r["status"] == "cancelled_for_time_budget"]
    new_cancelled = []
    for spec in LOCAL_MODEL_CATALOG:
        for scenario, body in LOCAL_SCENARIOS.items():
            for strategy in ("single", "static_two"):
                if (spec.id, scenario, strategy, 0) in existing:
                    continue
                matching = [
                    c
                    for c in live
                    if c["provider"]["model"] == spec.id
                    and c["strategy_override"] == strategy
                    and c.get("metadata", {}).get("objective") == body["objective"]
                ]
                config = matching[-1] if matching else None
                row = LocalTrial(
                    model=spec.id,
                    family=spec.family,
                    scenario_id=scenario,
                    strategy=strategy,
                    workers=1 if strategy == "single" else 2,
                    trial=0,
                    run_id=config["run_id"] if config else "-",
                    status="cancelled_for_time_budget",
                    verified=False,
                    tokens_used=0,
                    cost_usd=0.0,
                    wall_clock_s=round(now - config["_created_at"], 3) if config else 0.0,
                    model_calls=0,
                    tool_calls=0,
                    tool_call_parse_failures=0,
                    verification_failures=0,
                    error="Cancelled after user requested faster completion; not a model correctness failure",
                    notes=[
                        "Interrupted or unstarted trial; zero usage counters mean unavailable, not measured zero.",
                        "Elapsed time is approximate from run-config creation; zero if not started.",
                    ],
                ).to_dict()
                rows.append(row)
                cancelled.append(row)
                new_cancelled.append(row)
    assert len(rows) == 64
    with source.open("a") as stream:
        for row in new_cancelled:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    report = {
        "suite": "local-models",
        "started_at": started,
        "finished_at": now,
        "duration_s": round(now - started, 2),
        "models": [m.to_dict() for m in LOCAL_MODEL_CATALOG],
        "scenarios": list(LOCAL_SCENARIOS),
        "strategies": ["single", "static_two"],
        "trials_per_combination": 1,
        "parallel_models": 2,
        "parallel_runs": 3,
        "server_stats": [],
        "trials": rows,
        "aggregates": _aggregate(
            [
                LocalTrial(**{"cost_usd": 0.0, **r})
                for r in rows
                if r["status"] != "cancelled_for_time_budget"
            ]
        ),
        "completed_trials": len(rows) - len(cancelled),
        "cancelled_trials": len(cancelled),
        "notes": [
            "User requested faster completion. Remaining long workflow trials were cancelled; completed trials were retained.",
            "Cancelled rows are not model correctness failures; their counters are unavailable.",
            "Startup statistics were not persisted before interruption; GPU settings and evidence are in server logs, manifest and methodology.",
            "Wall time and success rates under this concurrent configuration are not controlled model rankings.",
        ],
    }
    temporary = cache / "sweep.final.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(cache / "sweep.json")
    (out / "local-cancellation.json").write_text(
        json.dumps(
            {
                "requested_reason": "User: Speed it up.",
                "benchmark_pid": pid,
                "finished_at": now,
                "completed_trials": len(rows) - len(cancelled),
                "cancelled_trials": len(cancelled),
                "cancelled_keys": [
                    (r["model"], r["scenario_id"], r["strategy"]) for r in cancelled
                ],
            },
            indent=2,
        )
        + "\n"
    )
    print(
        f"Preserved {len(rows) - len(cancelled)} completed trials; marked {len(cancelled)} cancellations"
    )


if __name__ == "__main__":
    main()
