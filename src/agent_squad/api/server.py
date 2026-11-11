"""FastAPI service exposing run state to the dashboard.

The API is a *read-mostly view* over the same SQLite state the CLI uses.
It does not keep its own copy of anything, so the dashboard always reflects
the backend's actual execution state rather than a simulation.

Writes are limited to the two operations that genuinely need a remote
interface: cancelling a run and answering an approval request.  Starting a
run is supported but returns immediately; the run executes in the background
and the client polls the run's status.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..config import RunConfig
from ..ids import run_id as new_run_id
from ..models.message import INFORMATIONAL_KINDS
from ..models.run import RunStatus
from ..orchestrator import Orchestrator
from ..store.db import Database
from ..store.events import EventLog, EventType
from ..store.state import StateStore

_INFORMATIONAL_KINDS = {k.value for k in INFORMATIONAL_KINDS}

try:  # pragma: no cover - optional dependency
    from fastapi import FastAPI, HTTPException, Query
    from pydantic import BaseModel

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    FASTAPI_AVAILABLE = False


if FASTAPI_AVAILABLE:

    class CreateRunRequest(BaseModel):
        objective: str
        description: str = ""
        repo_path: str
        spec: dict[str, Any] | None = None
        max_workers: int | None = None
        strategy: str | None = None
        provider: str = "mock"
        model: str = "mock-deterministic-v1"

    class ApprovalDecisionRequest(BaseModel):
        approved: bool
        decided_by: str = "api-user"
        reason: str = ""

    class CancelRunRequest(BaseModel):
        reason: str = "cancelled via the API"

else:  # pragma: no cover

    class CreateRunRequest:  # type: ignore[no-redef]
        pass

    class ApprovalDecisionRequest:  # type: ignore[no-redef]
        pass

    class CancelRunRequest:  # type: ignore[no-redef]
        pass


def create_app(data_dir: str | Path | None = None) -> Any:
    """Build the FastAPI application."""
    if not FASTAPI_AVAILABLE:  # pragma: no cover
        raise RuntimeError(
            "FastAPI is not installed. Install the 'api' extra: pip install 'agent-squad[api]'"
        )

    base = Path(data_dir or ".agent-squad").resolve()
    app = FastAPI(
        title="agent-squad",
        version="0.1.0",
        description=(
            "Adaptive, verifiable multi-agent software engineering runtime. "
            "This API exposes the durable execution state of real runs."
        ),
    )
    app.state.data_dir = base
    app.state.orchestrators = {}
    app.state.tasks = {}

    def _open(run_id: str) -> tuple[Database, StateStore, EventLog]:
        root = base / "runs" / run_id
        if not (root / "state.db").exists():
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        db = Database(root / "state.db")
        log = EventLog(db)
        return db, StateStore(db, log), log

    # ------------------------------------------------------------------ runs

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/api/runs")
    async def list_runs(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        """List the stored runs.

        Each run keeps its own database, so the listing scans the run
        directory rather than a central index.  This keeps runs
        self-contained and removable one at a time.
        """
        runs: list[dict[str, Any]] = []
        root = base / "runs"
        if root.exists():
            for entry in sorted(root.iterdir()):
                db_path = entry / "state.db"
                if not db_path.exists():
                    continue
                db = Database(db_path)
                try:
                    record = db.query_one(
                        "SELECT run_id, status, strategy, created_at, updated_at "
                        "FROM runs ORDER BY created_at DESC LIMIT 1"
                    )
                    if record:
                        runs.append(dict(record))
                finally:
                    db.close()
        runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return {"runs": runs[:limit]}

    @app.post("/api/runs")
    async def create_run(request: CreateRunRequest) -> dict[str, Any]:
        run_id = new_run_id()
        repo = Path(request.repo_path).resolve()
        if not repo.exists():
            raise HTTPException(status_code=400, detail=f"repository not found: {repo}")
        config = RunConfig(
            run_id=run_id,
            repo_path=str(repo),
            data_dir=str(base),
            provider={"name": request.provider, "model": request.model},
            scheduler={"max_workers": request.max_workers or 4},
            strategy_override=request.strategy,
        )
        orchestrator = Orchestrator(config, data_dir=base)
        app.state.orchestrators[run_id] = orchestrator

        async def _execute() -> None:
            try:
                await orchestrator.run(
                    request.objective,
                    description=request.description,
                    request_spec=request.spec,
                )
            except Exception:
                pass
            finally:
                await orchestrator.aclose()

        task = asyncio.create_task(_execute())
        app.state.tasks[run_id] = task
        return {"run_id": run_id, "status": RunStatus.CREATED.value}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        db, state, _ = _open(run_id)
        try:
            record = state.get_run(run_id)
            if record is None:
                raise HTTPException(status_code=404, detail="unknown run")
            return {
                "run": record,
                "tasks": [t.model_dump(mode="json") for t in state.list_task_states(run_id)],
                "pending_approvals": state.pending_approvals(run_id),
                "checkpoints": state.list_checkpoints(run_id),
            }
        finally:
            db.close()

    @app.get("/api/runs/{run_id}/events")
    async def get_events(
        run_id: str, after_seq: int = 0, limit: int = Query(default=500, le=5000)
    ) -> dict[str, Any]:
        db, _, log = _open(run_id)
        try:
            events = log.read(run_id, after_seq=after_seq, limit=limit)
            return {"events": [e.to_dict() for e in events], "last_seq": log.last_seq(run_id)}
        finally:
            db.close()

    @app.get("/api/runs/{run_id}/graph")
    async def get_graph(run_id: str) -> dict[str, Any]:
        """The task DAG in a shape the dashboard can render directly."""
        db, state, _ = _open(run_id)
        try:
            specs = state.list_task_specs(run_id)
            states = {t.id: t for t in state.list_task_states(run_id)}
            nodes = []
            for spec in specs:
                task_state = states.get(spec.id)
                nodes.append(
                    {
                        "id": spec.id,
                        "title": spec.title,
                        "role": spec.role.value,
                        "status": task_state.status.value if task_state else "pending",
                        "depends_on": list(spec.depends_on),
                        "attempts": task_state.attempt if task_state else 0,
                        "tokens_used": task_state.tokens_used if task_state else 0,
                        "cost_usd": task_state.cost_used_usd if task_state else 0.0,
                        "verification": task_state.verification if task_state else None,
                        "criteria": [
                            {
                                "id": c.id,
                                "description": c.description,
                                "kind": c.kind.value,
                                "required": c.required,
                            }
                            for c in spec.acceptance_criteria
                        ],
                    }
                )
            edges = [
                {"source": dep, "target": spec.id}
                for spec in specs
                for dep in spec.depends_on
            ]
            return {"nodes": nodes, "edges": edges}
        finally:
            db.close()

    @app.get("/api/runs/{run_id}/contract")
    async def get_contract(run_id: str) -> dict[str, Any]:
        """The request contract and the acceptance verdict for a run."""
        db, _state, log = _open(run_id)
        try:
            created = [
                e
                for e in log.read(run_id, types=[EventType.RUN_CREATED])
                if "contract" in e.payload
            ]
            contract = created[0].payload["contract"] if created else None
            verdicts = [
                e.payload
                for e in log.read(run_id, types=[EventType.VERIFICATION_CHECK])
                if e.payload.get("kind") == "contract"
            ]
            return {
                "contract": contract,
                "verdict": verdicts[-1] if verdicts else None,
                "outcomes": {
                    "accepted": "the request is satisfied by the final candidate tree",
                    "rejected": "a required outcome is not satisfied",
                    "inconclusive": "the evidence is insufficient",
                    "timed_out": "the run exceeded its deadline",
                    "cancelled": "the run was cancelled",
                },
            }
        finally:
            db.close()

    @app.get("/api/runs/{run_id}/artifacts")
    async def get_artifacts(run_id: str) -> dict[str, Any]:
        db, state, _ = _open(run_id)
        try:
            return {
                "artifacts": [
                    a.model_dump(mode="json") for a in state.list_artifacts(run_id)
                ]
            }
        finally:
            db.close()

    @app.get("/api/runs/{run_id}/messages")
    async def get_messages(run_id: str) -> dict[str, Any]:
        db, state, _ = _open(run_id)
        try:
            return {
                "messages": state.list_messages(run_id),
                "state_effecting": [
                    m for m in state.list_messages(run_id)
                    if m["kind"] not in _INFORMATIONAL_KINDS
                ],
            }
        finally:
            db.close()

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: CancelRunRequest) -> dict[str, Any]:
        db, state, log = _open(run_id)
        try:
            from ..store.events import EventType

            log.append(
                EventType.CANCELLATION_REQUESTED,
                run_id=run_id,
                payload={"reason": request.reason, "scope": "run"},
            )
            state.set_run_status(run_id, RunStatus.CANCELLED, payload={"reason": request.reason})
            task = app.state.tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
            return {"run_id": run_id, "status": RunStatus.CANCELLED.value}
        finally:
            db.close()

    @app.post("/api/approvals/{approval_id}")
    async def decide_approval(
        approval_id: str, request: ApprovalDecisionRequest
    ) -> dict[str, Any]:
        root = base / "runs"
        for entry in sorted(root.iterdir()) if root.exists() else []:
            db_path = entry / "state.db"
            if not db_path.exists():
                continue
            db = Database(db_path)
            state = StateStore(db, EventLog(db))
            record = state.get_approval(approval_id)
            if record is None:
                db.close()
                continue
            decision = state.decide_approval(
                approval_id,
                approved=request.approved,
                decided_by=request.decided_by,
                reason=request.reason,
            )
            db.close()
            return decision
        raise HTTPException(status_code=404, detail=f"unknown approval {approval_id}")

    @app.get("/api/approvals")
    async def list_approvals(run_id: str = Query(default="")) -> dict[str, Any]:
        if run_id:
            db, state, _ = _open(run_id)
            try:
                return {"approvals": state.pending_approvals(run_id)}
            finally:
                db.close()
        approvals: list[dict[str, Any]] = []
        root = base / "runs"
        for entry in sorted(root.iterdir()) if root.exists() else []:
            db_path = entry / "state.db"
            if not db_path.exists():
                continue
            db = Database(db_path)
            state = StateStore(db, EventLog(db))
            approvals.extend(state.pending_approvals(entry.name))
            db.close()
        return {"approvals": approvals}

    return app


def main() -> None:  # pragma: no cover - manual entry point
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the agent-squad API service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", default=".agent-squad")
    args = parser.parse_args()
    uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
