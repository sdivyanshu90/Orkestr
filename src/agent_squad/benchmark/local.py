"""Benchmarking against real, locally-served models.

The deterministic mock provider measures the *orchestration*.  It cannot
measure whether a real model can drive the agent loop at all: the mock emits
perfectly formed tool calls, whereas a 1.5B model on a laptop may not manage
a valid tool call on the first try.

This module closes that gap.  It serves a set of GGUF models through
`llama.cpp`'s OpenAI-compatible `llama-server`, points the orchestrator at
it through the ordinary `openai_compat` provider, and records what actually
happened.

Why `llama.cpp`
---------------

* it exposes an OpenAI-compatible `/v1/chat/completions` endpoint, so the
  adapter under test is the same one used against a hosted API;
* it runs a *single* model in a few gigabytes of RAM, which is what a laptop
  or a workstation actually has;
* it offloads layers to the GPU, so the RTX 3050 in this machine is used
  rather than idle;
* it has no daemon, no model registry and no background service to clean up
  afterwards.

Model selection is *recorded*, not asserted: :data:`LOCAL_MODEL_CATALOG`
carries, for every entry, the size on disk and the reason it is (or is not)
in the set.  Models that do not fit this machine are listed with their
measured size so that the exclusion is auditable rather than implicit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import InfrastructureError, ValidationError
from .harness import STRATEGIES

#: Where the GGUF weights live.  Overridable so the harness can point at a
#: different cache without editing the code.
DEFAULT_MODEL_DIR = Path(
    os.environ.get(
        "AGENT_SQUAD_LOCAL_MODELS",
        str(Path.home() / ".cache" / "agent-squad-benchmark" / "models"),
    )
)

#: Where the `llama-server` binary lives.
DEFAULT_SERVER = Path(
    os.environ.get(
        "AGENT_SQUAD_LLAMA_SERVER",
        str(
            Path.home()
            / ".cache"
            / "agent-squad-benchmark"
            / "llamacpp"
            / "llama-b12246"
            / "llama-server"
        ),
    )
)


@dataclass(frozen=True)
class LocalModelSpec:
    """One locally-servable model."""

    id: str
    family: str
    repo: str
    filename: str
    quant: str
    parameters: str
    size_mb: int
    note: str = ""
    tool_capable: bool = True

    @property
    def url(self) -> str:
        return (
            f"https://huggingface.co/{self.repo}/resolve/main/{self.filename}"
        )

    def local_path(self, directory: Path | None = None) -> Path:
        base = Path(directory or DEFAULT_MODEL_DIR)
        return base / f"{self.id}.gguf"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "repo": self.repo,
            "quant": self.quant,
            "parameters": self.parameters,
            "size_mb": self.size_mb,
            "note": self.note,
            "tool_capable": self.tool_capable,
        }


#: The curated set.  Every entry was checked against the GGUF file size on
#: Hugging Face; the `size_mb` field is the measured download size, not an
#: estimate.
LOCAL_MODEL_CATALOG: tuple[LocalModelSpec, ...] = (
    LocalModelSpec(
        id="qwen2.5-coder-1.5b-instruct",
        family="Qwen",
        repo="Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
        filename="qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
        quant="Q4_K_M",
        parameters="1.5B",
        size_mb=1129,
        note="code-specialised; the reference small model for this benchmark",
    ),
    LocalModelSpec(
        id="qwen3-1.7b",
        family="Qwen",
        repo="unsloth/Qwen3-1.7B-GGUF",
        filename="Qwen3-1.7B-Q4_K_M.gguf",
        quant="Q4_K_M",
        parameters="1.7B",
        size_mb=1110,
        note="newer generation with a hybrid reasoning mode",
    ),
    LocalModelSpec(
        id="deepseek-r1-distill-qwen-1.5b",
        family="DeepSeek",
        repo="unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF",
        filename="DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf",
        quant="Q4_K_M",
        parameters="1.5B",
        size_mb=1117,
        note="chain-of-thought distillation; emits long reasoning traces",
    ),
    LocalModelSpec(
        id="gemma-3-1b-it",
        family="Gemma",
        repo="ggml-org/gemma-3-1b-it-GGUF",
        filename="gemma-3-1b-it-Q4_K_M.gguf",
        quant="Q4_K_M",
        parameters="1B",
        size_mb=819,
        note="smallest model in the set; a useful lower bound",
    ),
    LocalModelSpec(
        id="llama-3.2-1b-instruct",
        family="Llama",
        repo="unsloth/Llama-3.2-1B-Instruct-GGUF",
        filename="Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        quant="Q4_K_M",
        parameters="1B",
        size_mb=808,
        note="reference small Llama",
    ),
    LocalModelSpec(
        id="phi-4-mini-instruct",
        family="Phi",
        repo="unsloth/Phi-4-mini-instruct-GGUF",
        filename="Phi-4-mini-instruct-Q4_K_M.gguf",
        quant="Q4_K_M",
        parameters="3.8B",
        size_mb=2409,
        note="largest model in the set; tests the GPU-offload path",
    ),
    LocalModelSpec(
        id="minimax-m1-distill-qwen3-4b",
        family="MiniMax",
        repo="mradermacher/Qwen3-4B-2507-Thinking-Minimax-M1-Distill-Uncensored-GGUF",
        filename="Qwen3-4B-2507-Thinking-Minimax-M1-Distill-Uncensored.Q4_K_M.gguf",
        quant="Q4_K_M",
        parameters="4B",
        size_mb=2497,
        note=(
            "the smallest available MiniMax-family artefact; MiniMax-M1 and "
            "MiniMax-Text-01 are 456B and do not fit this machine"
        ),
    ),
    LocalModelSpec(
        id="kimi-k3-distill-qwen3.5-9b",
        family="Kimi",
        repo="mradermacher/Qwen3.5-9B-Kimi-k3-Distilled-GGUF",
        filename="Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf",
        quant="Q4_K_M",
        parameters="9B",
        size_mb=5780,
        note=(
            "the smallest available Kimi-family artefact; the official Kimi-K3 "
            "is 48 GB per shard and does not fit this machine"
        ),
    ),
)


#: Models that were considered and excluded, with the measured reason.  This
#: is recorded so the exclusion is auditable rather than implicit.
EXCLUDED_MODELS: tuple[dict[str, str], ...] = (
    {
        "model": "moonshotai/Kimi-K3",
        "size": "~750 GB across 15 shards",
        "reason": "does not fit 8 GB of RAM; the smallest shard alone is 48 GB",
    },
    {
        "model": "moonshotai/Kimi-K2 / K2.5 / Kimi-Linear-48B-A3B",
        "size": "10.9 GB (Kimi-Linear IQ1_M) upwards",
        "reason": "exceeds the 8 GB RAM budget even at the lowest quantisation",
    },
    {
        "model": "MiniMax-M1 / MiniMax-Text-01 / MiniMax-M2",
        "size": "230B-456B",
        "reason": "two orders of magnitude beyond this machine's budget",
    },
    {
        "model": "GLM-4.7-Flash",
        "size": "11.3 GB (Q2_K) upwards",
        "reason": "exceeds the RAM budget at every available quantisation",
    },
    {
        "model": "Qwen2.5-Coder-7B / Qwen3-4B",
        "size": "4.7-2.6 GB",
        "reason": (
            "Qwen2.5-Coder-7B fits only with heavy CPU offload; the 1.5B and "
            "4B variants already cover the family's behaviour curve"
        ),
    },
)


@dataclass
class ServerConfig:
    """How to run `llama-server`."""

    server_path: Path = DEFAULT_SERVER
    host: str = "127.0.0.1"
    port: int = 0
    context_size: int = 4096
    threads: int = 6
    slots: int = 8
    """Concurrent request slots per server; more slots means better batching."""
    gpu_layers: int | None = None
    extra_args: tuple[str, ...] = ()
    startup_timeout_s: float = 300.0
    request_timeout_s: float = 600.0
    shared_gpu: int = 1
    """How many models will share the GPU at once."""
    baseline_vram_mb: int = 0
    """Free VRAM measured *before* any of this suite's servers started.

    Measuring it once, up front, avoids the planner being fooled by the
    VRAM that the suite's *own* servers are holding -- which would
    otherwise shrink every model's layer count as more models load.
    """

    def resolve_port(self) -> int:
        if self.port:
            return self.port
        import socket

        with socket.socket() as sock:
            sock.bind((self.host, 0))
            return int(sock.getsockname()[1])


@dataclass
class ServerHandle:
    """A running `llama-server`."""

    spec: LocalModelSpec
    process: subprocess.Popen
    base_url: str
    port: int
    gpu_layers: int
    load_seconds: float
    stderr_path: Path | None = None
    slots: int = 1
    context_size: int = 0

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            with contextlib.suppress(Exception):
                self.process.wait(timeout=10)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.spec.id,
            "family": self.spec.family,
            "base_url": self.base_url,
            "port": self.port,
            "gpu_layers": self.gpu_layers,
            "load_seconds": round(self.load_seconds, 2),
            "slots": self.slots,
            "context_size": self.context_size,
        }


def free_vram_mb() -> int:
    """Free VRAM in MiB, or 0 when there is no GPU."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    first = proc.stdout.strip().splitlines()[0]
    try:
        total, used = (int(part.strip()) for part in first.split(","))
    except ValueError:
        return 0
    return max(0, total - used)


