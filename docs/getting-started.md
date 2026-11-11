# Getting started

[Documentation home](README.md) · [Concepts](concepts.md) · [Configuration](configuration.md)

This guide runs a deterministic example in a disposable repository. It does not
need model weights or a paid endpoint. Run the commands from your Orkestr clone
on a development machine with Git and a supported Python interpreter.

## 1. Install

```bash
export ORKESTR_ROOT="$PWD"
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
agent-squad --help
```

The release matrix is Linux x86-64, Python 3.11–3.13. The lock also includes
artifacts for the generator's Python 3.14 development environment; that does not
qualify 3.14 as a supported release. If a wheel hash/platform is unsupported,
record that limitation rather than silently dropping `--require-hashes`.
For a non-release developer install, use `python -m pip install -e '.[dev]'`.

## 2. Create a disposable target

```bash
DEMO_DIR=$(mktemp -d)
export DEMO_DIR
python - <<'PYCODE'
import os
from pathlib import Path
from agent_squad.benchmark.repo_fixture import init_repo
print(init_repo(Path(os.environ["DEMO_DIR"]) / "repo"))
PYCODE
```

This uses the project's synthetic calculator fixture. Its Git identity and
baseline files are set up by the fixture helper. Your existing repository and
working tree are not the target of the example.

## 3. Inspect the plan

```bash
agent-squad plan "Add a rounding helper and its tests" \
  --repo "$DEMO_DIR/repo" \
  --spec "$ORKESTR_ROOT/examples/request-rounding.json" --json
```

`plan` inspects the repository and proposes tasks; it does not execute the worker
patches. With a real provider it can still make model requests. A planning
preview is not the same as the run's final contract verdict.

The supplied request contains two tasks: implement `round_half_up`, then add
its tests. Their `depends_on` relationship is why the test worker receives a
base that includes the implementation.

## 4. Run and read the result

```bash
agent-squad run "Add a rounding helper and its tests" \
  --repo "$DEMO_DIR/repo" \
  --spec "$ORKESTR_ROOT/examples/request-rounding.json" \
  --data-dir "$DEMO_DIR/run-data" --json > "$DEMO_DIR/outcome.json"

python - <<'PYCODE'
import json
import os
from pathlib import Path
outcome = json.loads((Path(os.environ["DEMO_DIR"]) / "outcome.json").read_text())
print("run:", outcome["run_id"])
print("execution:", outcome["status"])
print("contract:", (outcome.get("contract") or {}).get("outcome", "not judged"))
PYCODE
```

Inspect all three values. A successful execution status is not sufficient for
request acceptance. In a script that requires acceptance, reject the result
unless `(outcome.get("contract") or {}).get("outcome") == "accepted"`.
An absent verdict, such as a run made with `--no-integrate`, is not acceptance.

The normal run produces reviewable artifacts; it does not push a branch for you.
The rounding example is an integration exercise, not a complete numerical
specification or evidence of model coding capability.

## 5. Inspect the durable record

Substitute the printed run ID:

```bash
agent-squad status RUN_ID --data-dir "$DEMO_DIR/run-data" --json
agent-squad inspect RUN_ID --data-dir "$DEMO_DIR/run-data" \
  --events --artifacts --decisions --json
agent-squad replay RUN_ID --data-dir "$DEMO_DIR/run-data" --json
```

The root contains `runs/RUN_ID/`, including `state.db`, the effective
`run-config.json`, artifacts and the patch bundle. Recorded model exchanges are
used for replay. Recovery and replay have different purposes; read
[operations](operations.md) before resuming interrupted work.

## 6. Move to a real model deliberately

Start an endpoint separately, then set the matching provider/model/base URL in
your configuration or CLI flags. Keep the owner criteria in `--spec`; swapping
providers should not redefine the request. Verify tool-call and schema behavior
on one small task before running a larger graph. A model loaded locally still
consumes RAM, VRAM and time even when there is no hosted API bill.

See [configuration](configuration.md) for limits and isolation. Development
mode is not a strong boundary for an untrusted repository. The hardened profile
must establish its required containment or refuse execution.

## Troubleshooting this example

| Symptom | Check |
|---|---|
| Command not found | Activate the environment and verify the editable install |
| Fixture import fails | Install the project, then run with the environment's Python |
| `plan` succeeds but `run` is inconclusive | Read the contract verdict and individual criterion results |
| Git reports ownership, hooks or repository problems | Use a fresh disposable target and inspect the effective Git configuration |
| API import error | Optional FastAPI/uvicorn dependencies are separate; there is no `api` project extra |
| Test collection reports duplicate module names | Keep the configured importlib import mode; do not clear pytest `addopts` without restoring it |
| Hardened execution is refused | Read the capability/probe report; do not interpret refusal as successful isolation |

Do not delete another run's data or global model cache to troubleshoot an
example. Keep the evidence you need, then clean only the temporary resources
you created. [Operations](operations.md) explains retention and cleanup.
