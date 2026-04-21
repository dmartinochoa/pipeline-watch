"""CLI entry point.

Commands
--------
    pipeline_watch baseline init   --manifest PATH --ecosystem {pypi|npm}
    pipeline_watch baseline show   [--package NAME] [--ecosystem ...]
    pipeline_watch baseline reset  --scope {package:NAME|job:REPO:JOB|org:NAME}
    pipeline_watch baseline stats

    pipeline_watch scan deps       --manifest PATH --ecosystem {pypi|npm}
    pipeline_watch scan all        --manifest PATH [--ecosystem ...]

Exit codes
----------
    0   No finding at or above ``--fail-on`` severity (default HIGH) — gate passes
    1   Gate failed — at least one finding at or above that severity
    2   Scanner failure (registry API error, malformed manifest, etc.)
    3   Baseline lookup had nothing to return

The gate threshold mirrors pipeline-check so the two tools can share a
single CI step.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from . import __version__
from .baseline.store import Store, default_baseline_path
from .detectors import supply_chain as _supply
from .output.formatter import report_json, report_terminal
from .output.sarif import to_sarif
from .output.schema import Severity, score_from_findings, severity_rank
from .providers import github as _github
from .providers import npm as _npm
from .providers import pypi as _pypi
from .suppressions import (
    apply_suppressions,
    default_suppression_path,
    load_suppressions,
)


def _tolerate_unencodable_stdio() -> None:
    """Make stdout/stderr tolerate non-ASCII characters on legacy consoles.

    Windows ``cmd.exe`` defaults to cp1252; Rich output can include
    box-drawing characters that cp1252 can't encode. Reconfiguring with
    ``errors='replace'`` degrades the unprintables to ``?`` rather than
    crashing the CLI before it emits anything useful.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except OSError:
            pass


_tolerate_unencodable_stdio()


# ── Helpers ─────────────────────────────────────────────────────────


def _resolve_baseline_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return default_baseline_path()


def _infer_ecosystem(manifest: str, explicit: str | None) -> str:
    """Return the ecosystem for *manifest*.

    Priority: explicit flag > filename heuristic. ``package.json``
    maps to npm; any filename matching ``requirements*.txt`` maps to
    pypi. Ambiguous / unknown filenames raise ``click.UsageError``
    naming the manifest so the operator knows where to look.
    """
    if explicit:
        return explicit.lower()
    name = Path(manifest).name.lower()
    if name == "package.json":
        return "npm"
    if name.startswith("requirements") and name.endswith(".txt"):
        return "pypi"
    raise click.UsageError(
        f"could not infer --ecosystem from {manifest!r}; "
        f"pass --ecosystem {{pypi|npm}} explicitly."
    )


def _parse_skip_ids(raw: str | None) -> frozenset[str]:
    """Normalise and validate a comma-separated list of check IDs.

    Unknown IDs raise ``click.UsageError`` rather than silently passing
    through — otherwise a typo in CI would quietly re-enable a signal
    the operator believed was suppressed.
    """
    if not raw:
        return frozenset()
    requested = {part.strip().upper() for part in raw.split(",") if part.strip()}
    unknown = requested - set(_supply.SIGNAL_IDS)
    if unknown:
        known = ", ".join(sorted(_supply.SIGNAL_IDS))
        raise click.UsageError(
            f"--skip referenced unknown check ID(s): "
            f"{', '.join(sorted(unknown))}. Known: {known}"
        )
    return frozenset(requested)


def _github_probe_factory(enable: bool):
    """Return a probe callable, or None if disabled.

    Signature: ``(owner, repo) -> (has_commits, tags)``. The detector
    passes owner/repo; we answer tags unconditionally and leave the
    ``has_commits`` dimension to the detector's separate lookup.
    """
    if not enable:
        return None
    def probe(owner: str, repo: str) -> tuple[bool, list[str]]:
        tags = _github.list_tags(owner, repo)
        return False, tags
    return probe


