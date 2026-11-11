"""Sequential, fixed-score local inference probes plus a Qwen CPU baseline.

Requires the existing benchmark downloads; never downloads new weights. Each
model gets the same five prompts, output cap, seed and scoring rules. Raw API
responses and server logs are retained. This is a smoke benchmark, not a broad
quality leaderboard. Run only after the orchestration sweep finishes.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import re
import subprocess
import time
from pathlib import Path

import httpx

from agent_squad.benchmark.local import LOCAL_MODEL_CATALOG, LlamaServer, ServerConfig

OUT = Path("benchmarks/results")
TASKS = [
    (
        "arithmetic",
        "Return only a JSON object with key answer and the integer result of 17 * 23.",
        {"answer": 391},
    ),
    (
        "extraction",
        'From "Ada bought 3 apples", return only a JSON object with keys name (string) and count (integer).',
        {"name": "Ada", "count": 3},
    ),
    (
        "sorting",
        "Sort [2, -1, 9, 2, 0] ascending. Return only a JSON object with key values and the sorted array.",
        {"values": [-1, 0, 2, 2, 9]},
    ),
    (
        "python_square",
        "Return only a JSON object with key code containing Python source defining def square(x): which returns x multiplied by itself. No imports.",
        None,
    ),
    ("tool_call", "Call record_value with value 42. Use the provided tool.", None),
]
TOOL = {
    "type": "function",
    "function": {
        "name": "record_value",
        "description": "Record an integer.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    },
}


def score(task: str, message: dict, expected: dict | None) -> bool:
    if task == "tool_call":
        calls = message.get("tool_calls") or []
        return (
            len(calls) == 1
            and calls[0]["function"]["name"] == "record_value"
            and json.loads(calls[0]["function"]["arguments"]) == {"value": 42}
        )
    content = message.get("content") or ""
    # Explicitly permit common wrappers; do not extract arbitrary substrings.
    content = re.sub(r"^\s*<think>.*?</think>\s*", "", content, flags=re.S).strip()
    if content.startswith("```") and content.endswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)[:-3].strip()
    value = json.loads(content)
    if task != "python_square":
        return value == expected
    code = value["code"]
    if not isinstance(code, str) or len(code) > 2048:
        return False
    tree = ast.parse(code)
    # Execute only a tiny, checked expression function. No calls, imports,
    # attributes, indexing, loops, defaults or access to builtins are allowed.
    allowed = (
        ast.Module,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.Return,
        ast.BinOp,
        ast.Mult,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Expr,
    )
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        return False
    if any(
        isinstance(node, ast.Constant)
        and (type(node.value) not in (int, float) or abs(node.value) > 1_000_000)
        for node in ast.walk(tree)
    ):
        return False
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return False
    function = tree.body[0]
    if (
        function.name != "square"
        or len(function.args.args) != 1
        or function.args.defaults
        or function.decorator_list
    ):
        return False
    namespace = {"__builtins__": {}}
    exec(compile(tree, "<benchmark-square>", "exec"), namespace)
    return all(namespace["square"](x) == x * x for x in (-3, 0, 2, 1.5))


def telemetry() -> dict:
    proc = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {"time": time.time(), "gpu": proc.stdout.strip(), "error": proc.stderr.strip()}


async def main() -> None:
    sweep = Path.home() / ".cache" / "agent-squad-benchmark" / "sweep.json"
    deadline = time.monotonic() + 3600
    while not sweep.exists() or not sweep.stat().st_size:
        if time.monotonic() > deadline:
            raise TimeoutError("Orchestration sweep did not finish within one hour")
        print("Waiting for orchestration sweep before sequential inference probes...", flush=True)
        await asyncio.sleep(20)
    report = json.loads(sweep.read_text())
    if len(report["trials"]) != 64:
        raise RuntimeError("Expected accounting for all 64 workflow slots, including explicit cancellations")
    OUT.mkdir(parents=True, exist_ok=True)
    progress = OUT / "local-inference.jsonl"
    if progress.exists():
        raise RuntimeError(f"{progress} already exists; retain or move previous measurements first")
    records = []
    configurations = [(spec, "gpu") for spec in LOCAL_MODEL_CATALOG] + [
        (LOCAL_MODEL_CATALOG[0], "cpu")
    ]
    for spec, device in configurations:
        # Keep the largest model partly in RAM rather than requesting more than
        # 4 GiB of device memory. One loaded model and one request at a time.
        layers = 0 if device == "cpu" else (12 if spec.size_mb > 3200 else 999)
        server = LlamaServer(
            ServerConfig(
                gpu_layers=layers,
                extra_args=(
                    "--parallel",
                    "1",
                    "--ctx-size",
                    "4096",
                    "--batch-size",
                    "128",
                    "--ubatch-size",
                    "128",
                    "--verbose",
                ),
                startup_timeout_s=180,
            )
        )
        handle = None
        samples = []
        stop = asyncio.Event()

        async def sample(stop=stop, samples=samples) -> None:
            while not stop.is_set():
                samples.append(telemetry())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 1)

        sampler = None
        try:
            handle = server.start(spec, spec.local_path())
            sampler = asyncio.create_task(sample())
            async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
                # Same untimed warmup for every configuration.
                warmup = await client.post(
                    handle.base_url + "/chat/completions",
                    json={
                        "model": spec.id,
                        "messages": [{"role": "user", "content": "Say OK."}],
                        "max_tokens": 16,
                        "temperature": 0,
                        "seed": 42,
                    },
                )
                (OUT / f"warmup-{spec.id}-{device}.json").write_text(warmup.text)
                for task, prompt, expected in TASKS:
                    body = {
                        "model": spec.id,
                        "messages": [
                            {
                                "role": "system",
                                "content": "Follow instructions precisely. Answer directly without explaining your reasoning.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 256,
                        "temperature": 0,
                        "seed": 42,
                    }
                    if task == "tool_call":
                        body["tools"] = [TOOL]
                        body["tool_choice"] = "auto"
                    row = {
                        "model": spec.id,
                        "family": spec.family,
                        "device": device,
                        "task": task,
                        "request": body,
                        "expected": expected,
                        "started_at": time.time(),
                        "passed": False,
                        "error": None,
                    }
                    began = time.monotonic()
                    try:
                        response = await client.post(
                            handle.base_url + "/chat/completions", json=body
                        )
                        row["elapsed_s"] = time.monotonic() - began
                        row["http_status"] = response.status_code
                        row["response"] = response.json()
                        response.raise_for_status()
                        row["passed"] = score(
                            task, row["response"]["choices"][0]["message"], expected
                        )
                    except Exception as exc:
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        row.setdefault("elapsed_s", time.monotonic() - began)
                    row["load_seconds"] = handle.load_seconds
                    row["gpu_layers_requested"] = layers
                    row["actual_slots"] = 1
                    row["actual_context"] = 4096
                    records.append(row)
                    with progress.open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                    print(
                        f"{spec.id} {device} {task}: passed={row['passed']} {row['elapsed_s']:.2f}s",
                        flush=True,
                    )
        finally:
            stop.set()
            if sampler:
                await sampler
            server.stop()
            if handle and handle.stderr_path:
                (OUT / f"inference-{spec.id}-{device}.log").write_bytes(
                    handle.stderr_path.read_bytes()
                )
            (OUT / f"telemetry-{spec.id}-{device}.json").write_text(json.dumps(samples, indent=2))
    (OUT / "local-inference.json").write_text(
        json.dumps(
            {
                "tasks": TASKS,
                "max_tokens": 256,
                "temperature": 0,
                "seed": 42,
                "warmup_tokens": 16,
                "parallel_models": 1,
                "parallel_requests": 1,
                "trials_per_task": 1,
                "records": records,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
