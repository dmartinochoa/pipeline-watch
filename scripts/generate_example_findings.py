"""Regenerate ``findings.json`` from synthetic snapshots.

The committed ``findings.json`` at the repo root is the documented
"what does pipeline-watch output look like?" example — dashboards
consume it, the README links to it, and CI diffs it against a fresh
run to catch schema drift. Running this script keeps it in sync with
the real code path (no hand-edited fixture).

Executed as ``python scripts/generate_example_findings.py`` from the
repo root. No network calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pipeline_watch import __version__
from pipeline_watch.baseline.store import PackageSnapshot
from pipeline_watch.detectors.supply_chain import (
    ManifestEntry,
    signal_constraint_loosened,
    signal_cross_ecosystem,
    signal_dormant_revival,
    signal_install_script_change,
    signal_maintainer_removed,
    signal_new_maintainer,
    signal_new_transitive_dep,
    signal_off_hours_release,
    signal_release_without_tag,
    signal_typosquat,
    signal_version_downgrade,
    signal_yanked_or_deprecated,
)
from pipeline_watch.output.formatter import report_json
from pipeline_watch.providers.npm import NpmPackageInfo

NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def main() -> None:
    prev = PackageSnapshot(
        ecosystem="pypi", package="requests", version="2.31.0",
        maintainers=[{"name": "alice", "email": "a@example.com", "first_seen": "2015-01-01T00:00:00Z"}],
        release_hour=14, release_weekday=0,
        has_install_script=True, install_script_hash="a" * 64,
        dependencies={"urllib3": ">=1.21.1"},
        recorded_at="2026-04-19T12:00:00+00:00",
        manifest_constraint="==2.31.0",
        release_uploaded_at="2023-05-22T14:30:00+00:00",
    )
    current = PackageSnapshot(
        ecosystem="pypi", package="requests", version="2.32.0",
        maintainers=[
            {"name": "mallory", "email": "m@bad.example", "first_seen": ""},
        ],
        release_hour=3, release_weekday=5,
        has_install_script=True, install_script_hash="b" * 64,
        dependencies={"urllib3": ">=1.21.1", "evilmod": "==1.0"},
        recorded_at=NOW.isoformat(),
        manifest_constraint=">=2.31",
        release_uploaded_at=NOW.isoformat(),
        yanked=True,
    )
    findings = []
    findings += signal_new_maintainer(
        prev, current, github_probe=lambda _o, _r: (False, []),
        source_repo="https://github.com/psf/requests",
    )
    findings += signal_off_hours_release(
        [14, 14, 15, 16, 14, 15, 14, 16, 15, 14], current,
    )
    findings += signal_release_without_tag(
        current,
        github_probe=lambda _o, _r: (True, ["v2.30.0", "v2.31.0"]),
        source_repo="https://github.com/psf/requests",
    )
    findings += signal_install_script_change(prev, current)
    findings += signal_new_transitive_dep(prev, current)
    cur_entry = ManifestEntry(name="requests", constraint=">=2.31", source_line="requests>=2.31")
    findings += signal_constraint_loosened("==2.31.0", cur_entry)
    findings += signal_typosquat([
        ManifestEntry(name="requests", constraint="", source_line="requests"),
        ManifestEntry(name="reqeusts", constraint="", source_line="reqeusts"),
    ])
    findings += signal_cross_ecosystem(
        ManifestEntry(name="requests", constraint="", source_line="requests"),
        ecosystem="pypi",
        npm_probe=lambda _n: NpmPackageInfo(
            name="requests",
            created_iso=(NOW.replace(day=10)).isoformat(),
        ),
        pypi_probe=None,
        now=NOW,
    )
    findings += signal_maintainer_removed(prev, current)
    # Downgrade example uses a separate pair so the rest of the example
    # stays as a forward-moving 2.31.0 → 2.32.0 release.
    downgrade_prev = PackageSnapshot(
        ecosystem="pypi", package="colors", version="1.4.2",
        maintainers=[{"name": "marak"}],
        recorded_at="2026-03-01T00:00:00+00:00",
        release_uploaded_at="2022-01-01T03:00:00+00:00",
    )
    downgrade_current = PackageSnapshot(
        ecosystem="pypi", package="colors", version="1.4.1",
        maintainers=[{"name": "marak"}],
        recorded_at=NOW.isoformat(),
        release_uploaded_at=NOW.isoformat(),
    )
    findings += signal_version_downgrade(downgrade_prev, downgrade_current)
    findings += signal_dormant_revival(downgrade_prev, current)
    findings += signal_yanked_or_deprecated(current)

    # Stamp every finding's timestamp to NOW so the example stays
    # reproducible across runs.
    for f in findings:
        f.timestamp = NOW.isoformat()

    out = Path(__file__).resolve().parent.parent / "findings.json"
    out.write_text(
        report_json(findings, tool_version=__version__, module="supply-chain"),
        encoding="utf-8",
    )
    print(f"wrote {out} ({len(findings)} finding(s))")


if __name__ == "__main__":
    main()
