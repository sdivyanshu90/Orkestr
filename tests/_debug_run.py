import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fixtures.repo import init_repo, sample_spec

from agent_squad.config import RunConfig
from agent_squad.orchestrator import Orchestrator


async def main() -> None:
    tmp = Path("/tmp/squad-debug")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    repo = init_repo(tmp / "repo")
    config = RunConfig(
        run_id="run_debug",
        repo_path=str(repo),
        data_dir=str(tmp / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
        verification={"integration_test_command": ("python3", "-m", "pytest", "-q")},
    )
    orch = Orchestrator(config, data_dir=tmp / "data")
    spec = sample_spec()
    outcome = await orch.run(spec["objective"], request_spec=spec)
    print(json.dumps(outcome.to_dict(), indent=2, default=str))
    print("=== EVENTS ===")
    for e in orch.log.read(config.run_id):
        print(e.seq, e.type.value, e.task_id or "", json.dumps(e.payload, default=str)[:300])
    await orch.aclose()


asyncio.run(main())
