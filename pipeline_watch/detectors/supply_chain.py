"""Supply-chain behavioural detector.

Emits a :class:`~pipeline_watch.output.schema.Finding` for each of the
signals documented in the README (see ``SIGNAL_IDS``), comparing the
live state of a package manifest (PyPI or npm) against the prior
snapshot in the baseline store.

Each signal is a pure function over a pair of snapshots (plus any
extra context it needs — GitHub probes, release history, sibling
names). The top-level :func:`scan` drives the loop, records new
snapshots, and refreshes the precomputed stats.

All network calls flow through the providers' swappable fetchers, so
tests run end-to-end without touching the real network.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import Levenshtein

from ..baseline.stats import refresh_package_hour_stats
from ..baseline.store import PackageSnapshot, Store
from ..output.schema import Finding, Module, Severity
from ..providers import github as _github
from ..providers import npm as _npm
from ..providers import pypi as _pypi

# Signal catalogue — documented in README under "Signals". The tuples
# are (slug, top-severity, short description). Kept as the single
# source of truth so the CLI's ``signals`` subcommand stays honest:
# editing a detector without updating this table is a lint error in
# review, not a silent drift between the code and the docs.
SIGNAL_CATALOGUE: dict[str, tuple[str, Severity, str]] = {
    "SC-001": ("new-maintainer-no-commit-history", Severity.HIGH,
               "New maintainer with no commits in the source repo"),
    "SC-002": ("release-outside-historical-hour-window", Severity.MEDIUM,
               "Release outside the maintainer's 90th-percentile hour window"),
    "SC-003": ("release-without-git-tag", Severity.HIGH,
               "Registry release without a matching git tag upstream"),
    "SC-004": ("install-script-hook-added", Severity.HIGH,
               "Install-hook appeared or its hash changed"),
    "SC-005": ("new-transitive-dependency", Severity.LOW,
               "New transitive dependency since the last snapshot"),
    "SC-006": ("version-constraint-loosened", Severity.LOW,
               "Manifest pin relaxed from ==x.y.z to a floating range"),
    "SC-007": ("typosquat-distance", Severity.HIGH,
               "Two manifest packages within Levenshtein distance ≤ 2"),
    "SC-008": ("cross-ecosystem-new-registration", Severity.MEDIUM,
               "Same name freshly registered on the other ecosystem"),
    "SC-009": ("maintainer-removed", Severity.HIGH,
               "Entire maintainer list replaced — no overlap with prior owners"),
    "SC-010": ("version-downgrade", Severity.HIGH,
               "Registry's advertised latest dropped below the recorded version"),
    "SC-011": ("dormant-package-revival", Severity.MEDIUM,
               "New release after a dormant period > 365 days"),
    "SC-012": ("release-yanked-or-deprecated", Severity.HIGH,
               "Latest release is yanked (PyPI) or deprecated (npm)"),
    "SC-013": ("major-version-jump", Severity.MEDIUM,
               "Major version jumped ≥ 2 in a single release"),
    "SC-014": ("dependency-removed", Severity.LOW,
               "A dependency in the prior snapshot silently disappeared"),
    "SC-015": ("release-outside-historical-weekday-set", Severity.LOW,
               "Release on a weekday the maintainer has never used before"),
    "SC-016": ("prerelease-advertised-as-latest", Severity.MEDIUM,
               "Registry advertises a pre-release (alpha/beta/rc/dev) as latest"),
    "SC-017": ("release-velocity-spike", Severity.MEDIUM,
               "Burst of ≥3 releases in 24h with slow historical cadence"),
    "SC-020": ("maintainer-email-changed", Severity.HIGH,
               "A maintainer kept the same display name but the email changed"),
}

# Backwards-compatible slug-only mapping (legacy callers / tests).
SIGNAL_IDS: dict[str, str] = {k: v[0] for k, v in SIGNAL_CATALOGUE.items()}


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

    One package per non-comment line, first ``op + version`` captured
    as the constraint. Pip-only directives (``-r``, ``-e``,
    ``--find-links``) are skipped because those aren't packages to
    baseline. Malformed lines are skipped silently — baselining is
    best-effort observation.
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


def parse_package_json(path: str | Path) -> list[ManifestEntry]:
    """Parse a ``package.json`` into manifest entries (npm ecosystem)."""
    npm_entries = _npm.parse_package_json(path)
    return [
        ManifestEntry(name=e.name, constraint=e.constraint, source_line=e.source_line)
        for e in npm_entries
    ]


def parse_manifest(path: str | Path, ecosystem: str) -> list[ManifestEntry]:
    """Dispatch to the right parser based on *ecosystem*."""
    eco = ecosystem.lower()
    if eco == "pypi":
        return parse_requirements_txt(path)
    if eco == "npm":
        return parse_package_json(path)
    raise ValueError(f"unsupported ecosystem: {ecosystem!r}")


# ── Signal functions ────────────────────────────────────────────────


GitHubProbe = Callable[[str, str], tuple[bool, list[str]]]  # (has_commits, tags)
NpmProbe = Callable[[str], _npm.NpmPackageInfo | None]
PyPIProbe = Callable[[str], Any]  # returns PyPI package info or None


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
    """SC-002: release hour falls outside the historical 90% window."""
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
    candidates = {
        current.version,
        f"v{current.version}",
        f"{current.package}-{current.version}",
        f"{current.package}/{current.version}",
        f"release-{current.version}",
    }
    tag_set = set(tags)
    if candidates & tag_set:
        return []
    return [Finding(
        check_id="SC-003",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.HIGH,
        signal=(
            f"{current.package} {current.version} was published but "
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
            "issue upstream, and preserve the artifact for incident response."
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
    if not prev.has_install_script and current.has_install_script:
        severity = Severity.HIGH
        signal = (
            f"{current.package} {current.version} added an install-script "
            f"hook where the previous snapshot had none."
        )
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
            "Diff setup.py / __init__.py / package.json scripts between "
            "the two releases. Install-hook changes are the signature of "
            "the event-stream and ctx typosquat compromises."
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
            "but verify the release notes explain it. Run pipeline-check "
            "against the new dep's source tree."
        ),
        timestamp=current.recorded_at,
    )]


def signal_constraint_loosened(
    prev_entry_constraint: str,
    current_entry: ManifestEntry,
) -> list[Finding]:
    """SC-006: manifest constraint relaxed from pinned (``==``) to floating."""
    prev_c = (prev_entry_constraint or "").strip()
    cur_c = current_entry.constraint.strip()
    if not prev_c.startswith("==") or cur_c.startswith("=="):
        return []
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
    """SC-007: pair of manifest packages whose names are ≤2 edit distance apart."""
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
    ecosystem: str,
    npm_probe: NpmProbe | None,
    pypi_probe: PyPIProbe | None,
    now: datetime,
    window_days: int = 30,
) -> list[Finding]:
    """SC-008: same package name newly registered on the other ecosystem.

    Bidirectional: a PyPI manifest cross-checks against npm; an npm
    manifest cross-checks against PyPI. In both directions, a fresh
    cross-registration within *window_days* is the dependency-confusion
    signature.
    """
    other = "npm" if ecosystem == "pypi" else "pypi"
    created_iso = ""
    source_url = ""
    if other == "npm" and npm_probe is not None:
        try:
            info = npm_probe(entry.name)
        except _npm.NpmError:
            return []
        if info is None or not info.created_iso:
            return []
        created_iso = info.created_iso
        source_url = f"https://www.npmjs.com/package/{entry.name}"
    elif other == "pypi" and pypi_probe is not None:
        try:
            pkg = pypi_probe(entry.name)
        except _pypi.PyPIError:
            return []
        if pkg is None or not pkg.releases:
            return []
        # Earliest PyPI upload across all releases == first registration.
        times = sorted(
            [r.upload_time_iso for r in pkg.releases if r.upload_time_iso]
        )
        if not times:
            return []
        created_iso = times[0]
        source_url = f"https://pypi.org/project/{entry.name}/"
    else:
        return []

    try:
        created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    except ValueError:
        return []
    if (now - created) > timedelta(days=window_days):
        return []
    return [Finding(
        check_id="SC-008",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal=(
            f"A package named '{entry.name}' was registered on {other} "
            f"{(now - created).days} day(s) ago — matching a {ecosystem} "
            f"name in your manifest."
        ),
        baseline=(
            f"Cross-ecosystem collisions registered within the last "
            f"{window_days} days are a known dependency-confusion vector."
        ),
        evidence={
            "package": entry.name,
            "manifest_ecosystem": ecosystem,
            "registered_ecosystem": other,
            "registration_iso": created_iso,
            "source_url": source_url,
            "window_days": window_days,
        },
        remediation=(
            "Register the same name defensively on the other ecosystem, "
            "or add a resolver pin so a package-manager lookup can't "
            "silently switch ecosystems."
        ),
    )]


def signal_maintainer_removed(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-009: every previously known maintainer has disappeared.

    A wholesale maintainer swap — with no overlap — is the signature
    of a hostile takeover (npm account handover, abandoned-pkg hijack).
    We intentionally only fire when ``prev`` had maintainers *and*
    none of them remain in ``current``; losing one of many is common
    and handled by operator review.
    """
    if prev is None or not prev.maintainers or not current.maintainers:
        return []
    prev_names = {m.get("name", "").lower() for m in prev.maintainers if m.get("name")}
    curr_names = {m.get("name", "").lower() for m in current.maintainers if m.get("name")}
    if not prev_names or not curr_names:
        return []
    if prev_names & curr_names:
        return []
    return [Finding(
        check_id="SC-009",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.HIGH,
        signal=(
            f"{current.package} maintainer list completely replaced — no "
            f"previously known maintainer remains."
        ),
        baseline=(
            f"Previous maintainers: {', '.join(sorted(prev_names))}. "
            f"Current maintainers: {', '.join(sorted(curr_names))}."
        ),
        evidence={
            "package": current.package,
            "previous_maintainers": sorted(prev_names),
            "current_maintainers": sorted(curr_names),
        },
        remediation=(
            "Freeze the dependency immediately. A total ownership swap is "
            "the fingerprint of a hijacked account or abandoned-package "
            "takeover (ua-parser-js, event-stream)."
        ),
        timestamp=current.recorded_at,
    )]


