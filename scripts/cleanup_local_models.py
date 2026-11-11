"""Remove this benchmark's cached weights and optional inference runtime.

Dry run by default; use --yes after preserving results. Only servers executing
from this benchmark's runtime directory are stopped. Unrelated model caches and
servers are left alone. --report saves a machine-readable deletion receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path


def default_roots() -> list[Path]:
    cache = Path.home() / ".cache" / "agent-squad-benchmark"
    models = Path(os.environ.get("AGENT_SQUAD_LOCAL_MODELS", str(cache / "models")))
    # An external server override may point into a shared installation. Only
    # the benchmark-owned default runtime directory is eligible for removal.
    return [models, cache / "localmodels", cache / "llamacpp"]


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def stop_servers(runtime: Path) -> list[int]:
    """Stop only llama-server executables inside the selected runtime root."""
    processes = []
    runtime = runtime.resolve()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            executable = (proc / "exe").resolve(strict=True)
            if executable.name == "llama-server" and executable.is_relative_to(runtime):
                pid = int(proc.name)
                os.kill(pid, signal.SIGTERM)
                processes.append(pid)
        except (OSError, RuntimeError):
            continue
    deadline = time.monotonic() + 20
    for pid in processes:
        while Path(f"/proc/{pid}/exe").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if Path(f"/proc/{pid}/exe").exists():
            os.kill(pid, signal.SIGKILL)
    return processes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    roots = default_roots()
    runtime = roots[-1]
    if args.keep_server:
        roots = roots[:-1]
    # Refuse dangerous environment overrides before deleting anything.
    for root in roots:
        resolved = root.resolve()
        if (
            resolved in (Path("/"), Path.home(), Path.home() / ".cache", Path.cwd())
            or len(resolved.parts) < 4
        ):
            parser.error(f"unsafe cleanup root: {resolved}")
    records = []
    for root in roots:
        record = {
            "path": str(root),
            "bytes_before": dir_size(root),
            "existed_before": root.exists(),
        }
        records.append(record)
        print(f"{root}: {record['bytes_before']:,} bytes")
    if not args.yes:
        print("Dry run; pass --yes to remove these benchmark artifacts.")
        return 0
    stopped = stop_servers(runtime)
    errors = []
    for record, root in zip(records, roots, strict=True):
        try:
            if root.is_symlink() or root.is_file():
                root.unlink()
            elif root.exists():
                shutil.rmtree(root)
        except OSError as exc:
            errors.append(f"{root}: {exc}")
        record["absent_after"] = not root.exists() and not root.is_symlink()
    receipt = {
        "finished_at": time.time(),
        "roots": records,
        "stopped_server_pids": stopped,
        "errors": errors,
        "complete": not errors and all(r["absent_after"] for r in records),
        "scope": "Benchmark-owned model caches and selected runtime only; unrelated user models are not inspected or removed.",
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
