"""A timing sample whose process ownership is verified.

`docs/scoring-criteria.md` M1 says the qualification timing sample must be
taken with "recorded process occupancy and **no surviving benchmark-owned
server from a prior configuration**".  The historical sweep could not say
that: a server was still alive at the end of it, which is why that sample
is retained with a caveat and is not qualification evidence.

This script takes a *small* sample the right way, so that the mechanism
is demonstrated even though the sample is not large enough to qualify
anything on its own:

* before any server starts, it records the hardware baseline and checks
  that no previously owned server is alive;
* every server it starts is registered in a durable
  :class:`~agent_squad.runtime.registry.ResourceRegistry` **while it is
  alive**, so an interrupted run leaves a record;
* after each model, the server is stopped and the script verifies that the
  recorded PID is gone and the port is closed;
* after the last model, the registry is reconciled and every recorded
  process is checked a second time.

The output distinguishes cold and warm requests, and records the exact
model file, quantization, server arguments and context size, because a
timing number without those is not reproducible.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import socket
import sys
import time
from typing import Any

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_squad.benchmark.local import (  # noqa: E402
    LOCAL_MODEL_CATALOG,
    LlamaServer,
    ServerConfig,
    free_vram_mb,
    total_ram_mb,
)
from agent_squad.runtime.registry import (  # noqa: E402
    ResourceRegistry,
    reconcile,
)

OUT = ROOT / "benchmarks" / "results" / "local-clean-timing.json"
OWNERSHIP_DIR = ROOT / "benchmarks" / "results" / "ownership"

#: The smallest models still present on disk.  The full catalogue is
#: recorded in the output as `catalog`, so the reader can see exactly which
#: models the sample covers and which it does not.
PROMPTS = (
    "Return only the integer result of 17 * 23.",
    "Return only the word yes or no: is 9 prime?",
    "Return only a JSON object with key name and the value Ada.",
)
REPETITIONS = 2


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def _pid_alive(pid: int) -> bool:
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        fields = stat[stat.rindex(")") + 1 :].split()
        return not (fields and fields[0] == "Z")
    except (ValueError, IndexError):
        return False


async def _measure(handle: Any) -> list[dict[str, Any]]:
    """Warm up once, then take the timed samples."""
    samples: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        warmup_started = time.monotonic()
        await client.post(
            handle.base_url + "/chat/completions",
            json={
                "model": handle.spec.id,
                "messages": [{"role": "user", "content": "Say OK."}],
                "max_tokens": 8,
                "temperature": 0,
                "seed": 42,
            },
        )
        warmup_s = time.monotonic() - warmup_started
        for repetition in range(REPETITIONS):
            for index, prompt in enumerate(PROMPTS):
                started = time.monotonic()
                response = await client.post(
                    handle.base_url + "/chat/completions",
                    json={
                        "model": handle.spec.id,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 48,
                        "temperature": 0,
                        "seed": 42,
                    },
                )
                elapsed = time.monotonic() - started
                body = response.json() if response.status_code == 200 else {}
                usage = body.get("usage") or {}
                samples.append(
                    {
                        "repetition": repetition,
                        "prompt_index": index,
                        "cold": repetition == 0 and index == 0,
                        "warmup_s": round(warmup_s, 4),
                        "duration_s": round(elapsed, 4),
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "status": response.status_code,
                    }
                )
    return samples


async def main() -> int:
    OWNERSHIP_DIR.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    baseline = {
        "free_vram_mb": free_vram_mb(),
        "total_ram_mb": total_ram_mb(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Before starting anything: refuse to produce a timing sample on a host
    # that already has a benchmark-owned server alive.
    preexisting: list[str] = []
    for registry_path in sorted(OWNERSHIP_DIR.glob("*.json")):
        registry = ResourceRegistry.load(registry_path)
        for resource in registry.resources:
            if resource.pid is None:
                continue
            if _pid_alive(resource.pid):
                preexisting.append(f"{registry_path.name}: pid {resource.pid}")
    if preexisting:
        print("Refusing to take a timing sample: a prior server is alive:")
        for item in preexisting:
            print("  " + item)
        return 2

    catalogue = [
        {
            "id": spec.id,
            "family": spec.family,
            "filename": spec.filename,
            "size_mb": spec.size_mb,
            "quant": spec.quant,
            "present": spec.local_path().exists(),
        }
        for spec in LOCAL_MODEL_CATALOG
    ]
    selected = [spec for spec in LOCAL_MODEL_CATALOG if spec.local_path().exists()]
    if not selected:
        print("No model weights are present; nothing to measure.")
        return 2

    results: list[dict[str, Any]] = []
    for spec in selected:
        registry_path = OWNERSHIP_DIR / f"{spec.id}.json"
        registry = ResourceRegistry(registry_path)
        config = ServerConfig(
            registry=registry,
            gpu_layers=None,
            extra_args=("--parallel", "1", "--ctx-size", "2048"),
            startup_timeout_s=240,
        )
        server = LlamaServer(config)
        handle = None
        record: dict[str, Any] = {
            "model": spec.id,
            "family": spec.family,
            "quant": spec.quant,
            "size_mb": spec.size_mb,
            "context_size": 2048,
            "server_args": list(config.extra_args),
        }
        try:
            handle = server.start(spec, spec.local_path())
            record["load_s"] = round(handle.load_seconds, 3)
            record["gpu_layers"] = handle.gpu_layers
            record["port"] = handle.port
            record["pid"] = handle.process.pid
            record["samples"] = await _measure(handle)
        finally:
            if handle is not None:
                handle.stop()
            server.stop()

        # Ownership verification, from outside the process that owned it.
        time.sleep(0.5)
        pid_gone = handle is None or not _pid_alive(handle.process.pid)
        port_closed = handle is None or not _port_open(handle.port)
        report = reconcile(registry, grace_s=5.0)
        record["ownership"] = {
            "pid": None if handle is None else handle.process.pid,
            "pid_gone": pid_gone,
            "port_closed": port_closed,
            "registry_clean": report.clean,
            "survivors": list(report.survivors),
        }
        durations = [s["duration_s"] for s in record.get("samples", [])]
        record["median_s"] = round(sorted(durations)[len(durations) // 2], 4) if durations else None
        results.append(record)
        print(
            f"{spec.id}: pid_gone={pid_gone} port_closed={port_closed} "
            f"median={record['median_s']}s",
            flush=True,
        )

    payload = {
        "schema_version": 1,
        "purpose": (
            "A small timing sample taken with verified process ownership. "
            "It demonstrates the mechanism; it is not large enough to be "
            "qualification evidence under M1, and it does not cover the "
            "models that are no longer on disk."
        ),
        "hardware": baseline,
        "catalogue": catalogue,
        "models_measured": [r["model"] for r in results],
        "repetitions": REPETITIONS,
        "prompts": list(PROMPTS),
        "results": results,
        "all_ownership_verified": all(
            r["ownership"]["pid_gone"]
            and r["ownership"]["port_closed"]
            and r["ownership"]["registry_clean"]
            for r in results
        ),
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0 if payload["all_ownership_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