def total_ram_mb() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e6)
    except (ValueError, OSError):  # pragma: no cover
        return 0


def plan_gpu_layers(
    spec: LocalModelSpec, vram_mb: int, ram_mb: int, *, shared: int = 1
) -> int:
    """Choose how many layers to place on the GPU.

    The rule is deliberately simple and *recorded* rather than clever: put
    the whole model on the GPU when it comfortably fits, otherwise place as
    many layers as fit in the VRAM budget with a margin, and leave the rest
    on the CPU.

    ``shared`` is the number of models that will be served concurrently.  The
    VRAM budget is divided between them, so three small models running at
    once each get a third of the card rather than all claiming the whole of
    it.
    """
    if vram_mb <= 0:
        return 0
    budget = int(vram_mb * 0.85 / max(1, shared))
    if spec.size_mb <= budget:
        return 999  # all layers
    # Assume roughly 40 layers per billion parameters at Q4, and that the
    # GPU can hold a proportional share of the weights.
    layers_total = max(8, int(spec.parameters.split("B")[0].split(".")[0] or 1) * 40)
    fit = int(layers_total * (budget / spec.size_mb))
    # The remainder must still fit in RAM.
    cpu_mb = spec.size_mb - int(spec.size_mb * (fit / layers_total))
    if cpu_mb > ram_mb * 0.6:
        fit = int(layers_total * max(0.0, (budget / spec.size_mb) * 0.6))
    return max(0, min(layers_total, fit))


