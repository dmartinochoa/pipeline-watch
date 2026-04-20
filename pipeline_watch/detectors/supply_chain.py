"""Supply-chain behavioural detector.

Emits a :class:`~pipeline_watch.output.schema.Finding` for each of the
eight signals documented in the README, comparing the live state of a
PyPI manifest's packages against the prior snapshot in the baseline
store.

Design
------
Signals are small top-level functions that take two snapshots (``prev``
and ``current``) plus whatever auxiliary data they need (GitHub client,
sibling package names for typosquat) and return zero or more
``Finding`` objects. The top-level :func:`scan` drives the pipeline:

    for entry in manifest:
        current = fetch + snapshot from pypi
        prev    = store.latest_snapshot(...)
        findings += signal_new_maintainer(prev, current, github_probe)
        findings += signal_off_hours_release(prev, current, stats)
        ...
        store.record_snapshot(current)
    store_stats_refresh()

Two-pass typosquat / cross-ecosystem signals run over the full
manifest after the per-package loop so pairwise work happens once.

All network calls flow through the providers package's fetcher hooks,
so tests can run end-to-end without touching the real network.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import Levenshtein

from ..baseline.stats import refresh_package_hour_stats
from ..baseline.store import PackageSnapshot, Store
from ..output.schema import Finding, Module, Severity
from ..providers import github as _github
from ..providers import npm as _npm
from ..providers import pypi as _pypi

# Signal IDs — documented in README under "Detection modules".
SIGNAL_IDS = {
    "SC-001": "new-maintainer-no-commit-history",
    "SC-002": "release-outside-historical-hour-window",
    "SC-003": "release-without-git-tag",
    "SC-004": "install-script-hook-added",
    "SC-005": "new-transitive-dependency",
    "SC-006": "version-constraint-loosened",
    "SC-007": "typosquat-distance",
    "SC-008": "cross-ecosystem-new-registration",
}


# ── Manifest parsing ────────────────────────────────────────────────


@dataclass
class ManifestEntry:
    name: str
    constraint: str
    source_line: str


_MANIFEST_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._\-]+)"
    r"(?:\[[^\]]*\])?"                                # optional extras
    r"\s*(?P<op>==|~=|!=|>=|<=|>|<|===)?\s*"
    r"(?P<ver>[A-Za-z0-9._\-+!]*)"
)


def parse_requirements_txt(path: str | Path) -> list[ManifestEntry]:
    """Parse a ``requirements.txt`` into manifest entries.

    Intentionally simple: one package per non-comment line, first
    ``op + version`` captured as the constraint. We skip pip-only
    directives (``-r``, ``-e``, ``--find-links``) because those
    aren't packages to baseline. A malformed line is skipped with
    no error — baselining is a best-effort observation.
    """
    entries: list[ManifestEntry] = []
    text = Path(path).read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-", "--")):
            continue
        m = _MANIFEST_LINE_RE.match(line)
        if not m or not m.group("name"):
            continue
        name = m.group("name")
        op = m.group("op") or ""
        ver = m.group("ver") or ""
        constraint = f"{op}{ver}" if op else ""
        entries.append(ManifestEntry(name=name, constraint=constraint, source_line=line))
    return entries


# ── Signal functions ────────────────────────────────────────────────


GitHubProbe = Callable[[str, str], tuple[bool, list[str]]]  # (has_commits, tags)
NpmProbe = Callable[[str], _npm.NpmPackageInfo | None]


def signal_new_maintainer(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
    *,
    github_probe: GitHubProbe | None,
    source_repo: str | None,
) -> list[Finding]:
    """SC-001: a maintainer appears who has no commits in the source repo."""
    if prev is None:
        return []
    prev_names = {m.get("name", "").lower() for m in prev.maintainers}
    new_names = [
        m for m in current.maintainers
        if m.get("name") and m["name"].lower() not in prev_names
    ]
    if not new_names:
        return []
    # Use the GitHub probe (if available) to confirm "no commit history".
    # Without a probe we emit MEDIUM instead of HIGH — a new maintainer
    # is still a signal, just lower-confidence.
    findings: list[Finding] = []
    for new_m in new_names:
        has_commits = None
        if github_probe and source_repo:
            parsed = _github.parse_repo_url(source_repo)
            if parsed:
                try:
                    has_commits, _tags = github_probe(*parsed)
                except _github.GitHubError:
                    has_commits = None
                if has_commits:
                    # Legitimate maintainer — do not flag.
                    continue
        severity = Severity.HIGH if has_commits is False else Severity.MEDIUM
        findings.append(Finding(
            check_id="SC-001",
            module=Module.SUPPLY_CHAIN,
            severity=severity,
            signal=(
                f"New maintainer '{new_m['name']}' published "
                f"{current.package} {current.version}."
            ),
            baseline=(
                f"Previous maintainers for {current.package}: "
                f"{', '.join(sorted(prev_names)) or '(none recorded)'}."
            ),
            evidence={
                "package": current.package,
                "version": current.version,
                "new_maintainer": new_m["name"],
                "new_maintainer_email": new_m.get("email", ""),
                "has_commits_in_source_repo": has_commits,
                "source_repo": source_repo or "",
            },
            remediation=(
                "Freeze the dependency and verify the maintainer addition "
                "was announced upstream. Rotate any long-lived credentials "
                "the package ran against."
            ),
            timestamp=current.recorded_at,
        ))
    return findings


def signal_off_hours_release(
    prev_hours: list[int],
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-002: release hour falls outside the historical 90% window.

    Needs at least three prior observations — two samples degenerate
    into "every hour is an outlier". Three is the smallest sample
    that yields a meaningful quantile.
    """
    if current.release_hour is None or len(prev_hours) < 3:
        return []
    from ..baseline.stats import percentile_window
    window = percentile_window([float(h) for h in prev_hours], width=0.90)
    if window is None:
        return []
    low, high = window
    if low <= current.release_hour <= high:
        return []
    return [Finding(
        check_id="SC-002",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal=(
            f"{current.package} {current.version} was published at hour "
            f"{current.release_hour:02d}Z, outside the maintainer's "
            f"historical window."
        ),
        baseline=(
            f"Historical 90% window for {current.package}: "
            f"hours {int(low):02d}Z–{int(high):02d}Z "
            f"across {len(prev_hours)} prior releases."
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "release_hour_utc": current.release_hour,
            "historical_window_low": low,
            "historical_window_high": high,
            "sample_count": len(prev_hours),
        },
        remediation=(
            "Contact the maintainer out-of-band to confirm the release "
            "timing — legitimate maintainers rarely break their own "
            "circadian pattern without notice."
        ),
        timestamp=current.recorded_at,
    )]