def _npm_probe_factory(enable: bool):
    if not enable:
        return None
    def probe(name: str):
        return _npm.package_info(name)
    return probe


def _pypi_probe_factory(enable: bool):
    if not enable:
        return None
    def probe(name: str):
        # SC-008 only needs registration timing — skip the install-
        # script hash probe that fetch_package normally runs.
        return _pypi.fetch_package(name, include_install_script_hash=False)
    return probe


def _debug_factory(verbose: bool, quiet: bool):
    """Return a stderr debug-print helper (or no-op when quiet/not verbose)."""
    if quiet or not verbose:
        return lambda _msg: None
    def _debug(msg: str) -> None:
        click.echo(f"[debug] {msg}", err=True)
    return _debug


# ── Root group ──────────────────────────────────────────────────────


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="pipeline_watch")
@click.option(
    "--baseline-db",
    metavar="PATH",
    default=None,
    help=(
        "Path to the baseline SQLite file. Defaults to "
        "``.pipeline-watch/baseline.db`` when that directory exists at "
        "cwd, otherwise ``~/.pipeline-watch/baseline.db``."
    ),
)
@click.option(
    "--verbose", "-v", is_flag=True, default=False,
    help="Emit [debug] lines to stderr.",
)
@click.option(
    "--quiet", "-q", is_flag=True, default=False,
    help="Suppress all stderr output. Exit code still reflects the gate outcome.",
)
@click.pass_context
def cli(ctx: click.Context, baseline_db: str | None, verbose: bool, quiet: bool) -> None:
    """pipeline-watch — runtime behavioural detection for supply-chain anomalies.

    Companion to pipeline-check. Maintains a per-project SQLite
    baseline of package / CI-run / VCS-event state; each scan flags
    deviations from that baseline as findings in a format compatible
    with pipeline-check's output.
    """
    ctx.ensure_object(dict)
    ctx.obj["baseline_db"] = _resolve_baseline_path(baseline_db)
    ctx.obj["verbose"] = verbose and not quiet
    ctx.obj["quiet"] = quiet


# ── baseline <subcommand> ───────────────────────────────────────────


@cli.group()
def baseline() -> None:
    """Inspect and manage the pipeline-watch baseline database."""


@baseline.command("init")
@click.option("--manifest", required=True, metavar="PATH",
              help="Path to the dependency manifest (requirements.txt or package.json).")
@click.option("--ecosystem",
              type=click.Choice(["pypi", "npm"], case_sensitive=False),
              default=None,
              help="Package ecosystem. Inferred from the manifest filename "
                   "when unset (requirements*.txt → pypi, package.json → npm).")
@click.pass_context
def baseline_init(ctx: click.Context, manifest: str, ecosystem: str | None) -> None:
    """Populate the baseline from a manifest without emitting findings.

    Run this once per project before scanning — establishes what
    "normal" looks like for every listed package. Later ``scan deps``
    invocations compare against these snapshots.
    """
    if not os.path.isfile(manifest):
        raise click.UsageError(f"--manifest file not found: {manifest}")
    ecosystem = _infer_ecosystem(manifest, ecosystem)

    debug = _debug_factory(ctx.obj["verbose"], ctx.obj["quiet"])
    try:
        entries = _supply.parse_manifest(manifest, ecosystem)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    debug(f"parsed {len(entries)} entries from {manifest}")
    if not entries:
        click.echo(f"[init] {manifest} contained no packages.", err=True)
        return

    path = ctx.obj["baseline_db"]
    debug(f"opening baseline at {path}")
    try:
        with Store.open(path) as store:
            result = _supply.scan(store, entries, ecosystem=ecosystem, mode="init")
    except Exception as exc:  # noqa: BLE001
        import traceback
        click.echo(f"[error] baseline init failed: {exc}", err=True)
        click.echo(traceback.format_exc(), err=True, nl=False)
        sys.exit(2)

    if not ctx.obj["quiet"]:
        click.echo(
            f"[init] recorded {result.snapshots_recorded} snapshot(s) "
            f"into {path}.", err=True,
        )
        if result.packages_missing_from_registry:
            click.echo(
                f"[init] {len(result.packages_missing_from_registry)} "
                f"package(s) not found on {ecosystem}: "
                f"{', '.join(result.packages_missing_from_registry)}",
                err=True,
            )


