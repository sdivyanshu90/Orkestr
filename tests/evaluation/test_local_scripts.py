"""Check benchmark scoring boundaries and cleanup isolation without model weights."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_fixed_score_rejects_wrong_answers_and_unsafe_code():
    bench = module("benchmark_local_inference")
    assert bench.score("arithmetic", {"content": '{"answer":391}'}, {"answer": 391})
    assert not bench.score("arithmetic", {"content": '{"answer":390}'}, {"answer": 391})
    for code in ("def square(x):\n return x*x", "def square(x):\n return x * x\n"):
        assert bench.score("python_square", {"content": json.dumps({"code": code})}, None)
    for code in (
        "def square(x):\n return x",
        "import os\ndef square(x):\n return x*x",
        'def square(x):\n return "x"*99999999999999',
    ):
        assert not bench.score("python_square", {"content": json.dumps({"code": code})}, None)


def test_cleanup_dry_run_and_scope(tmp_path, monkeypatch):
    cleanup = module("cleanup_local_models")
    roots = [tmp_path / name for name in ("models", "localmodels", "llamacpp")]
    unrelated = tmp_path / "unrelated.gguf"
    unrelated.write_text("keep")
    for root in roots:
        root.mkdir()
        (root / "weight.gguf").write_text("weight")
    monkeypatch.setattr(cleanup, "default_roots", lambda: roots)
    monkeypatch.setattr(cleanup, "stop_servers", lambda root: [])
    assert cleanup.main([]) == 0
    assert all(root.exists() for root in roots)
    receipt = tmp_path / "receipt.json"
    assert cleanup.main(["--yes", "--report", str(receipt)]) == 0
    assert not any(root.exists() for root in roots)
    assert unrelated.read_text() == "keep"
    assert json.loads(receipt.read_text())["complete"]


def test_cleanup_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    cleanup = module("cleanup_local_models")
    roots = [tmp_path / name for name in ("models", "localmodels", "llamacpp")]
    roots[0].mkdir()
    monkeypatch.setattr(cleanup, "default_roots", lambda: roots)
    monkeypatch.setattr(cleanup, "stop_servers", lambda root: [])

    def fail(path):
        raise PermissionError("test refusal")

    monkeypatch.setattr(cleanup.shutil, "rmtree", fail)
    receipt = tmp_path / "receipt.json"
    assert cleanup.main(["--yes", "--report", str(receipt)]) == 1
    assert not json.loads(receipt.read_text())["complete"]