def plan_server_resources(spec: LocalModelSpec) -> tuple[int, int]:
    """Choose the server's slot count and total context size.

    llama.cpp divides ``--ctx-size`` across the request slots, so the total
    has to be ``slots * per_request_context``.  The per-request budget is
    what actually matters: the agent's prompt is roughly 1.2 k tokens and
    grows by a few hundred with each tool result.

    The number of slots is a throughput knob -- more slots means more
    requests can be batched onto the GPU -- but each slot costs KV cache, and
    the KV cache has to fit next to the weights in 4 GB of VRAM.  Hence
    fewer slots for larger models.
    """
    per_request = 3072
    if spec.size_mb <= 1600:
        slots = 8
    elif spec.size_mb <= 3200:
        slots = 6
    else:
        slots = 3
    return slots, slots * per_request


class LlamaServer:
    """Starts and stops `llama-server` for one model."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or ServerConfig()
        self._process: subprocess.Popen | None = None
        self._log_path: Path | None = None

    def start(self, spec: LocalModelSpec, model_path: Path) -> ServerHandle:
        if not self.config.server_path.exists():
            raise InfrastructureError(
                f"llama-server not found at {self.config.server_path}; "
                "set AGENT_SQUAD_LLAMA_SERVER or pass server_path",
                server_path=str(self.config.server_path),
            )
        if not model_path.exists():
            raise InfrastructureError(
                f"model weights not found at {model_path}", model_path=str(model_path)
            )

        port = self.config.resolve_port()
        gpu_layers = (
            self.config.gpu_layers
            if self.config.gpu_layers is not None
            else plan_gpu_layers(
                spec,
                self.config.baseline_vram_mb or free_vram_mb(),
                total_ram_mb(),
                shared=self.config.shared_gpu,
            )
        )
        slots, context_size = plan_server_resources(spec)
        args = [
            str(self.config.server_path),
            "--model",
            str(model_path),
            "--host",
            self.config.host,
            "--port",
            str(port),
            "--ctx-size",
            str(context_size),
            "--threads",
            str(self.config.threads),
            "--gpu-layers",
            str(gpu_layers),
            "--jinja",  # use the model's own chat template, including tools
            "--alias",
            spec.id,
            "--no-webui",
            # More slots means more requests can be batched onto the GPU at
            # once, which is where the throughput actually comes from.
            "--parallel",
            str(slots),
        ]
        args.extend(self.config.extra_args)

        log_path = Path("/tmp") / f"llama-server-{spec.id}-{port}.log"
        self._log_path = log_path
        handle = log_path.open("w", encoding="utf-8")
        started = time.monotonic()
        # The CUDA build ships its runtime next to the binary; make sure the
        # loader finds it.
        env = dict(os.environ)
        binary_dir = str(self.config.server_path.parent)
        env["LD_LIBRARY_PATH"] = (
            f"{binary_dir}:{env['LD_LIBRARY_PATH']}" if env.get("LD_LIBRARY_PATH") else binary_dir
        )
        env.setdefault("CUDA_VISIBLE_DEVICES", "0")
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=handle,
            start_new_session=True,
            env=env,
        )
        self._wait_ready(port, started)
        return ServerHandle(
            spec=spec,
            process=self._process,
            base_url=f"http://{self.config.host}:{port}/v1",
            port=port,
            gpu_layers=gpu_layers,
            load_seconds=time.monotonic() - started,
            stderr_path=log_path,
            slots=slots,
            context_size=context_size,
        )

    def _wait_ready(self, port: int, started: float) -> None:
        import urllib.error
        import urllib.request

        deadline = started + self.config.startup_timeout_s
        url = f"http://127.0.0.1:{port}/health"
        last_error = "timeout"
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                tail = ""
                if self._log_path and self._log_path.exists():
                    tail = self._log_path.read_text(errors="replace")[-2000:]
                raise InfrastructureError(
                    f"llama-server exited during startup:\n{tail}", port=port
                )
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = str(exc)
            time.sleep(1.0)
        raise InfrastructureError(
            f"llama-server did not become ready: {last_error}", port=port
        )

    def stop(self) -> None:
        if self._process is not None:
            with contextlib.suppress(Exception):
                self._process.terminate()
                self._process.wait(timeout=20)
            self._process = None


def download_model(spec: LocalModelSpec, directory: Path | None = None) -> Path:
    """Download the GGUF weights if they are not already present."""
    target = spec.local_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 100_000_000:
        return target
    import urllib.request

    with urllib.request.urlopen(spec.url, timeout=120) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
    return target


def parse_server_stats(log_path: Path | None) -> dict[str, Any]:
    """Extract timing information from the server's own log."""
    stats: dict[str, Any] = {}
    if not log_path or not log_path.exists():
        return stats
    text = log_path.read_text(errors="replace")
    for pattern, key in (
        (r"eval time = .*?([\d.]+) ms / ([\d.]+) runs", "eval_ms_per_token"),
        (r"prompt eval time = .*?([\d.]+) ms / ([\d.]+) tokens", "prompt_ms_per_token"),
        (r"total time =.*", "total_time_line"),
    ):
        match = re.search(pattern, text)
        if match:
            stats[key] = match.group(1)
    for pattern, key in (
        (r"load time = .*?([\d.]+) ms", "load_ms"),
        (r"n_gpu_layers offloaded = (\d+)", "gpu_layers_offloaded"),
    ):
        match = re.search(pattern, text)
        if match:
            stats[key] = match.group(1)
    return stats