def signal_release_without_tag(
    current: PackageSnapshot,
    *,
    github_probe: GitHubProbe | None,
    source_repo: str | None,
) -> list[Finding]:
    """SC-003: current version has no matching git tag in the source repo."""
    if github_probe is None or not source_repo:
        return []
    parsed = _github.parse_repo_url(source_repo)
    if parsed is None:
        return []
    try:
        _has_commits, tags = github_probe(*parsed)
    except _github.GitHubError:
        return []
    if not tags:
        return []
    # Accept both ``v1.2.3`` and ``1.2.3`` styles — and a prefixed
    # package name form (``requests-1.2.3``) that some multi-package
    # repos use.
    candidates = {
        current.version,
        f"v{current.version}",
        f"{current.package}-{current.version}",
        f"{current.package}/{current.version}",
    }
    tag_set = set(tags)
    if candidates & tag_set:
        return []
    return [Finding(
        check_id="SC-003",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.HIGH,
        signal=(
            f"{current.package} {current.version} was published to PyPI but "
            f"no matching tag exists in {source_repo}."
        ),
        baseline=(
            f"Recent tags in source repo: "
            f"{', '.join(tags[:5])}{'…' if len(tags) > 5 else ''}"
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "source_repo": source_repo,
            "tried_tags": sorted(candidates),
        },
        remediation=(
            "Publishing without tagging is the signature of a compromised "
            "registry credential. Pin away from this version, open an "
            "issue upstream, and preserve the sdist for incident response."
        ),
        timestamp=current.recorded_at,
    )]