def signal_version_downgrade(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-010: the registry's latest version is *older* than what we saw before.

    An attacker with publish rights can unpublish/yank the real latest
    and push an older-looking version that installers prefer. A drop in
    the ordered release tuple is always worth investigating.
    """
    if prev is None or not prev.version or not current.version:
        return []
    if _version_tuple(current.version) >= _version_tuple(prev.version):
        return []
    return [Finding(
        check_id="SC-010",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.HIGH,
        signal=(
            f"{current.package}'s advertised latest dropped from "
            f"{prev.version} to {current.version}."
        ),
        baseline=f"Prior recorded version: {prev.version}.",
        evidence={
            "package": current.package,
            "previous_version": prev.version,
            "current_version": current.version,
        },
        remediation=(
            "Pin the last known-good version and investigate whether the "
            "registry account published a rollback or whether the newer "
            "release was unpublished."
        ),
        timestamp=current.recorded_at,
    )]


def signal_dormant_revival(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
    *,
    dormant_days: int = 365,
) -> list[Finding]:
    """SC-011: a release after a long silence — the classic revival-attack shape."""
    if prev is None or not prev.release_uploaded_at or not current.release_uploaded_at:
        return []
    prev_dt = _parse_iso(prev.release_uploaded_at)
    cur_dt = _parse_iso(current.release_uploaded_at)
    if prev_dt is None or cur_dt is None:
        return []
    gap = cur_dt - prev_dt
    if gap < timedelta(days=dormant_days):
        return []
    return [Finding(
        check_id="SC-011",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal=(
            f"{current.package} {current.version} was published "
            f"{gap.days} days after {prev.version} — a long dormant period "
            f"followed by a sudden release."
        ),
        baseline=(
            f"Previous release {prev.version} was uploaded "
            f"{prev.release_uploaded_at}."
        ),
        evidence={
            "package": current.package,
            "previous_version": prev.version,
            "previous_uploaded_at": prev.release_uploaded_at,
            "current_version": current.version,
            "current_uploaded_at": current.release_uploaded_at,
            "dormant_days": gap.days,
            "threshold_days": dormant_days,
        },
        remediation=(
            "Dormant packages that suddenly republish are a prized target "
            "for attackers. Confirm the maintainer announced the revival "
            "before trusting the new release."
        ),
        timestamp=current.recorded_at,
    )]


def signal_yanked_or_deprecated(
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-012: the advertised latest release is yanked (PyPI) or deprecated (npm)."""
    if not current.yanked:
        return []
    ecosystem_label = "yanked" if current.ecosystem == "pypi" else "deprecated"
    return [Finding(
        check_id="SC-012",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.HIGH,
        signal=(
            f"{current.package} {current.version} is currently marked "
            f"{ecosystem_label} on {current.ecosystem}."
        ),
        baseline=(
            f"A {ecosystem_label} release means the maintainer (or the "
            f"registry) has withdrawn the artefact from normal use."
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "ecosystem": current.ecosystem,
        },
        remediation=(
            "Pin off this version. Yanked/deprecated releases are either "
            "broken or actively harmful — installers will still resolve "
            "them unless you pin away."
        ),
        timestamp=current.recorded_at,
    )]


def signal_major_version_jump(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
    *,
    min_jump: int = 2,
) -> list[Finding]:
    """SC-013: the major version advanced by ``min_jump`` or more in a single release.

    Real projects bump majors one at a time (1.x → 2.x). A published
    artefact that jumps 1.x → 4.x in one release is a sign either of
    a registry-account hijack retagging an old fork, or of an attacker
    lifting a newer project's version numbers to masquerade as it.
    """
    if prev is None or not prev.version or not current.version:
        return []
    prev_major = _major_component(prev.version)
    curr_major = _major_component(current.version)
    if prev_major is None or curr_major is None:
        return []
    delta = curr_major - prev_major
    if delta < min_jump:
        return []
    return [Finding(
        check_id="SC-013",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal=(
            f"{current.package} jumped {delta} major version(s) in one "
            f"release: {prev.version} → {current.version}."
        ),
        baseline=(
            f"Prior recorded version was {prev.version} "
            f"(major {prev_major}); current is {current.version} "
            f"(major {curr_major})."
        ),
        evidence={
            "package": current.package,
            "previous_version": prev.version,
            "current_version": current.version,
            "previous_major": prev_major,
            "current_major": curr_major,
            "major_delta": delta,
            "threshold": min_jump,
        },
        remediation=(
            "Confirm the release notes justify the major-version leap. "
            "Skipping majors is a rare, deliberate change — attackers "
            "sometimes publish inflated version numbers to outrank the "
            "real latest in resolver preference."
        ),
        timestamp=current.recorded_at,
    )]


def signal_dependency_removed(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-014: a dependency that was present previously silently disappeared.

    Reverse of SC-005. A removed dependency can be benign (refactor),
    but an attacker who replaces a library call with an inlined or
    obfuscated equivalent will also delete the now-unused dependency.
    Worth surfacing as LOW so a reviewer at least notices.
    """
    if prev is None:
        return []
    prev_deps = set(prev.dependencies)
    current_deps = set(current.dependencies)
    removed = sorted(prev_deps - current_deps)
    if not removed:
        return []
    return [Finding(
        check_id="SC-014",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.LOW,
        signal=(
            f"{current.package} {current.version} dropped "
            f"{len(removed)} dependency(ies): {', '.join(removed)}."
        ),
        baseline=(
            f"Previous {current.package} dependencies: "
            f"{', '.join(sorted(prev_deps)) or '(none)'}"
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "removed_dependencies": removed,
            "previous_dependencies": sorted(prev_deps),
            "current_dependencies": sorted(current_deps),
        },
        remediation=(
            "A removed dependency is usually a refactor, but occasionally "
            "an attacker inlines the replaced library to hide its usage. "
            "Diff the release's source tree against the prior version."
        ),
        timestamp=current.recorded_at,
    )]


def signal_off_weekday_release(
    prev_weekdays: list[int],
    current: PackageSnapshot,
    *,
    min_samples: int = 5,
) -> list[Finding]:
    """SC-015: release on a weekday the maintainer has never used before.

    Complements SC-002 (hour window) with a categorical axis. Requires
    ``min_samples`` historical releases so a brand-new package doesn't
    flood findings on its second upload. We only fire when the current
    weekday has *zero* occurrences in history — a hard "never before"
    signal rather than a statistical outlier.
    """
    if current.release_weekday is None or len(prev_weekdays) < min_samples:
        return []
    if current.release_weekday in set(prev_weekdays):
        return []
    observed = sorted(set(prev_weekdays))
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return [Finding(
        check_id="SC-015",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.LOW,
        signal=(
            f"{current.package} {current.version} was published on "
            f"{weekday_names[current.release_weekday]}, a weekday never "
            f"seen in the maintainer's history."
        ),
        baseline=(
            f"Historical release weekdays for {current.package}: "
            f"{', '.join(weekday_names[w] for w in observed)} "
            f"across {len(prev_weekdays)} prior releases."
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "release_weekday": current.release_weekday,
            "observed_weekdays": observed,
            "sample_count": len(prev_weekdays),
        },
        remediation=(
            "A first-ever weekend or off-day release is usually benign "
            "(vacation shift, co-maintainer handoff) but combined with "
            "SC-001 / SC-002 it tightens the case for a compromised "
            "publisher session."
        ),
        timestamp=current.recorded_at,
    )]


_PRERELEASE_MARKER_RE = re.compile(
    # PEP 440 allows "2.0.0rc1" (no separator); semver and npm prefer
    # "2.0.0-rc1". Accept both: either a separator or a digit precedes
    # the marker, and the marker is followed by end / digit / separator.
    r"(?:^|[.\-_+]|\d)(alpha|beta|pre|dev|nightly|snapshot|rc|a|b)"
    r"(?:\d+)?(?:$|[.\-_+])",
    re.IGNORECASE,
)


def signal_prerelease_as_latest(
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-016: the registry is advertising a pre-release string as the latest version.

    npm dist-tag ``latest`` and PyPI's "latest release" should point at
    a stable version. A maintainer can publish ``1.0.0-beta`` or
    ``2.0.0rc1`` and mark it latest by accident (or intent), exposing
    every unpinned consumer to a non-stable build. An attacker who
    obtains publish rights sometimes ships a pre-release deliberately
    because CI lints often whitelist them as "not a real release".
    """
    if not current.version:
        return []
    if not _PRERELEASE_MARKER_RE.search(current.version):
        return []
    return [Finding(
        check_id="SC-016",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal=(
            f"{current.package}'s advertised latest is {current.version}, "
            f"which carries a pre-release marker."
        ),
        baseline=(
            "Stable consumers expect the 'latest' tag to point at a "
            "release without alpha / beta / rc / dev / nightly markers."
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "ecosystem": current.ecosystem,
        },
        remediation=(
            "Pin to the last known stable version. If the pre-release is "
            "deliberate, confirm upstream intends it as latest — otherwise "
            "open an issue asking the maintainer to retag."
        ),
        timestamp=current.recorded_at,
    )]


def signal_maintainer_email_changed(
    prev: PackageSnapshot | None,
    current: PackageSnapshot,
) -> list[Finding]:
    """SC-020: a maintainer's display name stayed but their email flipped.

    A pure rename (alice@olddomain → alice@newdomain, same display name)
    is the textbook account-takeover footprint: an attacker who gets
    publish rights often changes the email to a mailbox they control
    without touching the visible name, so follow-up notifications land
    on their side. Benign cases (company moves domain, maintainer
    marries) exist — hence HIGH, not CRITICAL.
    """
    if prev is None:
        return []
    prev_by_name: dict[str, str] = {}
    for m in prev.maintainers:
        name = str(m.get("name") or "").strip().lower()
        email = str(m.get("email") or "").strip().lower()
        if name and email:
            prev_by_name[name] = email
    changes: list[dict[str, str]] = []
    for m in current.maintainers:
        name = str(m.get("name") or "").strip().lower()
        email = str(m.get("email") or "").strip().lower()
        if not name or not email:
            continue
        prior = prev_by_name.get(name)
        if prior and prior != email:
            changes.append({
                "maintainer": m.get("name") or "",
                "previous_email": prior,
                "current_email": email,
            })
    if not changes:
        return []
    names = ", ".join(c["maintainer"] for c in changes)
    return [Finding(
        check_id="SC-020",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.HIGH,
        signal=(
            f"{current.package} {current.version}: {len(changes)} maintainer(s) "
            f"kept their display name but changed email ({names})."
        ),
        baseline=(
            "Prior emails for the same names: "
            + "; ".join(
                f"{c['maintainer']} → {c['previous_email']}" for c in changes
            )
        ),
        evidence={
            "package": current.package,
            "version": current.version,
            "changes": changes,
        },
        remediation=(
            "Confirm with the named maintainer out-of-band (not via the "
            "new email). A silent email flip is a common account-takeover "
            "footprint — attackers redirect 2FA / recovery mail before "
            "pushing a poisoned release."
        ),
        timestamp=current.recorded_at,
    )]


def signal_release_velocity_spike(
    history: list[tuple[str, str]],
    current: PackageSnapshot,
    *,
    burst_threshold: int = 3,
    burst_window_hours: int = 24,
    min_quiet_days: float = 7.0,
) -> list[Finding]:
    """SC-017: a burst of releases when the historical cadence is slow.

    ``history`` is the full list of ``(version, release_uploaded_at)``
    pairs from the store — newest first. We count how many *distinct*
    versions landed within ``burst_window_hours`` of ``current``'s
    upload time; if that's ``>= burst_threshold`` *and* the median
    gap across the package's prior releases is ``>= min_quiet_days``,
    fire MEDIUM.

    The median-gap check keeps the signal quiet for packages that
    legitimately ship daily (build tooling, pre-release snapshots) —
    only the *spike relative to the package's own cadence* is the
    anomaly worth surfacing.
    """
    if not current.release_uploaded_at:
        return []
    try:
        cur_dt = datetime.fromisoformat(
            current.release_uploaded_at.replace("Z", "+00:00")
        )
    except ValueError:
        return []

    window = timedelta(hours=burst_window_hours)
    recent_versions: list[str] = [current.version]
    parsed: list[datetime] = []
    for ver, uploaded_at in history:
        try:
            dt = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        parsed.append(dt)
        if (
            ver != current.version
            and abs((cur_dt - dt).total_seconds()) <= window.total_seconds()
        ):
            recent_versions.append(ver)

    if len(set(recent_versions)) < burst_threshold:
        return []

    # Need at least a handful of prior releases to trust the cadence.
    if len(parsed) < 4:
        return []

    # Median gap (in days) across adjacent prior releases (newest first,
    # so flip differences to be positive).
    sorted_dts = sorted(parsed)
    gaps_days = [
        (sorted_dts[i + 1] - sorted_dts[i]).total_seconds() / 86400.0
        for i in range(len(sorted_dts) - 1)
    ]
    gaps_days.sort()
    mid = len(gaps_days) // 2
    if len(gaps_days) % 2:
        median_gap = gaps_days[mid]
    else:
        median_gap = (gaps_days[mid - 1] + gaps_days[mid]) / 2.0
    if median_gap < min_quiet_days:
        return []

    return [Finding(
        check_id="SC-017",
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal=(
            f"{current.package}: {len(set(recent_versions))} distinct versions "
            f"released within {burst_window_hours}h — a burst against a "
            f"~{median_gap:.1f}-day median cadence."
        ),
        baseline=(
            f"Historically the package shipped on a ~{median_gap:.1f}-day "
            f"cadence across {len(parsed)} recorded releases. A rapid "
            f"burst is rare enough to surface."
        ),
        evidence={
            "package": current.package,
            "current_version": current.version,
            "versions_in_window": sorted(set(recent_versions)),
            "burst_window_hours": burst_window_hours,
            "historical_release_count": len(parsed),
            "historical_median_gap_days": round(median_gap, 2),
            "threshold": burst_threshold,
        },
        remediation=(
            "A release burst is a classic compromised-publisher pattern "
            "(attackers often ship several quick tags to bury a malicious "
            "one in the feed). Confirm each release corresponds to a "
            "distinct, reviewed commit upstream."
        ),
        timestamp=current.recorded_at,
    )]


# ── Orchestrator ────────────────────────────────────────────────────


@dataclass
class ScanResult:
    findings: list[Finding]
    snapshots_recorded: int
    packages_missing_from_registry: list[str]
    #: Packages seen during scan that had *some* finding and were not
    #: recorded because ``update_baseline`` was false. Surfaced so the
    #: CLI can tell operators exactly what they need to re-examine /
    #: accept with ``--baseline-update``.
    packages_skipped_due_to_findings: list[str] = field(default_factory=list)


def scan(
    store: Store,
    entries: list[ManifestEntry],
    *,
    ecosystem: str = "pypi",
    github_probe: GitHubProbe | None = None,
    npm_probe: NpmProbe | None = None,
    pypi_probe: PyPIProbe | None = None,
    mode: str = "scan",
    now: datetime | None = None,
    update_baseline: bool = True,
    fetch_workers: int = 8,
) -> ScanResult:
    """Run every signal against *entries* and return findings + side effects.

    ``mode="init"`` records snapshots but emits no findings — the
    baseline is being established. ``mode="scan"`` (default) also
    emits findings against the prior snapshot.

    ``update_baseline`` controls whether the *new* snapshot is written
    when findings were raised for the package:

    * ``True`` (the default, and the only behaviour in ``mode="init"``)
      — always record the new snapshot. A subsequent scan treats the
      current state as the new normal.
    * ``False`` — when any finding fired for the package, keep the
      prior snapshot in place so the same deviation re-flags on the
      next run. Packages with zero findings still record (history
      continues to grow for quiet dependencies).

    The ``False`` mode pairs with the CLI's ``--baseline-update`` flag:
    without the flag, findings persist across CI runs until an operator
    deliberately accepts them.
    """
    if mode not in ("scan", "init"):
        raise ValueError(f"mode must be 'scan' or 'init', got {mode!r}")
    if ecosystem not in ("pypi", "npm"):
        raise ValueError(f"ecosystem must be 'pypi' or 'npm', got {ecosystem!r}")
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()

    findings: list[Finding] = []
    snapshots_recorded = 0
    missing: list[str] = []
    skipped_due_to_findings: list[str] = []

    # Parallelise the network-bound registry fetches. Signals and
    # store writes stay on the main thread so stats and per-package
    # ordering remain deterministic, and SQLite doesn't see concurrent
    # writers. The workers touch only the injected fetchers which are
    # already stateless.
    if len(entries) > 1 and fetch_workers > 1:
        with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            fetched = list(pool.map(
                lambda e: _fetch_current_snapshot(
                    ecosystem, e, now_iso=now_iso,
                ),
                entries,
            ))
    else:
        fetched = [
            _fetch_current_snapshot(ecosystem, e, now_iso=now_iso)
            for e in entries
        ]

    for entry, (pkg_info, current) in zip(entries, fetched, strict=True):
        if current is None:
            missing.append(entry.name)
            continue
        prev = store.latest_snapshot(ecosystem, entry.name)
        prev_hours = store.release_hours(ecosystem, entry.name)
        prev_weekdays = store.release_weekdays(ecosystem, entry.name)
        pkg_findings: list[Finding] = []

        if mode == "scan":
            source_repo = _source_repo(pkg_info)
            pkg_findings += signal_new_maintainer(
                prev, current,
                github_probe=github_probe, source_repo=source_repo,
            )
            pkg_findings += signal_off_hours_release(prev_hours, current)
            pkg_findings += signal_off_weekday_release(prev_weekdays, current)
            pkg_findings += signal_release_without_tag(
                current,
                github_probe=github_probe, source_repo=source_repo,
            )
            pkg_findings += signal_install_script_change(prev, current)
            pkg_findings += signal_new_transitive_dep(prev, current)
            pkg_findings += signal_dependency_removed(prev, current)
            pkg_findings += signal_maintainer_removed(prev, current)
            pkg_findings += signal_maintainer_email_changed(prev, current)
            pkg_findings += signal_version_downgrade(prev, current)
            pkg_findings += signal_major_version_jump(prev, current)
            pkg_findings += signal_dormant_revival(prev, current)
            pkg_findings += signal_yanked_or_deprecated(current)
            pkg_findings += signal_prerelease_as_latest(current)
            pkg_findings += signal_release_velocity_spike(
                store.distinct_version_upload_times(ecosystem, entry.name),
                current,
            )
            if prev is not None:
                pkg_findings += signal_constraint_loosened(
                    prev.manifest_constraint or "", entry,
                )
            pkg_findings += signal_cross_ecosystem(
                entry,
                ecosystem=ecosystem,
                npm_probe=npm_probe, pypi_probe=pypi_probe,
                now=now,
            )

        findings.extend(pkg_findings)

        record = update_baseline or mode == "init" or prev is None or not pkg_findings
        if record:
            store.record_snapshot(current)
            snapshots_recorded += 1
        else:
            skipped_due_to_findings.append(entry.name)

    if mode == "scan":
        findings += signal_typosquat(entries)

    refresh_package_hour_stats(store, now=now)
    return ScanResult(
        findings=findings,
        snapshots_recorded=snapshots_recorded,
        packages_missing_from_registry=missing,
        packages_skipped_due_to_findings=skipped_due_to_findings,
    )


# ── Internal helpers ────────────────────────────────────────────────


def _fetch_current_snapshot(
    ecosystem: str,
    entry: ManifestEntry,
    *,
    now_iso: str,
) -> tuple[Any, PackageSnapshot | None]:
    if ecosystem == "pypi":
        pypi_pkg = _pypi.fetch_package(entry.name, include_install_script_hash=True)
        if pypi_pkg is None:
            return None, None
        snap = _pypi.snapshot_from_package(
            pypi_pkg, recorded_at=now_iso, manifest_constraint=entry.constraint,
        )
        return pypi_pkg, snap
    if ecosystem == "npm":
        npm_pkg = _npm.fetch_package(entry.name)
        if npm_pkg is None:
            return None, None
        snap = _npm.snapshot_from_package(
            npm_pkg, recorded_at=now_iso, manifest_constraint=entry.constraint,
        )
        return npm_pkg, snap
    raise ValueError(f"unsupported ecosystem: {ecosystem!r}")


def _source_repo(pkg_info: Any) -> str | None:
    if pkg_info is None:
        return None
    getter = getattr(pkg_info, "source_repo", None)
    return getter() if callable(getter) else None


_VERSION_COMPONENT_RE = re.compile(r"^(\d+)(.*)$")


def _major_component(v: str) -> int | None:
    """Return the leading integer of a version string, or ``None``.

    ``"1.2.3"`` → 1, ``"v4.0.0-rc1"`` → 4, ``"unknown"`` → None.
    SC-013 only needs the major; SC-010 keeps using the fuller tuple.
    """
    if not v:
        return None
    head = v.lstrip("vV").split(".", 1)[0]
    m = _VERSION_COMPONENT_RE.match(head)
    if not m:
        return None
    return int(m.group(1))


def _version_tuple(v: str) -> tuple:
    """Loose ordering for PEP 440 / semver-ish version strings.

    Not a full parser — we only need to detect a *drop*. Non-numeric
    tails sort after numeric components for the same position so
    ``1.0.0`` > ``1.0.0rc1``. Unparseable components fall back to
    string comparison, which is good enough to notice rollbacks.
    """
    parts = re.split(r"[.\-+]", v.strip())
    tupled: list[tuple[int, int, str]] = []
    for p in parts:
        m = _VERSION_COMPONENT_RE.match(p)
        if m:
            tupled.append((0, int(m.group(1)), m.group(2) or ""))
        else:
            tupled.append((1, 0, p))
    return tuple(tupled)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
