"""Rendering of the local-model benchmark report.

The report is written so that a *negative* result is as visible as a
positive one: a model that cannot drive the tool loop is reported as such,
with the numbers that show why.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table


def render_local_report(report: dict[str, Any], console: Console | None = None) -> None:
    """Render the local-model benchmark report."""
    console = console or Console()

    console.print(
        f"[bold]local-model benchmark[/bold]  "
        f"models={len(report.get('models', []))}  "
        f"scenarios={len(report.get('scenarios', []))}  "
        f"strategies={len(report.get('strategies', []))}  "
        f"duration={report.get('duration_s', 0):.0f}s"
    )

    stats = report.get("server_stats") or []
    if stats:
        table = Table(title="models served")
        table.add_column("model")
        table.add_column("family")
        table.add_column("size MB", justify="right")
        table.add_column("GPU layers", justify="right")
        table.add_column("load s", justify="right")
        for entry in stats:
            table.add_row(
                str(entry.get("model")),
                str(entry.get("family")),
                str(entry.get("size_mb", "")),
                str(entry.get("gpu_layers", "")),
                str(entry.get("load_seconds", "")),
            )
        console.print(table)

    aggregates = report.get("aggregates") or {}
    if aggregates:
        table = Table(title="results by model")
        table.add_column("model")
        table.add_column("family")
        table.add_column("runs", justify="right")
        table.add_column("verified", justify="right")
        table.add_column("verified rate", justify="right")
        table.add_column("wall s", justify="right")
        table.add_column("tokens", justify="right")
        table.add_column("model calls", justify="right")
        table.add_column("tool calls", justify="right")
        table.add_column("verif. fails", justify="right")
        for model, data in aggregates.items():
            if data.get("runs", 0) == 0:
                table.add_row(
                    model,
                    "-",
                    "0",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    str(data.get("error") or "not run")[:40],
                )
                continue
            table.add_row(
                model,
                str(data.get("family", "")),
                str(data["runs"]),
                str(data["verified_runs"]),
                f"{data['verified_rate']:.3f}",
                f"{data['mean_wall_clock_s']:.1f}",
                f"{data['mean_tokens']:.0f}",
                f"{data['mean_model_calls']:.1f}",
                f"{data['mean_tool_calls']:.1f}",
                str(data.get("verification_failures", 0)),
            )
        console.print(table)

    trials = report.get("trials") or []
    if trials:
        table = Table(title="individual runs")
        table.add_column("model")
        table.add_column("scenario")
        table.add_column("strategy")
        table.add_column("status")
        table.add_column("verified")
        table.add_column("wall s", justify="right")
        table.add_column("tokens", justify="right")
        table.add_column("error")
        for trial in trials:
            table.add_row(
                str(trial.get("model")),
                str(trial.get("scenario_id")),
                str(trial.get("strategy")),
                str(trial.get("status")),
                "yes" if trial.get("verified") else "no",
                f"{trial.get('wall_clock_s', 0):.1f}",
                str(trial.get("tokens_used", 0)),
                str(trial.get("error") or "")[:50],
            )
        console.print(table)

    excluded = report.get("excluded_models") or []
    if excluded:
        table = Table(title="models considered and excluded")
        table.add_column("model")
        table.add_column("size")
        table.add_column("reason")
        for entry in excluded:
            table.add_row(
                str(entry.get("model")), str(entry.get("size")), str(entry.get("reason"))
            )
        console.print(table)

    for note in report.get("notes", []):
        console.print(f"[dim]note: {note}[/dim]")
