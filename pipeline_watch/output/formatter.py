"""Terminal (rich) and JSON formatters for pipeline-watch findings.

Matches pipeline-check's reporter styling intentionally — same grade
colour palette, same bar glyphs, same Panel-then-Table layout — so a
user running both tools gets a consistent look.
"""
from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .schema import (
    TOOL_NAME,
    Finding,
    Severity,
    dumps,
    findings_envelope,
    severity_rank,
)

_SEVERITY_STYLE: dict[Severity, str] = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
}

_GRADE_STYLE: dict[str, str] = {
    "A": "bold green",
    "B": "green",
    "C": "yellow",
    "D": "bold red",
}

_GRADE_COLOR: dict[str, str] = {
    "A": "green",
    "B": "green",
    "C": "yellow",
    "D": "red",
}


def _visible(findings: list[Finding]) -> list[Finding]:
    """Sort by most-severe-first so the dashboard panel leads with the worst."""
    return sorted(
        findings,
        key=lambda f: (-severity_rank(f.severity), f.module.value, f.check_id),
    )


def report_terminal(
    findings: list[Finding],
    score_result: dict,
    *,
    console: Console | None = None,
) -> None:
    """Render a rich terminal report.

    The header panel mirrors pipeline-check's "Grade + score bar +
    failure breakdown" shape so operators running both tools on a
    repo don't have to re-orient themselves between reports.
    """
    if console is None:
        console = Console()

    grade = score_result["grade"]
    summary = score_result.get("summary", {})
    total = score_result.get("total", len(findings))
    grade_style = _GRADE_STYLE.get(grade, "white")

    # Severity breakdown — only show counts > 0.
    sev_parts: list[str] = []
    for sev in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        n = summary.get(sev.value, {}).get("count", 0)
        if n == 0:
            continue
        style = _SEVERITY_STYLE[sev]
        sev_parts.append(f"[{style}]{n} {sev.value}[/{style}]")

    # Grade bar — fill proportion matches the letter grade, same widths
    # as pipeline-check so it lines up visually when rendered side by side.
    bar_color = _GRADE_COLOR.get(grade, "white")
    filled = {"A": 20, "B": 16, "C": 12, "D": 6}.get(grade, 0)
    bar = f"[{bar_color}]{'#' * filled}[/{bar_color}][dim]{'.' * (20 - filled)}[/dim]"

    header_lines = [
        f"[{grade_style}]Grade {grade}[/{grade_style}]   {bar}  "
        f"[dim]{total} finding(s)[/dim]",
    ]
    if sev_parts:
        header_lines.append("Signals: " + "  ".join(sev_parts))
    else:
        header_lines.append(
            "[green]No behavioural deviations detected against baseline.[/green]"
        )

    console.print(
        Panel(
            "\n".join(header_lines),
            title=f"[bold]{TOOL_NAME}[/bold]",
            border_style="blue",
            padding=(0, 2),
        )
    )
    console.print()

    visible = _visible(findings)
    if not visible:
        return

    table = Table(box=box.SIMPLE_HEAVY, expand=True, pad_edge=False)
    table.add_column("ID", style="bold", no_wrap=True, width=8)
    table.add_column("Severity", no_wrap=True, width=10)
    table.add_column("Module", no_wrap=True, width=14)
    table.add_column("Signal", ratio=1)

    for f in visible:
        sev_style = _SEVERITY_STYLE.get(f.severity, "white")
        table.add_row(
            f.check_id or "-",
            f"[{sev_style}]{f.severity.value}[/{sev_style}]",
            f.module.value,
            f.signal,
        )

    console.print(table)
    console.print()

    # Detail panels — one per finding, severity-tinted border.
    for f in visible:
        style = _SEVERITY_STYLE.get(f.severity, "white")
        evidence_lines: list[str] = []
        for k, v in f.evidence.items():
            evidence_lines.append(f"  [dim]{k}:[/dim] {v}")
        evidence_block = (
            "\n[bold]Evidence:[/bold]\n" + "\n".join(evidence_lines)
            if evidence_lines else ""
        )
        body = (
            f"[bold]Signal:[/bold] {f.signal}\n"
            f"[bold]Baseline:[/bold] {f.baseline}"
            f"{evidence_block}\n"
            f"[bold]Remediation:[/bold] {f.remediation}"
        )
        title_id = f.check_id or f.module.value
        console.print(
            Panel(
                body,
                title=f"[{style}]{title_id}[/{style}]  [dim]{f.timestamp}[/dim]",
                border_style="dim",
                padding=(0, 2),
            )
        )


def report_json(
    findings: list[Finding],
    *,
    tool_version: str,
    module: str | None = None,
) -> str:
    """Serialise findings to the envelope defined in ``schema.findings_envelope``."""
    return dumps(findings_envelope(findings, tool_version=tool_version, module=module))