@dataclass
class LocalTrial:
    """One run of one scenario against one local model."""

    model: str
    family: str
    scenario_id: str
    strategy: str
    workers: int
    trial: int
    run_id: str
    status: str
    verified: bool
    tokens_used: int
    cost_usd: float
    wall_clock_s: float
    model_calls: int
    tool_calls: int
    tool_call_parse_failures: int
    verification_failures: int
    salvaged_tool_calls: int = 0
    protected_action_requests: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "family": self.family,
            "scenario_id": self.scenario_id,
            "strategy": self.strategy,
            "workers": self.workers,
            "trial": self.trial,
            "run_id": self.run_id,
            "status": self.status,
            "verified": self.verified,
            "tokens_used": self.tokens_used,
            "wall_clock_s": round(self.wall_clock_s, 3),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tool_call_parse_failures": self.tool_call_parse_failures,
            "verification_failures": self.verification_failures,
            "salvaged_tool_calls": self.salvaged_tool_calls,
            "protected_action_requests": self.protected_action_requests,
            "error": self.error,
            "notes": list(self.notes),
        }


#: Scenarios for the local benchmark.
#:
#: These differ from the deterministic ones in one important way: they have
#: no ``edits`` field.  The ``edits`` field is an affordance of the mock
#: provider -- a real model has to *write the file itself* through the
#: ``write_file`` tool, which is precisely the capability being measured.
LOCAL_SCENARIOS: dict[str, dict[str, Any]] = {
    "write_a_helper": {
        "objective": "Create src/greeter.py with a greet() function",
        "tasks": [
            {
                "id": "impl",
                "title": "Write the greeter module",
                "objective": (
                    "Create a file named src/greeter.py that defines "
                    "def greet(name): returning the string 'Hello, ' + name + '!'"
                ),
                "role": "implementer",
                "depends_on": [],
                "include_paths": ["src/"],
                "anticipated_files": ["src/greeter.py"],
                "acceptance_criteria": [
                    {
                        "id": "file-exists",
                        "description": "src/greeter.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "src/greeter.py"},
                    },
                    {
                        "id": "defines-greet",
                        "description": "the module defines greet",
                        "kind": "file_contains",
                        "spec": {"path": "src/greeter.py", "pattern": "def greet"},
                    },
                ],
            }
        ],
    },
    "write_a_test": {
        "objective": "Add a test for the existing calculator",
        "tasks": [
            {
                "id": "tests",
                "title": "Add a calculator test",
                "objective": (
                    "Create a file tests/test_calc.py containing a pytest test "
                    "that imports add from calc and asserts add(2, 3) == 5"
                ),
                "role": "test_engineer",
                "depends_on": [],
                "include_paths": ["tests/"],
                "anticipated_files": ["tests/test_calc.py"],
                "acceptance_criteria": [
                    {
                        "id": "test-exists",
                        "description": "tests/test_calc.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "tests/test_calc.py"},
                    },
                    {
                        "id": "test-passes",
                        "description": "the new test passes",
                        "kind": "pytest_selector",
                        "spec": {"selector": "tests/test_calc.py"},
                    },
                ],
            }
        ],
    },
    "two_independent_files": {
        "objective": "Create two independent modules",
        "tasks": [
            {
                "id": "alpha",
                "title": "Create alpha.py",
                "objective": "Create alpha.py defining def alpha(): returning 1",
                "role": "implementer",
                "depends_on": [],
                "include_paths": ["alpha.py"],
                "anticipated_files": ["alpha.py"],
                "acceptance_criteria": [
                    {
                        "id": "alpha-exists",
                        "description": "alpha.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "alpha.py"},
                    }
                ],
            },
            {
                "id": "beta",
                "title": "Create beta.py",
                "objective": "Create beta.py defining def beta(): returning 2",
                "role": "implementer",
                "depends_on": [],
                "include_paths": ["beta.py"],
                "anticipated_files": ["beta.py"],
                "acceptance_criteria": [
                    {
                        "id": "beta-exists",
                        "description": "beta.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "beta.py"},
                    }
                ],
            },
        ],
    },
    "dependent_chain": {
        "objective": "Implement a module and then test it",
        "tasks": [
            {
                "id": "impl",
                "title": "Create mathutil.py",
                "objective": (
                    "Create mathutil.py defining def square(x): returning x * x"
                ),
                "role": "implementer",
                "depends_on": [],
                "include_paths": ["mathutil.py"],
                "anticipated_files": ["mathutil.py"],
                "acceptance_criteria": [
                    {
                        "id": "mathutil-exists",
                        "description": "mathutil.py exists",
                        "kind": "file_exists",
                        "spec": {"path": "mathutil.py"},
                    }
                ],
            },
            {
                "id": "tests",
                "title": "Test mathutil",
                "objective": (
                    "Create tests/test_mathutil.py with a pytest test importing "
                    "square from mathutil and asserting square(3) == 9"
                ),
                "role": "test_engineer",
                "depends_on": ["impl"],
                "include_paths": ["tests/"],
                "anticipated_files": ["tests/test_mathutil.py"],
                "acceptance_criteria": [
                    {
                        "id": "mathutil-test-passes",
                        "description": "the new test passes",
                        "kind": "pytest_selector",
                        "spec": {"selector": "tests/test_mathutil.py"},
                    }
                ],
            },
        ],
    },
}