@baseline.command("show")
@click.option("--package", "package_name", metavar="NAME", default=None,
              help="Show the most recent snapshot for this package.")
@click.option("--ecosystem",
              type=click.Choice(["pypi", "npm"], case_sensitive=False),
              default="pypi", show_default=True)
@click.pass_context
def baseline_show(ctx: click.Context, package_name: str | None, ecosystem: str) -> None:
    """Render the latest snapshot(s) from the baseline."""
    console = Console()
    path = ctx.obj["baseline_db"]
    with Store.open(path) as store:
        if package_name:
            snap = store.latest_snapshot(ecosystem.lower(), package_name)
            if snap is None:
                click.echo(
                    f"[show] no snapshot for {ecosystem}:{package_name}. "
                    f"Run 'pipeline_watch baseline init' first.",
                    err=True,
                )
                sys.exit(3)
            _render_snapshot(console, snap)
            return
        pairs = store.all_packages(ecosystem.lower())
        if not pairs:
            click.echo(
                f"[show] the baseline has no {ecosystem} packages yet. "
                f"Run 'pipeline_watch baseline init' first.",
                err=True,
            )
            sys.exit(3)
        table = Table(title=f"Baseline — {ecosystem} packages", box=box.SIMPLE_HEAD)
        table.add_column("Package", style="bold")
        table.add_column("Latest version")
        table.add_column("Recorded at", style="dim")
        for eco, pkg in pairs:
            snap = store.latest_snapshot(eco, pkg)
            if snap:
                table.add_row(pkg, snap.version, snap.recorded_at)
        console.print(table)


def _render_snapshot(console: Console, snap) -> None:
    table = Table(title=f"{snap.package} @ {snap.version}", box=box.SIMPLE_HEAD)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("ecosystem", snap.ecosystem)
    table.add_row("recorded_at", snap.recorded_at)
    table.add_row("release_uploaded_at", snap.release_uploaded_at or "-")
    table.add_row("release_hour (UTC)",
                  "-" if snap.release_hour is None else f"{snap.release_hour:02d}")
    table.add_row("release_weekday",
                  "-" if snap.release_weekday is None else str(snap.release_weekday))
    table.add_row("has_install_script", str(snap.has_install_script))
    table.add_row("install_script_hash", snap.install_script_hash or "-")
    table.add_row("manifest_constraint", snap.manifest_constraint or "-")
    table.add_row("yanked_or_deprecated", str(snap.yanked))
    maintainer_names = ", ".join(m.get("name", "") for m in snap.maintainers) or "-"
    table.add_row("maintainers", maintainer_names)
    deps = ", ".join(f"{k}{v}" for k, v in snap.dependencies.items()) or "-"
    table.add_row("dependencies", deps)
    console.print(table)


@baseline.command("diff")
@click.option("--manifest", required=True, metavar="PATH",
              help="Path to the dependency manifest to compare against the baseline.")
@click.option("--ecosystem",
              type=click.Choice(["pypi", "npm"], case_sensitive=False),
              default=None,
              help="Package ecosystem. Inferred from the manifest filename "
                   "when unset.")