def signal_install_script_change(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-004: install-script hooks appeared or their hash changed."""
    if prev is None:
        return []
    # Case A: a hook appeared where none existed.
    if not prev.has_install_script and current.has_install_script:
        severity = Severity.HIGH
        signal = (
            f"{current.package} {current.version} added an install-script "
            f"hook (setup.py / __init__.py / pyproject.toml) where the "
            f"previous snapshot had none."
        )
    # Case B: the hash changed between releases.
    elif (
        prev.has_install_script
        and current.has_install_script
        and prev.install_script_hash
        and current.install_script_hash
        and prev.install_script_hash != current.install_script_hash
    ):
        severity = Severity.MEDIUM
        signal = (
            f"{current.package} {current.version} modified its install-script "
            f"content; the hash differs from the previous release."
        )
    else:
        return []
    return [Finding(
        check_id="SC-004",
        module=Module.SUPPLY_CHAIN,
        severity=severity,
        signal=signal,
        baseline=(
            f"Previous snapshot ({prev.version}): "
            f"{'has install script' if prev.has_install_script else 'no install script'}, "
            f"hash={prev.install_script_hash or 'n/a'}"
        ),
        evidence={
            "package": current.package,
            "previous_version": prev.version,
            "current_version": current.version,
            "previous_hash": prev.install_script_hash,
            "current_hash": current.install_script_hash,
        },
        remediation=(
            "Review the diff of setup.py / __init__.py between the two "
            "sdists. Install-script additions are the signature of the "
            "event-stream and ctx-typosquat compromises."
        ),
        timestamp=current.recorded_at,
    )]


def signal_new_transitive_dep(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-005: a dependency in the current snapshot was absent from the prior one."""
    if prev is None:
        return []
    prev_deps = set(prev.dependencies)
    new_deps = sorted(set(current.dependencies) - prev_deps)
    if not new_deps:
        return []
    return [Finding(
        check_id="SC-005",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.LOW,
        signal=(
            f"{current.package} {current.version} pulled in new "
            f"dependencies: {', '.join(new_deps)}."
        ),
        baseline=(
            f"Previous {current.package} dependencies: "
            f"{', '.join(sorted(prev_deps)) or '(none)'}"
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "new_dependencies": new_deps,
            "previous_dependencies": sorted(prev_deps),
        },
        remediation=(
            "A new transitive dependency is not automatically malicious, "
            "but verify the release notes explain it. Run this package "
            "through pipeline-check against the new dep's source tree."
        ),
        timestamp=current.recorded_at,
    )]


def signal_constraint_loosened(
    prev_entry_constraint: str,
    current_entry: ManifestEntry,
) -> list[Finding]:
    """SC-006: manifest constraint went from ``==`` to ``>=`` / unpinned."""
    # We compare the *manifest* constraint, not the package's runtime deps,
    # because SC-006 is about how the consuming repo pins its dependencies.
    prev_c = prev_entry_constraint.strip()
    cur_c = current_entry.constraint.strip()
    if not prev_c.startswith("==") or cur_c.startswith("=="):
        return []
    # Exact-pin dropped — either unpinned entirely or loosened to a range.
    return [Finding(
        check_id="SC-006",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.LOW,
        signal=(
            f"{current_entry.name} constraint relaxed from '{prev_c}' to "
            f"'{cur_c or 'unpinned'}'."
        ),
        baseline=f"Previous manifest pin: {prev_c}",
        evidence={
            "package": current_entry.name,
            "previous_constraint": prev_c,
            "current_constraint": cur_c or "(unpinned)",
        },
        remediation=(
            "Re-pin the dependency. Floating constraints let a compromised "
            "upstream release ship unreviewed into every fresh install."
        ),
    )]


def signal_typosquat(entries: list[ManifestEntry]) -> list[Finding]:
    """SC-007: pair of manifest packages whose names are ≤2 edit distance apart.

    Flags the *pair* — the detector can't know which side is legitimate,
    and the operator needs both names to investigate.
    """
    findings: list[Finding] = []
    names = [e.name.lower() for e in entries]
    n = len(names)
    for i in range(n):
        for j in range(i + 1, n):
            if names[i] == names[j]:
                continue
            d = Levenshtein.distance(names[i], names[j])
            if d <= 2:
                findings.append(Finding(
                    check_id="SC-007",
                    module=Module.SUPPLY_CHAIN,
                    severity=Severity.HIGH,
                    signal=(
                        f"Manifest packages '{entries[i].name}' and "
                        f"'{entries[j].name}' differ by only {d} character(s) — "
                        f"possible typosquat."
                    ),
                    baseline=(
                        "Typosquat campaigns publish names one or two edits "
                        "away from popular packages (Levenshtein ≤ 2)."
                    ),
                    evidence={
                        "package_a": entries[i].name,
                        "package_b": entries[j].name,
                        "levenshtein_distance": d,
                    },
                    remediation=(
                        "Verify one of the two names isn't a transitive "
                        "typosquat. Drop whichever was pulled in most "
                        "recently until you confirm provenance."
                    ),
                ))
    return findings


def signal_cross_ecosystem(
    entry: ManifestEntry,
    *,
    npm_probe: NpmProbe | None,
    now: datetime,
    window_days: int = 30,
) -> list[Finding]:
    """SC-008: same package name newly registered on the other ecosystem.

    Only npm → PyPI cross-check is implemented in Module 1 (the PyPI
    manifest is the input). The reverse direction arrives with the npm
    manifest parser.
    """
    if npm_probe is None:
        return []
    try:
        info = npm_probe(entry.name)
    except _npm.NpmError:
        return []
    if info is None or not info.created_iso:
        return []
    try:
        created = datetime.fromisoformat(info.created_iso.replace("Z", "+00:00"))
    except ValueError:
        return []
    if (now - created) > timedelta(days=window_days):
        return []
    return [Finding(
        check_id="SC-008",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal=(
            f"A package named '{entry.name}' was registered on npm "
            f"{(now - created).days} day(s) ago — matching a PyPI name "
            f"in your manifest."
        ),
        baseline=(
            f"Cross-ecosystem collisions registered within the last "
            f"{window_days} days are a known dependency-confusion vector."
        ),
        evidence={
            "package": entry.name,
            "npm_registered": info.created_iso,
            "window_days": window_days,
        },
        remediation=(
            "Register the same name defensively on the other ecosystem, "
            "or add a resolver pin to prevent a package-manager lookup "
            "from silently switching ecosystems."
        ),
    )]


# ── Orchestrator ────────────────────────────────────────────────────


@dataclass
class ScanResult:
    findings: list[Finding]
    snapshots_recorded: int
    packages_missing_from_registry: list[str]


def scan(
    store: Store,
    entries: list[ManifestEntry],
    *,
    github_probe: GitHubProbe | None = None,
    npm_probe: NpmProbe | None = None,
    mode: str = "scan",
    now: datetime | None = None,
) -> ScanResult:
    """Run every signal against *entries* and return findings + side effects.

    ``mode="init"`` records snapshots but emits no findings — the
    baseline is being established. ``mode="scan"`` (default) does
    both: emits findings against the prior snapshot *and* records
    the new one.
    """
    if mode not in ("scan", "init"):
        raise ValueError(f"mode must be 'scan' or 'init', got {mode!r}")
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()

    findings: list[Finding] = []
    snapshots_recorded = 0
    missing: list[str] = []

    # Remember the prior constraint per package so SC-006 can diff the
    # manifest even though the store only holds package snapshots.
    prior_constraints: dict[str, str] = {}
    for eco, pkg in store.all_packages("pypi"):
        snap = store.latest_snapshot(eco, pkg)
        if snap:
            prior_constraints[pkg] = ""  # placeholder — constraint comes from the manifest side

    for entry in entries:
        pkg_info = _pypi.fetch_package(entry.name, include_install_script_hash=True)
        if pkg_info is None:
            missing.append(entry.name)
            continue
        current = _pypi.snapshot_from_package(pkg_info, recorded_at=now_iso)
        prev = store.latest_snapshot("pypi", entry.name)
        prev_hours = store.release_hours("pypi", entry.name)

        if mode == "scan":
            source_repo = pkg_info.source_repo()
            findings += signal_new_maintainer(
                prev, current,
                github_probe=github_probe, source_repo=source_repo,
            )
            findings += signal_off_hours_release(prev_hours, current)
            findings += signal_release_without_tag(
                current,
                github_probe=github_probe, source_repo=source_repo,
            )
            findings += signal_install_script_change(prev, current)
            findings += signal_new_transitive_dep(prev, current)
            if entry.name in prior_constraints:
                findings += signal_constraint_loosened(
                    prior_constraints[entry.name], entry,
                )
            findings += signal_cross_ecosystem(
                entry, npm_probe=npm_probe, now=now,
            )

        store.record_snapshot(current)
        snapshots_recorded += 1

    if mode == "scan":
        # Typosquat is a pairwise signal — run once over the manifest.
        findings += signal_typosquat(entries)

    refresh_package_hour_stats(store, now=now)
    return ScanResult(
        findings=findings,
        snapshots_recorded=snapshots_recorded,
        packages_missing_from_registry=missing,
    )