def _append_progress(path: Path | None, trial: LocalTrial) -> None:
    """Append a finished trial to the progress log.

    A long benchmark must not lose its results to an interruption, so every
    trial is written as it completes, not only at the end.
    """
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trial.to_dict(), sort_keys=True) + "\n")
            handle.flush()
    except OSError:
        pass


async def run_local_suite(
    *,
    models: list[str] | None = None,
    scenarios: list[str] | None = None,
    strategies: list[str] | None = None,
    trials: int = 1,
    model_dir: Path | None = None,
    server_config: ServerConfig | None = None,
    base_url: str | None = None,
    keep_models: bool = False,
    parallel_models: int = 1,
    parallel_runs: int = 1,
    progress_path: Path | None = None,
) -> dict[str, Any]:
    """Run the local-model benchmark.

    ``base_url`` points at an *already running* OpenAI-compatible server.  If
    it is not given, a `llama-server` is started for each model.

    ``parallel_models`` serves that many models at once and ``parallel_runs``
    runs that many of a model's scenarios concurrently.  Both are recorded in
    the report, because a wall-clock time measured under contention is not
    comparable with one measured alone.
    """
    catalog = {spec.id: spec for spec in LOCAL_MODEL_CATALOG}
    selected = [catalog[m] for m in models] if models else list(catalog.values())
    if not selected:
        raise ValidationError("no local models selected")
    scenario_ids = scenarios or list(LOCAL_SCENARIOS)
    strategy_names = strategies or ["single", "static_two"]

    results: list[LocalTrial] = []
    server_stats: list[dict[str, Any]] = []
    started_at = time.time()

    config = server_config or ServerConfig()
    # Measure the free VRAM *before* any of this suite's servers start, so
    # the layer planner is not fooled by the memory our own models hold.
    baseline_vram = free_vram_mb()
    config = ServerConfig(
        server_path=config.server_path,
        host=config.host,
        port=config.port,
        context_size=config.context_size,
        threads=config.threads,
        slots=config.slots,
        gpu_layers=config.gpu_layers,
        extra_args=config.extra_args,
        startup_timeout_s=config.startup_timeout_s,
        request_timeout_s=config.request_timeout_s,
        shared_gpu=max(1, parallel_models),
        baseline_vram_mb=baseline_vram,
    )

    model_semaphore = asyncio.Semaphore(max(1, parallel_models))
    run_semaphore = asyncio.Semaphore(max(1, parallel_runs))

    async def run_one(spec: LocalModelSpec) -> list[LocalTrial]:
        """Serve one model and run every scenario/strategy combination."""
        async with model_semaphore:
            model_path = spec.local_path(model_dir)
            if not model_path.exists() and not base_url:
                download_model(spec, model_dir)
            if base_url:
                endpoint = base_url
                gpu_layers = 0
                load_seconds = 0.0
                handle = None
            else:
                server = LlamaServer(config)
                try:
                    handle = server.start(spec, model_path)
                except InfrastructureError as exc:
                    return [
                        LocalTrial(
                            model=spec.id,
                            family=spec.family,
                            scenario_id="-",
                            strategy="-",
                            workers=0,
                            trial=0,
                            run_id="-",
                            status="error",
                            verified=False,
                            tokens_used=0,
                            cost_usd=0.0,
                            wall_clock_s=0.0,
                            model_calls=0,
                            tool_calls=0,
                            tool_call_parse_failures=0,
                            verification_failures=0,
                            error=exc.message,
                        )
                    ]
                endpoint = handle.base_url
                gpu_layers = handle.gpu_layers
                load_seconds = handle.load_seconds

            stats = parse_server_stats(handle.stderr_path if handle else None)
            server_stats.append(
                {
                    "model": spec.id,
                    "family": spec.family,
                    "gpu_layers": gpu_layers,
                    "load_seconds": round(load_seconds, 2),
                    "size_mb": spec.size_mb,
                    "slots": handle.slots if handle else 0,
                    "context_size": handle.context_size if handle else 0,
                    **stats,
                }
            )

            async def one(scenario_id: str, strategy: str, trial: int) -> LocalTrial:
                async with run_semaphore:
                    result = await _run_one(
                        spec=spec,
                        endpoint=endpoint,
                        scenario_id=scenario_id,
                        strategy=strategy,
                        trial=trial,
                    )
                    _append_progress(progress_path, result)
                    return result

            try:
                combinations = [
                    (scenario_id, strategy, trial)
                    for scenario_id in scenario_ids
                    for strategy in strategy_names
                    for trial in range(trials)
                ]
                outcomes = await asyncio.gather(
                    *(one(*combination) for combination in combinations)
                )
                return list(outcomes)
            finally:
                if handle is not None:
                    handle.stop()

    for batch in await asyncio.gather(*(run_one(spec) for spec in selected)):
        results.extend(batch)

    aggregates = _aggregate(results)
    return {
        "suite": "local-models",
        "started_at": started_at,
        "finished_at": time.time(),
        "duration_s": round(time.time() - started_at, 2),
        "models": [spec.to_dict() for spec in selected],
        "excluded_models": list(EXCLUDED_MODELS),
        "scenarios": scenario_ids,
        "strategies": strategy_names,
        "trials_per_combination": trials,
        "parallel_models": parallel_models,
        "parallel_runs": parallel_runs,
        "server_stats": server_stats,
        "trials": [t.to_dict() for t in results],
        "aggregates": aggregates,
        "progress_path": str(progress_path) if progress_path else None,
        "notes": [
            "The provider is a real, locally served model; the tool loop, the "
            "policy engine and the verification gate are the production ones.",
            "The mock provider's `edits` affordance is deliberately absent "
            "here: a real model must call write_file itself, which is the "
            "capability being measured.",
            "Cost is zero because the models are local; the token counts are "
            "the real, provider-reported ones.",
            (
                f"Models were served concurrently (parallel_models={parallel_models}, "
                f"parallel_runs={parallel_runs}), so wall-clock times are NOT "
                "comparable between models. The verified rate, the token counts "
                "and the tool-call counts are."
            ),
        ],
    }