@click.pass_context
def baseline_diff(
    ctx: click.Context, manifest: str, ecosystem: str | None,
) -> None:
    """Dry-run: show what changed between the registry and the baseline.

    Unlike ``scan deps``, diff writes nothing, emits no findings, and
    never fails the gate. Use it when preparing a re-baseline PR to
    preview the side effects before running ``scan --baseline-update``.
    """
    if not os.path.isfile(manifest):
        raise click.UsageError(f"--manifest file not found: {manifest}")
    ecosystem = _infer_ecosystem(manifest, ecosystem)

    try:
        entries = _supply.parse_manifest(manifest, ecosystem)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    path = ctx.obj["baseline_db"]
    console = Console()
    changes = 0
    now_iso = _utc_now_iso()

    with Store.open(path) as store:
        for entry in entries:
            _, current = _supply._fetch_current_snapshot(
                ecosystem, entry, now_iso=now_iso,
            )
            if current is None:
                console.print(
                    f"[yellow]?[/yellow] {entry.name}: not found on "
                    f"{ecosystem} (skipping).",
                )
                continue
            prev = store.latest_snapshot(ecosystem, entry.name)
            diffs = _snapshot_diff(prev, current)
            if not diffs:
                continue
            changes += 1
            console.print(_render_diff(entry.name, prev, current, diffs))

    if changes == 0:
        console.print("[green]No differences between baseline and registry.[/green]")


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _snapshot_diff(prev, current) -> list[tuple[str, str, str]]:
    """Return ``(field, prev_repr, current_repr)`` for fields that changed.

    ``prev`` may be ``None`` (new package). We diff only the
    security-relevant columns — noise like ``recorded_at`` always
    differs and adds nothing.
    """
    def _fmt_maintainers(snap) -> str:
        if snap is None:
            return "(none)"
        return ", ".join(m.get("name", "") for m in snap.maintainers) or "(none)"

    def _fmt_deps(snap) -> str:
        if snap is None:
            return "(none)"
        return ", ".join(f"{k}{v}" for k, v in snap.dependencies.items()) or "(none)"

    def _get(snap, attr, default="-"):
        return default if snap is None else getattr(snap, attr) or default

    fields = [
        ("version", _get(prev, "version", "(new)"), current.version),
        (
            "install_script_hash",
            _get(prev, "install_script_hash", "-"),
            current.install_script_hash or "-",
        ),
        ("maintainers", _fmt_maintainers(prev), _fmt_maintainers(current)),
        ("dependencies", _fmt_deps(prev), _fmt_deps(current)),
        (
            "release_uploaded_at",
            _get(prev, "release_uploaded_at", "-"),
            current.release_uploaded_at or "-",
        ),
        (
            "yanked_or_deprecated",
            "True" if prev and prev.yanked else "False",
            "True" if current.yanked else "False",
        ),
    ]
    return [(k, a, b) for k, a, b in fields if a != b]


def _render_diff(
    package: str, prev, current, diffs: list[tuple[str, str, str]],
) -> Table:
    label = "[cyan]NEW[/cyan]" if prev is None else "[yellow]CHANGED[/yellow]"
    table = Table(
        title=f"{label} {package} @ {current.version}",
        box=box.SIMPLE_HEAD,
    )
    table.add_column("Field", style="bold")
    table.add_column("Baseline", style="dim")
    table.add_column("Current")
    for field_name, before, after in diffs:
        table.add_row(field_name, before, after)
    return table


@baseline.command("reset")
@click.option("--scope", required=True, metavar="SCOPE",
              help="Scope to reset: 'package:NAME', 'job:REPO:JOB', or 'org:NAME'.")
@click.pass_context
def baseline_reset(ctx: click.Context, scope: str) -> None:
    """Delete every record for *scope* from the baseline."""
    path = ctx.obj["baseline_db"]
    with Store.open(path) as store:
        try:
            n = store.reset_scope(scope)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
    click.echo(f"[reset] removed {n} row(s) for scope {scope}.", err=True)


