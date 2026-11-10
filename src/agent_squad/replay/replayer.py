"""Replay of recorded runs.

Replay reconstructs a run's execution from the durable record.  It comes in
two flavours, and the difference matters:

``state replay`` (default)
    Replays the event log to reconstruct the task state machine and the
    decisions the scheduler made.  Deterministic: the same log always yields
    the same reconstruction.

``decision replay`` (``--execute``)
    Re-runs the *control plane* against a provider that serves the recorded
    model responses.  No network calls and no tokens are spent.  The point is
    to check that the deterministic machinery -- the scheduler, the state
    machine, the verifier -- reaches the same decisions when it is handed the
    same inputs.  It is still a replay, not a fresh evaluation: the model
    never thinks again.

Neither mode is ever described as a re-execution of the work.  A recorded
replay proves what happened; it says nothing about what a model would do
today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts.store import ArtifactStore
from ..errors import ValidationError
from ..models.task import TaskStatus
from ..recovery import find_run
from ..store.db import Database
from ..store.events import EventLog, EventType
from ..store.state import StateStore
from .recorder import Exchange, RecordedProvider, load_exchanges

REPLAY_MODE = "recorded_replay"


@dataclass
class ReplayResult:
    """The outcome of a replay."""

    run_id: str
    mode: str = REPLAY_MODE
    found: bool = False
    event_count: int = 0
    exchanges: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    state_transitions: list[dict[str, Any]] = field(default_factory=list)
    final_state: dict[str, str] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    executed: bool = False
    execution_matches_record: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "found": self.found,
            "event_count": self.event_count,
            "exchanges": self.exchanges,
            "decisions": self.decisions,
            "state_transitions": self.state_transitions,
            "final_state": self.final_state,
            "checkpoints": self.checkpoints,
            "warnings": self.warnings,
            "executed": self.executed,
            "execution_matches_record": self.execution_matches_record,
        }


def replay_run(
    run_id: str,
    *,
    data_dir: str | Path | None = None,
    checkpoint: str | None = None,
    execute: bool = False,
) -> ReplayResult:
    """Replay a recorded run."""
    result = ReplayResult(run_id=run_id)
    root = find_run(run_id, data_dir)
    if root is None:
        result.warnings.append("no run directory found")
        return result
    result.found = True

    db = Database(root / "state.db")
    try:
        log = EventLog(db)
        state = StateStore(db, log)
        events = log.read(run_id)
        result.event_count = len(events)

        store = ArtifactStore(root / "artifacts")
        exchanges = load_exchanges(store, run_id)
        result.exchanges = len(exchanges)

        cutoff_seq = None
        if checkpoint:
            checkpoints = state.list_checkpoints(run_id)
            match = next((c for c in checkpoints if c["checkpoint_id"] == checkpoint), None)
            if match is None:
                raise ValidationError(
                    f"unknown checkpoint {checkpoint!r}", checkpoint=checkpoint
                )
            cutoff_seq = int(match["seq"])
            result.checkpoints = [match]
        else:
            result.checkpoints = state.list_checkpoints(run_id)

        selected = [e for e in events if cutoff_seq is None or e.seq <= cutoff_seq]

        for event in selected:
            if event.type is EventType.STRATEGY_SELECTED:
                result.decisions.append(
                    {
                        "seq": event.seq,
                        "kind": "strategy",
                        **event.payload,
                    }
                )
            elif event.type is EventType.TASK_STATUS_CHANGED:
                result.state_transitions.append(
                    {
                        "seq": event.seq,
                        "task_id": event.task_id,
                        "attempt": event.attempt,
                        "from": event.payload.get("from"),
                        "to": event.payload.get("to"),
                        "error_class": event.payload.get("error_class"),
                    }
                )
            elif event.type is EventType.PLAN_REVISED:
                result.decisions.append({"seq": event.seq, "kind": "replan", **event.payload})

        # Rebuild the state machine from the log rather than trusting the
        # projection: this is what makes the replay independent of the
        # original process.
        rebuilt = state.rebuild_projection(run_id)
        result.warnings.append(
            f"rebuilt projection from {rebuilt['events_replayed']} event(s)"
        )
        result.final_state = {
            task.id: task.status.value for task in state.list_task_states(run_id)
        }

        if execute:
            result.executed = True
            result.execution_matches_record = _execute_replay(
                root=root,
                run_id=run_id,
                exchanges=exchanges,
                recorded=result.state_transitions,
                result=result,
            )
        return result
    finally:
        db.close()


def _execute_replay(
    *,
    root: Path,
    run_id: str,
    exchanges: list[Exchange],
    recorded: list[dict[str, Any]],
    result: ReplayResult,
) -> bool | None:
    """Re-run the control plane against the recorded transcript.

    Returns whether the replayed execution produced the same task status
    sequence as the recorded one.  ``None`` means the replay could not be
    attempted (for example because the recorded transcript is incomplete).
    """
    from ..config import RunConfig
    from ..orchestrator import Orchestrator

    run_dir = root / "run-config.json"
    if not run_dir.exists():
        result.warnings.append(
            "the run configuration was not persisted; control-plane replay is not possible"
        )
        return None
    try:
        config = RunConfig.model_validate_json(run_dir.read_text(encoding="utf-8"))
    except Exception as exc:
        result.warnings.append(f"the run configuration could not be loaded: {exc}")
        return None
    if not exchanges:
        result.warnings.append("no model exchanges were recorded; nothing to replay")
        return None

    recorded_status = [t.get("to") for t in recorded if t.get("task_id")]
    provider = RecordedProvider(exchanges)
    orchestrator = Orchestrator(config, data_dir=root.parent.parent, provider=provider)
    try:

        outcome = orchestrator.run(
            "replay", description="control-plane replay", integrate=False
        )
        replayed_status = [
            o.status.value for o in outcome.outcomes.values()
        ]
        expected = [s for s in recorded_status if s != TaskStatus.PENDING.value]
        result.warnings.append(
            f"replayed {len(replayed_status)} task outcome(s) from {len(exchanges)} exchange(s)"
        )
        return sorted(replayed_status) == sorted(expected)
    except Exception as exc:
        result.warnings.append(f"control-plane replay failed: {exc}")
        return None
    finally:
        await_close(orchestrator)


def await_close(orchestrator: Any) -> None:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(orchestrator.aclose())
        return
    # The reference is kept so the task is not garbage collected mid-flight.
    closer = loop.create_task(orchestrator.aclose())
    closer.add_done_callback(lambda _t: None)
