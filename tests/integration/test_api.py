"""API tests.

The API is a view over the durable run state, so these tests create a real
run and then read it back through the HTTP surface.  Nothing is simulated:
the endpoints return what the runtime actually recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.api.server import create_app
from agent_squad.config import RunConfig
from agent_squad.orchestrator import Orchestrator

from helpers import two_independent_spec

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture()
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    app = create_app(tmp_path / "data")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def completed_run(tmp_path: Path, repo: Path) -> str:
    config = RunConfig(
        run_id="run_api",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        import asyncio

        asyncio.run(
            orchestrator.run("api work", request_spec=two_independent_spec())
        )
    finally:
        orchestrator.close()
    return "run_api"


def test_healthz(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_listing_runs_returns_the_stored_run(client, completed_run: str) -> None:
    response = client.get("/api/runs")
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert any(r["run_id"] == completed_run for r in runs)


def test_run_detail_reports_the_task_states(client, completed_run: str) -> None:
    response = client.get(f"/api/runs/{completed_run}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["status"] == "succeeded"
    statuses = {t["id"]: t["status"] for t in payload["tasks"]}
    assert statuses == {"one": "succeeded", "two": "succeeded"}


def test_the_graph_endpoint_returns_nodes_and_edges(client, completed_run: str) -> None:
    response = client.get(f"/api/runs/{completed_run}/graph")
    assert response.status_code == 200
    payload = response.json()
    assert {n["id"] for n in payload["nodes"]} == {"one", "two"}
    assert payload["edges"] == []
    for node in payload["nodes"]:
        assert node["status"] == "succeeded"
        assert node["criteria"]


def test_events_are_returned_in_sequence_order(client, completed_run: str) -> None:
    response = client.get(f"/api/runs/{completed_run}/events")
    assert response.status_code == 200
    payload = response.json()
    events = payload["events"]
    assert events
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    kinds = {e["type"] for e in events}
    assert "run.plan_created" in kinds
    assert "verification.passed" in kinds


def test_events_can_be_paged(client, completed_run: str) -> None:
    first = client.get(f"/api/runs/{completed_run}/events?limit=5").json()
    assert len(first["events"]) == 5
    after = first["events"][-1]["seq"]
    second = client.get(
        f"/api/runs/{completed_run}/events?after_seq={after}&limit=5"
    ).json()
    assert all(e["seq"] > after for e in second["events"])


def test_artifacts_are_exposed_with_their_hashes(client, completed_run: str) -> None:
    response = client.get(f"/api/runs/{completed_run}/artifacts")
    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    patches = [a for a in artifacts if a["kind"] == "patch"]
    assert patches
    for patch in patches:
        assert len(patch["sha256"]) == 64
        assert patch["provenance"]["task_id"]


def test_an_unknown_run_is_a_404(client) -> None:
    response = client.get("/api/runs/run_does_not_exist")
    assert response.status_code == 404


def test_cancelling_a_run_records_the_cancellation(client, completed_run: str) -> None:
    response = client.post(
        f"/api/runs/{completed_run}/cancel", json={"reason": "test"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_the_api_reflects_real_state_not_simulated_state(
    client, completed_run: str
) -> None:
    """The task statuses must match what the runtime recorded."""
    detail = client.get(f"/api/runs/{completed_run}").json()
    graph = client.get(f"/api/runs/{completed_run}/graph").json()
    by_id = {t["id"]: t for t in detail["tasks"]}
    for node in graph["nodes"]:
        assert node["status"] == by_id[node["id"]]["status"]
        assert node["attempts"] == by_id[node["id"]]["attempt"]
