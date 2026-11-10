"""The dashboard/API contract.

The web dashboard is written in TypeScript against the FastAPI service.
The risk is not that either side is wrong on its own, but that they drift
apart: a field is renamed on one side and the dashboard silently renders
`undefined`.

This test reads the dashboard's TypeScript interface declarations and
asserts that a live API response contains every field they declare.  If the
dashboard is not present (it is optional), the test is skipped rather than
failing -- the backend is still correct on its own.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from helpers import two_independent_spec

from agent_squad.config import RunConfig
from agent_squad.orchestrator import Orchestrator

DASHBOARD_TYPES = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "types.ts"

pytest.importorskip("fastapi.testclient")


def _interfaces() -> dict[str, list[str]]:
    """Extract the field names of every exported TypeScript interface."""
    if not DASHBOARD_TYPES.exists():
        return {}
    text = DASHBOARD_TYPES.read_text(encoding="utf-8")
    interfaces: dict[str, list[str]] = {}
    pattern = re.compile(
        r"export interface (\w+)\s*\{(.*?)\n\}", re.DOTALL
    )
    for match in pattern.finditer(text):
        name, body = match.group(1), match.group(2)
        fields = [
            line.split(":")[0].strip().rstrip("?")
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith("//")
            and ":" in line
            and not line.strip().startswith("*")
        ]
        interfaces[name] = [f for f in fields if f]
    return interfaces


INTERFACES = _interfaces()


def test_the_dashboard_types_are_available() -> None:
    if not DASHBOARD_TYPES.exists():
        pytest.skip("the dashboard is not present in this checkout")
    assert "GraphNode" in INTERFACES
    assert "RunEvent" in INTERFACES
    assert "Artifact" in INTERFACES
    assert len(INTERFACES) > 15


def _assert_fields(payload: Any, fields: list[str], where: str) -> None:
    assert isinstance(payload, dict), f"{where} is not an object"
    missing = [f for f in fields if f not in payload]
    assert not missing, f"{where} is missing {missing}; got {sorted(payload)}"


def test_the_api_satisfies_the_dashboard_contract(tmp_path: Path, repo: Path) -> None:
    if not DASHBOARD_TYPES.exists():
        pytest.skip("the dashboard is not present in this checkout")

    from fastapi.testclient import TestClient

    from agent_squad.api.server import create_app

    config = RunConfig(
        run_id="run_contract",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "mock-deterministic-v1"},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        import asyncio

        asyncio.run(
            orchestrator.run("contract", request_spec=two_independent_spec())
        )
    finally:
        orchestrator.close()

    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        runs = client.get("/api/runs").json()
        _assert_fields(runs, INTERFACES["RunsResponse"], "RunsResponse")
        for run in runs["runs"]:
            _assert_fields(run, INTERFACES["RunSummary"], "RunSummary")

        detail = client.get("/api/runs/run_contract").json()
        _assert_fields(detail, INTERFACES["RunDetailResponse"], "RunDetailResponse")
        _assert_fields(detail["run"], INTERFACES["RunRecord"], "RunRecord")
        for task in detail["tasks"]:
            _assert_fields(task, INTERFACES["TaskState"], "TaskState")
            if task.get("verification"):
                _assert_fields(
                    task["verification"],
                    INTERFACES["VerificationReport"],
                    "VerificationReport",
                )
                for check in task["verification"]["checks"]:
                    _assert_fields(check, INTERFACES["CheckRecord"], "CheckRecord")
        for approval in detail["pending_approvals"]:
            _assert_fields(approval, INTERFACES["Approval"], "Approval")
        for checkpoint in detail["checkpoints"]:
            _assert_fields(checkpoint, INTERFACES["Checkpoint"], "Checkpoint")

        graph = client.get("/api/runs/run_contract/graph").json()
        _assert_fields(graph, INTERFACES["GraphResponse"], "GraphResponse")
        for node in graph["nodes"]:
            _assert_fields(node, INTERFACES["GraphNode"], "GraphNode")
            for criterion in node["criteria"]:
                _assert_fields(
                    criterion,
                    INTERFACES["AcceptanceCriterionSummary"],
                    "AcceptanceCriterionSummary",
                )
        for edge in graph["edges"]:
            _assert_fields(edge, INTERFACES["GraphEdge"], "GraphEdge")

        events = client.get("/api/runs/run_contract/events").json()
        _assert_fields(events, INTERFACES["EventsResponse"], "EventsResponse")
        for event in events["events"]:
            _assert_fields(event, INTERFACES["RunEvent"], "RunEvent")

        artifacts = client.get("/api/runs/run_contract/artifacts").json()
        _assert_fields(artifacts, INTERFACES["ArtifactsResponse"], "ArtifactsResponse")
        for artifact in artifacts["artifacts"]:
            _assert_fields(artifact, INTERFACES["Artifact"], "Artifact")
            _assert_fields(
                artifact["provenance"],
                INTERFACES["ArtifactProvenance"],
                "ArtifactProvenance",
            )

        messages = client.get("/api/runs/run_contract/messages").json()
        _assert_fields(messages, INTERFACES["MessagesResponse"], "MessagesResponse")
        for message in messages["messages"]:
            _assert_fields(message, INTERFACES["MessageRecord"], "MessageRecord")

        approvals = client.get("/api/approvals").json()
        _assert_fields(approvals, INTERFACES["ApprovalsResponse"], "ApprovalsResponse")

        health = client.get("/healthz").json()
        _assert_fields(health, INTERFACES["HealthResponse"], "HealthResponse")


def test_the_dashboard_declares_no_field_the_api_omits() -> None:
    """The dashboard's enum unions must match the backend's enumerations."""
    if not DASHBOARD_TYPES.exists():
        pytest.skip("the dashboard is not present in this checkout")

    from agent_squad.models.artifact import ArtifactKind
    from agent_squad.models.evidence import CriterionState
    from agent_squad.models.run import RunStatus, StrategyName, VerificationLevel
    from agent_squad.models.task import Role, TaskStatus

    text = DASHBOARD_TYPES.read_text(encoding="utf-8")

    def declared(name: str) -> set[str]:
        match = re.search(
            rf"export type {name} =\s*(.*?);", text, re.DOTALL
        )
        assert match, f"{name} is not declared in the dashboard types"
        return set(re.findall(r"'([^']+)'", match.group(1)))

    assert declared("RunStatus") == {s.value for s in RunStatus}
    assert declared("TaskStatus") == {s.value for s in TaskStatus}
    assert declared("VerificationLevel") == {v.value for v in VerificationLevel}
    assert declared("CriterionState") == {c.value for c in CriterionState}
    assert declared("ArtifactKind") == {k.value for k in ArtifactKind}
    assert declared("TaskRole") == {r.value for r in Role}
    assert declared("StrategyName") == {s.value for s in StrategyName}