@baseline.command("stats")
@click.pass_context
def baseline_stats(ctx: click.Context) -> None:
    """Show every precomputed statistic pipeline-watch knows about."""
    console = Console()
    path = ctx.obj["baseline_db"]
    with Store.open(path) as store:
        rows = store.all_stats()
    if not rows:
        click.echo("[stats] baseline has no precomputed stats yet.", err=True)
        return
    table = Table(title="Baseline statistics", box=box.SIMPLE_HEAD)
    table.add_column("Scope", style="bold")
    table.add_column("Metric")
    table.add_column("Mean", justify="right")
    table.add_column("Stddev", justify="right")
    table.add_column("N", justify="right")
    table.add_column("Updated", style="dim")
    for r in rows:
        table.add_row(
            r["scope"], r["metric"],
            f"{r['mean']:.2f}" if r["mean"] is not None else "-",
            f"{r['stddev']:.2f}" if r["stddev"] is not None else "-",
            str(r["sample_count"] or 0),
            str(r["last_updated"]),
        )
    console.print(table)


# ── signals ─────────────────────────────────────────────────────────


_SEVERITY_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
}


@cli.command("signals")
@click.option("--output", "-o",
              type=click.Choice(["terminal", "json"], case_sensitive=False),
              default="terminal", show_default=True,
              help="Output format for the signal listing.")
def signals(output: str) -> None:
    """List every implemented check (SC-XXX) with its severity and description.

    The list is driven by the detector's own catalogue, so ``signals``
    stays accurate even when the README drifts. Useful for discovering
    valid IDs to pass to ``scan deps --skip``.
    """
    catalogue = _supply.SIGNAL_CATALOGUE
    if output == "json":
        import json
        payload = {
            "schema_version": "1.0",
            "signals": [
                {"id": sc_id, "slug": slug, "severity": sev.value, "description": desc}
                for sc_id, (slug, sev, desc) in catalogue.items()
            ],
        }
        click.echo(json.dumps(payload, indent=2))
        return

    console = Console()
    table = Table(
        title=f"pipeline-watch signals ({len(catalogue)} checks)",
        box=box.SIMPLE_HEAD,
    )
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Slug", style="dim")
    table.add_column("Description")
    for sc_id, (slug, sev, desc) in catalogue.items():
        style = _SEVERITY_STYLE[sev]
        table.add_row(
            sc_id,
            f"[{style}]{sev.value}[/{style}]",
            slug,
            desc,
        )
    console.print(table)


# ── scan <subcommand> ───────────────────────────────────────────────


@cli.group()
def scan() -> None:
    """Detect behavioural deviations from the baseline."""


@scan.command("deps")
@click.option("--manifest", required=True, metavar="PATH",
              help="Path to the dependency manifest (requirements.txt or package.json).")
@click.option("--ecosystem",
              type=click.Choice(["pypi", "npm"], case_sensitive=False),
              default=None,
              help="Package ecosystem. Inferred from the manifest filename "
                   "when unset (requirements*.txt → pypi, package.json → npm).")
@click.option("--output", "-o",
              type=click.Choice(["terminal", "json", "sarif", "both"], case_sensitive=False),
              default="terminal", show_default=True,
              help="Output format. 'sarif' emits SARIF 2.1.0 for GitHub "
                   "Code Scanning / Azure DevOps.")
@click.option("--output-file", "-O", metavar="PATH", default=None,
              help="Write the structured report to this path. Format tracks --output.")
@click.option("--fail-on",
              type=click.Choice(["CRITICAL", "HIGH", "MEDIUM", "LOW"], case_sensitive=False),
              default="HIGH", show_default=True,
              help="Exit 1 when any finding is at or above this severity.")
@click.option("--no-github", is_flag=True, default=False,
              help="Skip GitHub API calls — SC-001/SC-003 confidence drops to MEDIUM.")
@click.option("--no-cross-ecosystem", is_flag=True, default=False,
              help="Skip cross-ecosystem lookups — disables SC-008.")
@click.option("--skip", "skip_ids", metavar="IDS", default=None,
              help="Comma-separated check IDs to suppress from the report "
                   "and from the gate (e.g. 'SC-005,SC-014'). Unknown IDs error.")
@click.option("--baseline-update", is_flag=True, default=False,
              help="Accept the current state as the new normal. Without "
                   "this flag, packages with findings keep their prior "
                   "snapshot so the same deviation re-flags next run.")