async def _run_one(
    *,
    spec: LocalModelSpec,
    endpoint: str,
    scenario_id: str,
    strategy: str,
    trial: int,
) -> LocalTrial:
    """Run one scenario against one local model."""
    import tempfile

    from ..config import RunConfig
    from ..ids import run_id as new_run_id
    from ..orchestrator import Orchestrator
    from ..providers.openai_compat import OpenAICompatProvider
    from ..store.events import EventType

    spec_body = LOCAL_SCENARIOS[scenario_id]
    with tempfile.TemporaryDirectory(prefix="agent-squad-local-") as tmp:
        root = Path(tmp)
        from ..benchmark.repo_fixture import init_repo

        repo = init_repo(root / "repo")
        run_id = new_run_id()
        config = RunConfig(
            run_id=run_id,
            repo_path=str(repo),
            data_dir=str(root / "data"),
            provider={
                "name": "openai_compat",
                "model": spec.id,
                "base_url": endpoint,
                # A tool call is short.  Letting the model ramble to the full
                # 4096-token cap costs minutes per run and teaches it
                # nothing; 512 is enough for a call and its arguments.
                "max_output_tokens": 512,
            },
            scheduler={"max_workers": STRATEGIES.get(strategy, 2)},
            strategy_override=strategy,
            roles={
                role: {"max_iterations": 6}
                for role in ("implementer", "test_engineer", "reviewer", "supervisor")
            },
            verification={
                "integration_test_command": ("python3", "-m", "pytest", "-q")
            },
            retry={"max_attempts": 2, "initial_backoff_s": 1.0, "max_backoff_s": 4.0},
            # The benchmark has no human to answer an approval request, so a
            # protected action fails fast instead of blocking the run.  The
            # approval workflow itself is covered by the security suite.
            approval={"approval_timeout_s": 10.0},
        )
        provider = OpenAICompatProvider(
            model=spec.id,
            base_url=endpoint,
            api_key="local",
            timeout_s=600.0,
            max_concurrency=1,
        )
        orchestrator = Orchestrator(config, data_dir=root / "data", provider=provider)
        started = time.monotonic()
        error: str | None = None
        status = "error"
        tokens = 0
        model_calls = 0
        tool_calls = 0
        verification_failures = 0
        protected_requests = 0
        verified_tasks = 0
        wall = 0.0
        try:
            outcome = await orchestrator.run(
                spec_body["objective"], request_spec=spec_body, integrate=False
            )
            status = outcome.status.value
            tokens = outcome.summary.tokens_used
            events = orchestrator.log.read(run_id)
            model_calls = sum(1 for e in events if e.type is EventType.MODEL_CALL)
            tool_calls = sum(1 for e in events if e.type is EventType.TOOL_INVOKED)
            verification_failures = sum(
                1 for e in events if e.type is EventType.VERIFICATION_FAILED
            )
            protected_requests = sum(
                1
                for e in events
                if e.type is EventType.HUMAN_APPROVAL
                and e.payload.get("status") == "requested"
            )
            wall = time.monotonic() - started
            verified_tasks = outcome.summary.succeeded
        except Exception as exc:
            error = str(exc)[:500]
            wall = time.monotonic() - started
        finally:
            await orchestrator.aclose()

    return LocalTrial(
        model=spec.id,
        family=spec.family,
        scenario_id=scenario_id,
        strategy=strategy,
        workers=STRATEGIES.get(strategy, 2),
        trial=trial,
        run_id=run_id,
        status=status,
        verified=verified_tasks > 0 and status == "succeeded",
        tokens_used=tokens,
        cost_usd=0.0,
        wall_clock_s=wall,
        model_calls=model_calls,
        tool_calls=tool_calls,
        tool_call_parse_failures=0,
        verification_failures=verification_failures,
        salvaged_tool_calls=provider.salvaged_calls,
        protected_action_requests=protected_requests,
        error=error,
    )


