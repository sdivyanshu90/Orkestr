"""``agent-squad`` command line interface.

The CLI is the primary interface for driving runs, inspecting them and
answering approval requests.  Every command supports ``--json`` so that the
tool can be scripted; the human-readable output uses ``rich`` tables.

    agent-squad run "Add a rounding helper" --repo . --spec request.json
    agent-squad plan "Add a rounding helper" --repo . --spec request.json
    agent-squad status <run-id>
    agent-squad inspect <run-id> --tasks
    agent-squad approve <approval-id> --decision yes
    agent-squad cancel <run-id>
    agent-squad resume <run-id>
    agent-squad replay <run-id> --checkpoint <id>
    agent-squad benchmark --suite demo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from ..config import build_run_config, load_config
from ..errors import AgentSquadError, ValidationError
from ..ids import run_id as new_run_id
from ..models.run import RunStatus
from ..orchestrator import Orchestrator, RunOutcome
from ..store.events import EventType

console = Console()


def _print_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, indent=2, default=str))


def _load_run_config(args: argparse.Namespace) -> Any:
    data: dict[str, Any] = {}
    if getattr(args, "config", None):
        data = load_config(args.config)
    if getattr(args, "provider", None):
        data.setdefault("provider", {})
        data["provider"]["name"] = args.provider
    if getattr(args, "model", None):
        data.setdefault("provider", {})
        data["provider"]["model"] = args.model
    if getattr(args, "base_url", None):
        data.setdefault("provider", {})
        data["provider"]["base_url"] = args.base_url
    if getattr(args, "max_workers", None):
        data.setdefault("scheduler", {})
        data["scheduler"]["max_workers"] = args.max_workers
    if getattr(args, "strategy", None):
        data["strategy_override"] = args.strategy
    if getattr(args, "data_dir", None):
        data["data_dir"] = args.data_dir
    return build_run_config(
        data, repo_path=str(Path(args.repo).resolve()), run_id=new_run_id()
    )


def _load_spec(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        raise ValidationError(f"spec file not found: {file_path}")
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"spec file is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    config = _load_run_config(args)
    orchestrator = Orchestrator(config, data_dir=args.data_dir)

    async def _go() -> RunOutcome:
        try:
            return await orchestrator.run(
                args.objective,
                description=args.description or "",
                request_spec=spec,
                integrate=not args.no_integrate,
            )
        finally:
            await orchestrator.aclose()

    try:
        outcome = asyncio.run(_go())
    except AgentSquadError as exc:
        if args.json:
            _print_json({"error": exc.to_dict()})
        else:
            console.print(f"[red]error:[/red] {exc.message}")
        return 2

    if args.json:
        _print_json(outcome.to_dict())
        return 0 if outcome.status in (RunStatus.SUCCEEDED,) else 1

    _render_outcome(outcome)
    return 0 if outcome.status is RunStatus.SUCCEEDED else 1


def _render_outcome(outcome: RunOutcome) -> None:
    console.print(f"[bold]run[/bold] {outcome.run_id}  status={outcome.status.value}")
    if outcome.decision:
        console.print(f"[dim]{outcome.decision.explain()}[/dim]")
        for line in outcome.decision.rationale:
            console.print(f"  • {line}")
    table = Table(title="tasks")
    table.add_column("task")
    table.add_column("status")
    table.add_column("attempts")
    table.add_column("verified")
    table.add_column("error")
    for task_id, task_outcome in sorted(outcome.outcomes.items()):
        verified = "-"
        if task_outcome.verification:
            verified = task_outcome.verification.level
        table.add_row(
            task_id,
            task_outcome.status.value,
            str(task_outcome.attempts),
            verified,
            (task_outcome.error or "")[:60],
        )
    console.print(table)
    if outcome.integration is not None:
        integration = outcome.integration
        console.print(
            f"[bold]integration[/bold] passed={integration.passed} "
            f"applied={len(integration.applied)} conflicts={integration.conflict_count}"
        )
        for conflict in integration.conflicts:
            console.print(f"  [yellow]conflict[/yellow] {conflict.kind}: {conflict.detail}")
    if outcome.warnings:
        for warning in outcome.warnings:
            console.print(f"[yellow]warning:[/yellow] {warning}")


# -------------------------------------------------------------------- plan


def cmd_plan(args: argparse.Namespace) -> int:
    import asyncio as _asyncio

    from ..planning.inspector import RepositoryInspector
    from ..planning.planner import PlanRequest, build_planner
    from ..providers.registry import build_provider

    spec = _load_spec(args.spec)
    config = _load_run_config(args)
    provider = build_provider(config.provider)
    planner = build_planner(config, provider)
    inspector = RepositoryInspector(config.repo_path)
    context = inspector.inspect()
    request = PlanRequest(
        run_id=config.run_id,
        objective=args.objective,
        description=args.description or "",
        repo_path=config.repo_path,
        context=context,
        request_spec=spec,
    )
    plan = _asyncio.run(planner.plan(request))

    if args.json:
        _print_json(
            {
                "planner": plan.planner,
                "rationale": plan.rationale,
                "warnings": plan.warnings,
                "tasks": [t.model_dump(mode="json") for t in plan.tasks],
            }
        )
        return 0

    console.print(f"[bold]plan[/bold] via {plan.planner}")
    for line in plan.rationale:
        console.print(f"  • {line}")
    table = Table(title="tasks")
    table.add_column("id")
    table.add_column("role")
    table.add_column("depends on")
    table.add_column("criteria")
    table.add_column("objective")
    for task in plan.tasks:
        table.add_row(
            task.id,
            task.role.value,
            ", ".join(task.depends_on) or "-",
            str(len(task.acceptance_criteria)),
            task.objective[:60],
        )
    console.print(table)
    for warning in plan.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")
    return 0


# ------------------------------------------------------------------ status


def cmd_status(args: argparse.Namespace) -> int:
    from ..store.db import Database
    from ..store.events import EventLog
    from ..store.state import StateStore

    run = _find_run(args.run_id, args.data_dir)
    if run is None:
        console.print(f"[red]run not found:[/red] {args.run_id}")
        return 2
    db = Database(run["db_path"])
    state = StateStore(db, EventLog(db))
    record = state.get_run(args.run_id)
    tasks = state.list_task_states(args.run_id)
    if args.json:
        _print_json(
            {
                "run": record,
                "tasks": [t.model_dump(mode="json") for t in tasks],
            }
        )
        return 0
    console.print(f"[bold]{args.run_id}[/bold]  {record['status'] if record else '?'}")
    table = Table()
    table.add_column("task")
    table.add_column("status")
    table.add_column("attempt")
    table.add_column("tokens")
    table.add_column("cost")
    for task in tasks:
        table.add_row(
            task.id,
            task.status.value,
            str(task.attempt),
            str(task.tokens_used),
            f"${task.cost_used_usd:.4f}",
        )
    console.print(table)
    return 0


def _find_run(run_id: str, data_dir: str | None) -> dict[str, Any] | None:
    base = Path(data_dir or ".agent-squad").resolve() / "runs"
    if not base.exists():
        return None
    candidate = base / run_id
    if candidate.exists():
        return {"db_path": str(candidate / "state.db"), "root": str(candidate)}
    for entry in sorted(base.iterdir()):
        if entry.name.startswith(run_id):
            return {"db_path": str(entry / "state.db"), "root": str(entry)}
    return None


# ----------------------------------------------------------------- inspect


def cmd_inspect(args: argparse.Namespace) -> int:
    from ..store.db import Database
    from ..store.events import EventLog
    from ..store.state import StateStore

    run = _find_run(args.run_id, args.data_dir)
    if run is None:
        console.print(f"[red]run not found:[/red] {args.run_id}")
        return 2
    db = Database(run["db_path"])
    state = StateStore(db, EventLog(db))
    events = EventLog(db)

    payload: dict[str, Any] = {"run": state.get_run(args.run_id)}
    if args.tasks:
        payload["tasks"] = [t.model_dump(mode="json") for t in state.list_task_states(args.run_id)]
    if args.artifacts:
        payload["artifacts"] = [
            a.model_dump(mode="json") for a in state.list_artifacts(args.run_id)
        ]
    if args.messages:
        payload["messages"] = state.list_messages(args.run_id)
    if args.events:
        payload["events"] = [e.to_dict() for e in events.read(args.run_id)]
    if args.checkpoints:
        payload["checkpoints"] = state.list_checkpoints(args.run_id)
    if args.decisions:
        payload["decisions"] = [
            e.payload
            for e in events.read(args.run_id, types=[EventType.STRATEGY_SELECTED])
        ]
    if args.export:
        # The exported log is newline-delimited JSON: portable, greppable
        # and readable without this tool.
        payload["export"] = events.export(args.run_id)

    if args.json:
        _print_json(payload)
        return 0

    console.print(f"[bold]{args.run_id}[/bold]")
    console.print(json.dumps(payload, indent=2, default=str)[:20_000])
    return 0


# ----------------------------------------------------------------- approve


def cmd_approve(args: argparse.Namespace) -> int:
    from ..store.db import Database
    from ..store.events import EventLog
    from ..store.state import StateStore

    run = _find_run(args.run_id, args.data_dir) if args.run_id else _find_run_by_approval(
        args.approval_id, args.data_dir
    )
    if run is None:
        console.print(f"[red]approval not found:[/red] {args.approval_id}")
        return 2
    db = Database(run["db_path"])
    state = StateStore(db, EventLog(db))
    approved = args.decision.lower() in ("yes", "approve", "y", "true", "allow")
    decision = state.decide_approval(
        args.approval_id,
        approved=approved,
        decided_by=args.who or "cli-user",
        reason=args.reason or "",
    )
    if args.json:
        _print_json(decision)
    else:
        console.print(
            f"approval {decision['approval_id']} -> {decision['status']} "
            f"({decision['decided_by']})"
        )
    return 0


def _find_run_by_approval(approval_id: str, data_dir: str | None) -> dict[str, Any] | None:
    """Locate the run that owns an approval id."""
    base = Path(data_dir or ".agent-squad").resolve() / "runs"
    if not base.exists():
        return None
    from ..store.db import Database
    from ..store.events import EventLog
    from ..store.state import StateStore

    for entry in sorted(base.iterdir()):
        db_path = entry / "state.db"
        if not db_path.exists():
            continue
        db = Database(db_path)
        state = StateStore(db, EventLog(db))
        record = state.get_approval(approval_id)
        db.close()
        if record:
            return {"db_path": str(db_path), "root": str(entry)}
    return None


# ------------------------------------------------------------------ cancel


def cmd_cancel(args: argparse.Namespace) -> int:
    from ..store.db import Database
    from ..store.events import EventLog
    from ..store.state import StateStore

    run = _find_run(args.run_id, args.data_dir)
    if run is None:
        console.print(f"[red]run not found:[/red] {args.run_id}")
        return 2
    db = Database(run["db_path"])
    state = StateStore(db, EventLog(db))
    log = EventLog(db)
    log.append(
        EventType.CANCELLATION_REQUESTED,
        run_id=args.run_id,
        payload={"reason": args.reason or "cancelled from the CLI", "scope": "run"},
    )
    state.set_run_status(args.run_id, RunStatus.CANCELLED, payload={"reason": args.reason})
    if args.json:
        _print_json({"run_id": args.run_id, "status": "cancelled"})
    else:
        console.print(f"run {args.run_id} marked cancelled")
    return 0


# ------------------------------------------------------------------ resume


def cmd_resume(args: argparse.Namespace) -> int:
    from ..recovery import resume_run

    result = asyncio.run(resume_run(args.run_id, data_dir=args.data_dir))
    if args.json:
        _print_json(result)
        return 0
    console.print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("resumed") else 1


# ------------------------------------------------------------------ replay


def cmd_replay(args: argparse.Namespace) -> int:
    from ..replay.replayer import replay_run

    result = replay_run(args.run_id, data_dir=args.data_dir, checkpoint=args.checkpoint)
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    if args.json:
        _print_json(payload)
        return 0
    console.print(json.dumps(payload, indent=2, default=str)[:20_000])
    return 0


# --------------------------------------------------------------- benchmark


def cmd_benchmark(args: argparse.Namespace) -> int:
    from ..benchmark.harness import run_suite

    result = asyncio.run(
        run_suite(
            suite=args.suite,
            trials=args.trials,
            data_dir=args.data_dir,
            strategies=args.strategies.split(",") if args.strategies else None,
        )
    )
    if args.json:
        _print_json(result)
        return 0
    from ..benchmark.report import render_report

    render_report(result)
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-squad",
        description="Adaptive, verifiable multi-agent software engineering runtime.",
    )
    parser.add_argument("--data-dir", default=None, help="root directory for run data")

    # Every subcommand also accepts --data-dir so that it can be given after
    # the subcommand as well as before it.  SUPPRESS keeps the subparser from
    # overwriting the top-level value when it is not supplied.
    def add_common(sub_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub_parser.add_argument(
            "--data-dir",
            default=argparse.SUPPRESS,
            help="root directory for run data",
        )
        return sub_parser

    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = add_common(sub.add_parser("run", help="execute an orchestration run"))
    run_parser.add_argument("objective")
    run_parser.add_argument("--description", default="")
    run_parser.add_argument("--repo", default=".", help="path to the repository")
    run_parser.add_argument("--spec", default=None, help="structured request JSON file")
    run_parser.add_argument("--config", default=None, help="TOML configuration file")
    run_parser.add_argument("--provider", default=None)
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--base-url", default=None)
    run_parser.add_argument("--max-workers", type=int, default=None)
    run_parser.add_argument("--strategy", default=None, choices=["single", "static_two", "static_four", "adaptive"])
    run_parser.add_argument("--no-integrate", action="store_true")
    run_parser.add_argument("--json", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    plan_parser = add_common(sub.add_parser("plan", help="produce a plan without executing it"))
    plan_parser.add_argument("objective")
    plan_parser.add_argument("--description", default="")
    plan_parser.add_argument("--repo", default=".")
    plan_parser.add_argument("--spec", default=None)
    plan_parser.add_argument("--config", default=None)
    plan_parser.add_argument("--provider", default=None)
    plan_parser.add_argument("--model", default=None)
    plan_parser.add_argument("--base-url", default=None)
    plan_parser.add_argument("--max-workers", type=int, default=None)
    plan_parser.add_argument("--strategy", default=None)
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.set_defaults(func=cmd_plan)

    status_parser = add_common(sub.add_parser("status", help="show the status of a run"))
    status_parser.add_argument("run_id")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    inspect_parser = add_common(sub.add_parser("inspect", help="inspect a run in detail"))
    inspect_parser.add_argument("run_id")
    inspect_parser.add_argument("--tasks", action="store_true")
    inspect_parser.add_argument("--events", action="store_true")
    inspect_parser.add_argument("--artifacts", action="store_true")
    inspect_parser.add_argument("--messages", action="store_true")
    inspect_parser.add_argument("--checkpoints", action="store_true")
    inspect_parser.add_argument("--decisions", action="store_true")
    inspect_parser.add_argument(
        "--export", action="store_true", help="include the newline-delimited event log"
    )
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(func=cmd_inspect)

    approve_parser = add_common(sub.add_parser("approve", help="answer a pending approval request"))
    approve_parser.add_argument("approval_id")
    approve_parser.add_argument("--run-id", dest="run_id", default=None)
    approve_parser.add_argument("--decision", default="yes", choices=["yes", "no"])
    approve_parser.add_argument("--who", default=None)
    approve_parser.add_argument("--reason", default=None)
    approve_parser.add_argument("--json", action="store_true")
    approve_parser.set_defaults(func=cmd_approve)

    cancel_parser = add_common(sub.add_parser("cancel", help="cancel a run"))
    cancel_parser.add_argument("run_id")
    cancel_parser.add_argument("--reason", default=None)
    cancel_parser.add_argument("--json", action="store_true")
    cancel_parser.set_defaults(func=cmd_cancel)

    resume_parser = add_common(sub.add_parser("resume", help="recover an interrupted run"))
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--json", action="store_true")
    resume_parser.set_defaults(func=cmd_resume)

    replay_parser = add_common(sub.add_parser("replay", help="replay a recorded run"))
    replay_parser.add_argument("run_id")
    replay_parser.add_argument("--checkpoint", default=None)
    replay_parser.add_argument("--json", action="store_true")
    replay_parser.set_defaults(func=cmd_replay)

    bench_parser = add_common(sub.add_parser("benchmark", help="run the benchmark suite"))
    bench_parser.add_argument("--suite", default="demo")
    bench_parser.add_argument("--trials", type=int, default=1)
    bench_parser.add_argument("--strategies", default=None)
    bench_parser.add_argument("--json", action="store_true")
    bench_parser.set_defaults(func=cmd_benchmark)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AgentSquadError as exc:
        console.print(f"[red]error:[/red] {exc.message}")
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        console.print("[yellow]interrupted[/yellow]")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