@click.option("--ignore-file", metavar="PATH", default=None,
              help="Path to a suppression file. Defaults to "
                   "<baseline-dir>/ignore.json; missing files are silently skipped.")
@click.option("--no-ignore", is_flag=True, default=False,
              help="Do not load any suppression file for this run.")
@click.pass_context
def scan_deps(
    ctx: click.Context, manifest: str, ecosystem: str | None,
    output: str, output_file: str | None,
    fail_on: str, no_github: bool, no_cross_ecosystem: bool,
    skip_ids: str | None,
    baseline_update: bool,
    ignore_file: str | None, no_ignore: bool,
) -> None:
    """Scan a dependency manifest against the baseline.

    First run needs ``pipeline_watch baseline init`` to establish a
    starting point. Subsequent runs diff the registry's current view
    against the stored snapshot and emit findings.
    """
    if not os.path.isfile(manifest):
        raise click.UsageError(f"--manifest file not found: {manifest}")
    ecosystem = _infer_ecosystem(manifest, ecosystem)
    skip = _parse_skip_ids(skip_ids)

    debug = _debug_factory(ctx.obj["verbose"], ctx.obj["quiet"])
    try:
        entries = _supply.parse_manifest(manifest, ecosystem)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    debug(f"parsed {len(entries)} entries from {manifest}")

    path = ctx.obj["baseline_db"]
    debug(f"opening baseline at {path}")
    github_probe = None if no_github else _github_probe_factory(True)
    # SC-008 is bidirectional: a PyPI manifest cross-checks npm, and
    # vice versa. We enable only the opposite-ecosystem probe.
    if no_cross_ecosystem:
        npm_probe = None
        pypi_probe = None
    else:
        npm_probe = _npm_probe_factory(ecosystem == "pypi")
        pypi_probe = _pypi_probe_factory(ecosystem == "npm")

    try:
        with Store.open(path) as store:
            # When the baseline is empty, surface that clearly rather
            # than silently emitting no findings (and misleading the
            # operator into thinking all is well).
            if not store.all_packages(ecosystem) and not ctx.obj["quiet"]:
                click.echo(
                    "[scan] baseline is empty; treating this run as a fresh init. "
                    "Re-run after a real release to see findings.",
                    err=True,
                )
                mode = "init"
            else:
                mode = "scan"
            result = _supply.scan(
                store, entries, ecosystem=ecosystem,
                github_probe=github_probe,
                npm_probe=npm_probe, pypi_probe=pypi_probe,
                mode=mode, update_baseline=baseline_update,
            )
    except Exception as exc:  # noqa: BLE001
        import traceback
        click.echo(f"[error] scan failed: {exc}", err=True)
        click.echo(traceback.format_exc(), err=True, nl=False)
        sys.exit(2)

    if result.packages_missing_from_registry and not ctx.obj["quiet"]:
        click.echo(
            f"[scan] {len(result.packages_missing_from_registry)} package(s) "
            f"not found on {ecosystem}: "
            f"{', '.join(result.packages_missing_from_registry)}",
            err=True,
        )
    if result.packages_skipped_due_to_findings and not ctx.obj["quiet"]:
        click.echo(
            f"[scan] baseline NOT updated for "
            f"{len(result.packages_skipped_due_to_findings)} package(s) with "
            f"findings: {', '.join(result.packages_skipped_due_to_findings)}. "
            f"Re-run with --baseline-update once reviewed.",
            err=True,
        )

    findings = result.findings
    if skip:
        before = len(findings)
        findings = [f for f in findings if f.check_id not in skip]
        suppressed = before - len(findings)
        if suppressed and not ctx.obj["quiet"]:
            click.echo(
                f"[scan] suppressed {suppressed} finding(s) via --skip "
                f"({', '.join(sorted(skip))}).",
                err=True,
            )

    # Suppression file — applied after --skip so both CI-level flags
    # and project-committed policy stack.
    suppressed_by_file: list = []
    if not no_ignore:
        ignore_path = (
            Path(ignore_file) if ignore_file
            else default_suppression_path(path)
        )
        try:
            loaded = load_suppressions(ignore_path)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        for w in loaded.warnings:
            if not ctx.obj["quiet"]:
                click.echo(f"[scan] {w}", err=True)
        if loaded.suppressions:
            findings, suppressed_by_file = apply_suppressions(
                findings, loaded.suppressions,
            )
            if suppressed_by_file and not ctx.obj["quiet"]:
                click.echo(
                    f"[scan] suppressed {len(suppressed_by_file)} finding(s) "
                    f"via {ignore_path}.",
                    err=True,
                )
                if ctx.obj["verbose"]:
                    for f, rule in suppressed_by_file:
                        click.echo(
                            f"[debug]   {f.check_id} on "
                            f"{f.evidence.get('package', '?')}: "
                            f"{rule.reason}",
                            err=True,
                        )

    score = score_from_findings(findings)

    # Output — terminal goes to stdout unless structured output is also being
    # produced, in which case the terminal goes to stderr so stdout stays parseable.
    is_structured = output in ("json", "sarif", "both")
    structured_text = ""
    if output == "sarif" or (output_file and output == "sarif"):
        structured_text = to_sarif(
            findings, tool_version=__version__, manifest=manifest,
        )
    elif is_structured or output_file:
        structured_text = report_json(
            findings, tool_version=__version__, module="supply-chain",
        )

    if output in ("terminal", "both") and not ctx.obj["quiet"]:
        stderr_console = output == "both"
        console = Console(stderr=stderr_console)
        report_terminal(findings, score, console=console)
    if is_structured or output_file:
        if output_file:
            Path(output_file).write_text(structured_text, encoding="utf-8")
            if not ctx.obj["quiet"]:
                fmt = "SARIF" if output == "sarif" else "JSON"
                click.echo(
                    f"[scan] {fmt} report written to {output_file}", err=True,
                )
        elif is_structured:
            click.echo(structured_text)

    # Gate — exit 1 when any finding meets or exceeds --fail-on severity.
    threshold = Severity(fail_on.upper())
    th_rank = severity_rank(threshold)
    gating = [f for f in findings if severity_rank(f.severity) >= th_rank]
    if gating:
        if not ctx.obj["quiet"]:
            click.echo(
                f"[gate] FAIL — {len(gating)} finding(s) at or above {fail_on}. "
                f"Grade {score['grade']}.",
                err=True,
            )
        sys.exit(1)
    if not ctx.obj["quiet"]:
        click.echo(
            f"[gate] PASS — {len(findings)} finding(s), grade {score['grade']}.",
            err=True,
        )