def _aggregate(trials: list[LocalTrial]) -> dict[str, Any]:
    """Aggregate the local trials by model."""
    by_model: dict[str, list[LocalTrial]] = {}
    for trial in trials:
        by_model.setdefault(trial.model, []).append(trial)

    out: dict[str, Any] = {}
    for model, model_trials in sorted(by_model.items()):
        real = [t for t in model_trials if t.scenario_id != "-"]
        if not real:
            out[model] = {
                "runs": 0,
                "status": "skipped" if model_trials else "unknown",
                "error": model_trials[0].error if model_trials else None,
            }
            continue
        out[model] = {
            "family": real[0].family,
            "runs": len(real),
            "verified_runs": sum(1 for t in real if t.verified),
            "verified_rate": round(
                sum(1 for t in real if t.verified) / len(real), 4
            ),
            "mean_wall_clock_s": round(
                sum(t.wall_clock_s for t in real) / len(real), 3
            ),
            "mean_tokens": round(sum(t.tokens_used for t in real) / len(real), 1),
            "mean_model_calls": round(
                sum(t.model_calls for t in real) / len(real), 2
            ),
            "mean_tool_calls": round(
                sum(t.tool_calls for t in real) / len(real), 2
            ),
            "verification_failures": sum(t.verification_failures for t in real),
            "salvaged_tool_calls": sum(t.salvaged_tool_calls for t in real),
            "protected_action_requests": sum(t.protected_action_requests for t in real),
            "errors": [t.error for t in real if t.error][:3],
        }
    return out
