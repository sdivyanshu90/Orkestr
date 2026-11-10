"""Rendering of benchmark results.

The report is written so that a *negative* result is as visible as a
positive one.  Every comparison against the baseline is shown, including
the ones where the multi-agent strategy was worse.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table


def render_report(report: dict[str, Any], console: Console | None = None) -> None:
    """Render a suite report as tables."""
    console = console or Console()
    console.print(
        f"[bold]benchmark suite[/bold] {report['suite']}  "
        f"trials={report['trial_count']}  duration={report['duration_s']}s"
    )

    aggregates = report.get("aggregates", {})
    if aggregates:
        table = Table(title="strategy aggregates")
        table.add_column("strategy")
        table.add_column("workers", justify="right")
        table.add_column("trials", justify="right")
        table.add_column("verified rate", justify="right")
        table.add_column("wall clock s", justify="right")
        table.add_column("tokens", justify="right")
        table.add_column("cost usd", justify="right")
        table.add_column("tool calls", justify="right")
        table.add_column("conflicts", justify="right")
        for name, data in aggregates.items():
            mean = data["mean"]
            table.add_row(
                name,
                str(data["workers"]),
                str(data["trials"]),
                f"{mean.get('verified_completion_rate', 0):.3f}",
                f"{mean.get('wall_clock_s', 0):.2f}",
                f"{mean.get('tokens_used', 0):.0f}",
                f"{mean.get('cost_usd', 0):.4f}",
                f"{mean.get('tool_calls', 0):.0f}",
                f"{mean.get('integration_conflicts', 0):.1f}",
            )
        console.print(table)

    comparisons = report.get("comparisons", [])
    if comparisons:
        table = Table(title="comparisons against the single-agent baseline")
        table.add_column("candidate")
        table.add_column("metric")
        table.add_column("baseline", justify="right")
        table.add_column("candidate", justify="right")
        table.add_column("outcome")
        for comparison in comparisons:
            outcome = comparison.get("outcome") or (
                "better" if comparison["improved"] else "worse"
            )
            marker = {
                "better": "[green]better[/green]",
                "worse": "[red]worse[/red]",
                "unchanged": "[dim]unchanged[/dim]",
            }.get(outcome, outcome)
            table.add_row(
                comparison["candidate"],
                comparison["metric"],
                f"{comparison['baseline_value']:.4f}",
                f"{comparison['candidate_value']:.4f}",
                marker,
            )
        console.print(table)

    trials = report.get("trials", [])
    if trials:
        table = Table(title="individual trials")
        table.add_column("scenario")
        table.add_column("strategy")
        table.add_column("workers", justify="right")
        table.add_column("status")
        table.add_column("verified", justify="right")
        table.add_column("wall clock s", justify="right")
        table.add_column("retries", justify="right")
        for trial in trials:
            metrics = trial["metrics"]
            table.add_row(
                trial["scenario_id"],
                trial["strategy"],
                str(trial["workers"]),
                trial["error"] or "ok",
                f"{metrics['verified_completion_rate']:.2f}",
                f"{metrics['wall_clock_s']:.2f}",
                str(metrics["retries"]),
            )
        console.print(table)

    for note in report.get("notes", []):
        console.print(f"[dim]note: {note}[/dim]")