@scan.command("all")
@click.option("--manifest", required=True, metavar="PATH")
@click.option("--ecosystem",
              type=click.Choice(["pypi", "npm"], case_sensitive=False),
              default=None)
@click.option("--output-file", "-O", default="findings.json", show_default=True,
              metavar="PATH")
@click.option("--fail-on",
              type=click.Choice(["CRITICAL", "HIGH", "MEDIUM", "LOW"], case_sensitive=False),
              default="HIGH", show_default=True)
@click.option("--skip", "skip_ids", metavar="IDS", default=None,
              help="Comma-separated check IDs to suppress (see 'scan deps --help').")
@click.pass_context
def scan_all(
    ctx: click.Context, manifest: str, ecosystem: str | None,
    output_file: str, fail_on: str, skip_ids: str | None,
) -> None:
    """Run every implemented module and write a consolidated report.

    Currently this runs the supply-chain module only — ci-runtime and
    vcs-audit come online in Modules 2 and 3 and will extend this
    command without any CLI shape change.
    """
    ctx.invoke(
        scan_deps,
        manifest=manifest,
        ecosystem=ecosystem,
        output="json",
        output_file=output_file,
        fail_on=fail_on,
        no_github=False,
        no_cross_ecosystem=False,
        skip_ids=skip_ids,
        baseline_update=False,
        ignore_file=None,
        no_ignore=False,
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
