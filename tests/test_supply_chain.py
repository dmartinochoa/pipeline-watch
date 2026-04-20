"""Per-signal supply-chain tests — every signal has trigger + no-trigger cases."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline_watch.baseline.store import PackageSnapshot, Store
from pipeline_watch.detectors.supply_chain import (
    ManifestEntry,
    parse_requirements_txt,
    scan,
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
from pipeline_watch.output.schema import Severity
from pipeline_watch.providers import npm as _npm
from pipeline_watch.providers.pypi import PYPI_JSON_URL

NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def _snap(**overrides) -> PackageSnapshot:
    base = PackageSnapshot(
        ecosystem="pypi",
        package="requests",
        version="2.31.0",
        maintainers=[{"name": "alice", "email": "a@example.com", "first_seen": ""}],
        release_hour=14,
        release_weekday=1,
        has_install_script=False,
        install_script_hash=None,
        dependencies={"urllib3": ">=1.21.1"},
        recorded_at="2026-04-20T12:00:00+00:00",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ── SC-001 new maintainer ───────────────────────────────────────────


def test_sc001_fires_when_new_maintainer_has_no_commits() -> None:
    prev = _snap()
    current = _snap(
        maintainers=[
            {"name": "alice", "email": "a@example.com", "first_seen": ""},
            {"name": "mallory", "email": "m@bad.example", "first_seen": ""},
        ],
    )
    # Probe: mallory has no commits, tags empty.
    def probe(owner, repo):  # noqa: ARG001
        return False, []
    findings = signal_new_maintainer(
        prev, current, github_probe=probe, source_repo="https://github.com/psf/requests",
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "SC-001"
    assert f.severity == Severity.HIGH  # has_commits = False → HIGH
    assert "mallory" in f.signal


def test_sc001_skipped_when_new_maintainer_is_known_contributor() -> None:
    prev = _snap()
    current = _snap(
        maintainers=[
            {"name": "alice", "email": "a@example.com", "first_seen": ""},
            {"name": "bob", "email": "b@example.com", "first_seen": ""},
        ],
    )
    def probe(owner, repo):  # noqa: ARG001
        return True, []  # bob has commits → legitimate
    findings = signal_new_maintainer(
        prev, current, github_probe=probe, source_repo="https://github.com/psf/requests",
    )
    assert findings == []


def test_sc001_medium_severity_without_github_probe() -> None:
    prev = _snap()
    current = _snap(
        maintainers=[
            {"name": "alice", "email": "a@example.com", "first_seen": ""},
            {"name": "mallory", "email": "m@bad.example", "first_seen": ""},
        ],
    )
    findings = signal_new_maintainer(prev, current, github_probe=None, source_repo=None)
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM


def test_sc001_no_finding_when_no_prior_snapshot() -> None:
    current = _snap()
    findings = signal_new_maintainer(None, current, github_probe=None, source_repo=None)
    assert findings == []


# ── SC-002 off-hours release ────────────────────────────────────────


def test_sc002_fires_outside_historical_window() -> None:
    current = _snap(release_hour=3)
    # Baseline clustered around 14-16Z for 10 samples.
    prev_hours = [14, 14, 15, 14, 16, 15, 14, 15, 14, 16]
    findings = signal_off_hours_release(prev_hours, current)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-002"
    assert "03Z" in findings[0].signal


def test_sc002_skipped_inside_window() -> None:
    current = _snap(release_hour=15)
    prev_hours = [14, 14, 15, 14, 16, 15, 14, 15, 14, 16]
    assert signal_off_hours_release(prev_hours, current) == []


def test_sc002_needs_three_samples() -> None:
    # Two samples: not enough history to define a window.
    current = _snap(release_hour=3)
    assert signal_off_hours_release([14, 15], current) == []


# ── SC-003 release without tag ──────────────────────────────────────


def test_sc003_fires_when_no_matching_tag() -> None:
    current = _snap(version="2.99.0")
    def probe(owner, repo):  # noqa: ARG001
        return True, ["v2.30.0", "v2.31.0", "v2.32.0"]
    findings = signal_release_without_tag(
        current, github_probe=probe, source_repo="https://github.com/psf/requests",
    )
    assert len(findings) == 1
    assert findings[0].check_id == "SC-003"


def test_sc003_accepts_plain_and_v_prefixed_tags() -> None:
    current = _snap(version="2.31.0")
    def probe_v(owner, repo):  # noqa: ARG001
        return True, ["v2.31.0"]
    def probe_plain(owner, repo):  # noqa: ARG001
        return True, ["2.31.0"]
    assert signal_release_without_tag(
        current, github_probe=probe_v, source_repo="https://github.com/psf/requests",
    ) == []
    assert signal_release_without_tag(
        current, github_probe=probe_plain, source_repo="https://github.com/psf/requests",
    ) == []


def test_sc003_skipped_without_probe_or_repo() -> None:
    current = _snap(version="2.99.0")
    assert signal_release_without_tag(current, github_probe=None, source_repo="x") == []
    def probe(owner, repo):  # noqa: ARG001
        return True, ["v2.99.0"]
    assert signal_release_without_tag(current, github_probe=probe, source_repo=None) == []


# ── SC-004 install-script change ────────────────────────────────────


def test_sc004_fires_when_hook_appears() -> None:
    prev = _snap(has_install_script=False, install_script_hash=None)
    current = _snap(has_install_script=True, install_script_hash="a" * 64)
    findings = signal_install_script_change(prev, current)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-004"
    assert findings[0].severity == Severity.HIGH


def test_sc004_fires_when_hash_changes() -> None:
    prev = _snap(has_install_script=True, install_script_hash="a" * 64)
    current = _snap(has_install_script=True, install_script_hash="b" * 64)
    findings = signal_install_script_change(prev, current)
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM


def test_sc004_skipped_when_hash_unchanged() -> None:
    prev = _snap(has_install_script=True, install_script_hash="a" * 64)
    current = _snap(has_install_script=True, install_script_hash="a" * 64)
    assert signal_install_script_change(prev, current) == []


# ── SC-005 new transitive dependency ────────────────────────────────


def test_sc005_fires_on_new_dep() -> None:
    prev = _snap(dependencies={"urllib3": ">=1.21"})
    current = _snap(dependencies={"urllib3": ">=1.21", "requests-new-dep": ">=1.0"})
    findings = signal_new_transitive_dep(prev, current)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-005"
    assert "requests-new-dep" in findings[0].signal


def test_sc005_skipped_when_deps_unchanged() -> None:
    prev = _snap(dependencies={"urllib3": ">=1.21"})
    current = _snap(dependencies={"urllib3": ">=1.21"})
    assert signal_new_transitive_dep(prev, current) == []


# ── SC-006 constraint loosened ──────────────────────────────────────


def test_sc006_fires_on_pin_loosening() -> None:
    cur = ManifestEntry(name="requests", constraint=">=2.31", source_line="requests>=2.31")
    findings = signal_constraint_loosened("==2.31.0", cur)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-006"


def test_sc006_skipped_when_still_pinned() -> None:
    cur = ManifestEntry(name="requests", constraint="==2.32.0", source_line="requests==2.32.0")
    assert signal_constraint_loosened("==2.31.0", cur) == []


def test_sc006_skipped_when_never_pinned() -> None:
    cur = ManifestEntry(name="requests", constraint=">=2.31", source_line="requests>=2.31")
    assert signal_constraint_loosened(">=2.30", cur) == []


# ── SC-007 typosquat ────────────────────────────────────────────────


def test_sc007_flags_pair_within_distance_2() -> None:
    entries = [
        ManifestEntry(name="requests", constraint="", source_line="requests"),
        ManifestEntry(name="reqeusts", constraint="", source_line="reqeusts"),
    ]
    findings = signal_typosquat(entries)
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "SC-007"
    assert {f.evidence["package_a"], f.evidence["package_b"]} == {"requests", "reqeusts"}


def test_sc007_skipped_when_distance_exceeds_2() -> None:
    entries = [
        ManifestEntry(name="requests", constraint="", source_line="requests"),
        ManifestEntry(name="flask", constraint="", source_line="flask"),
    ]
    assert signal_typosquat(entries) == []


def test_sc007_skipped_on_exact_duplicate() -> None:
    entries = [
        ManifestEntry(name="Requests", constraint="", source_line="Requests"),
        ManifestEntry(name="requests", constraint="", source_line="requests"),
    ]
    # Case-folded exact match, not a typosquat.
    assert signal_typosquat(entries) == []


# ── SC-008 cross-ecosystem new registration ─────────────────────────


def test_sc008_fires_within_window() -> None:
    entry = ManifestEntry(name="requests", constraint="", source_line="requests")
    created = (NOW - timedelta(days=10)).isoformat()
    def probe(name: str):  # noqa: ARG001
        return _npm.NpmPackageInfo(name=name, created_iso=created)
    findings = signal_cross_ecosystem(
        entry, ecosystem="pypi", npm_probe=probe, pypi_probe=None, now=NOW,
    )
    assert len(findings) == 1
    assert findings[0].check_id == "SC-008"


def test_sc008_skipped_when_registered_long_ago() -> None:
    entry = ManifestEntry(name="requests", constraint="", source_line="requests")
    created = (NOW - timedelta(days=365)).isoformat()
    def probe(name: str):  # noqa: ARG001
        return _npm.NpmPackageInfo(name=name, created_iso=created)
    assert signal_cross_ecosystem(
        entry, ecosystem="pypi", npm_probe=probe, pypi_probe=None, now=NOW,
    ) == []


def test_sc008_skipped_when_not_on_npm() -> None:
    entry = ManifestEntry(name="requests", constraint="", source_line="requests")
    def probe(name: str):  # noqa: ARG001
        return None
    assert signal_cross_ecosystem(
        entry, ecosystem="pypi", npm_probe=probe, pypi_probe=None, now=NOW,
    ) == []


def test_sc008_npm_manifest_cross_checks_pypi() -> None:
    """An npm manifest entry cross-checks PyPI in the opposite direction."""
    from pipeline_watch.providers.pypi import PyPIPackage, PyPIRelease

    entry = ManifestEntry(name="internal-utils", constraint="", source_line="internal-utils")
    recent = (NOW - timedelta(days=5)).isoformat()

    def probe(name: str):  # noqa: ARG001
        return PyPIPackage(
            name=name, latest_version="0.1.0", maintainers=[],
            releases=[PyPIRelease(
                version="0.1.0", upload_time_iso=recent,
                sdist_url=None, has_install_script=False, install_script_hash=None,
            )],
            dependencies={}, project_urls={},
        )
    findings = signal_cross_ecosystem(
        entry, ecosystem="npm", npm_probe=None, pypi_probe=probe, now=NOW,
    )
    assert len(findings) == 1
    assert findings[0].check_id == "SC-008"
    assert findings[0].evidence["registered_ecosystem"] == "pypi"


# ── SC-009 maintainer removed ───────────────────────────────────────


def test_sc009_fires_on_complete_swap() -> None:
    prev = _snap(maintainers=[{"name": "alice"}, {"name": "bob"}])
    current = _snap(maintainers=[{"name": "mallory"}, {"name": "eve"}])
    findings = signal_maintainer_removed(prev, current)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-009"
    assert findings[0].severity == Severity.HIGH


def test_sc009_skipped_when_any_overlap() -> None:
    prev = _snap(maintainers=[{"name": "alice"}, {"name": "bob"}])
    current = _snap(maintainers=[{"name": "alice"}, {"name": "mallory"}])
    assert signal_maintainer_removed(prev, current) == []


def test_sc009_needs_both_sides_populated() -> None:
    prev = _snap(maintainers=[])
    current = _snap(maintainers=[{"name": "mallory"}])
    assert signal_maintainer_removed(prev, current) == []


# ── SC-010 version downgrade ────────────────────────────────────────


def test_sc010_fires_when_latest_drops() -> None:
    prev = _snap(version="2.31.0")
    current = _snap(version="2.30.0")
    findings = signal_version_downgrade(prev, current)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-010"


def test_sc010_skipped_on_forward_progress() -> None:
    prev = _snap(version="2.31.0")
    current = _snap(version="2.32.0")
    assert signal_version_downgrade(prev, current) == []


def test_sc010_skipped_on_same_version() -> None:
    prev = _snap(version="2.31.0")
    current = _snap(version="2.31.0")
    assert signal_version_downgrade(prev, current) == []


# ── SC-011 dormant revival ──────────────────────────────────────────


def test_sc011_fires_after_long_silence() -> None:
    prev = _snap(
        version="1.0.0",
        release_uploaded_at="2020-01-01T12:00:00+00:00",
    )
    current = _snap(
        version="1.1.0",
        release_uploaded_at="2026-04-20T12:00:00+00:00",
    )
    findings = signal_dormant_revival(prev, current)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-011"


def test_sc011_skipped_within_dormant_threshold() -> None:
    prev = _snap(release_uploaded_at="2026-01-01T00:00:00+00:00")
    current = _snap(
        version="1.1.0",
        release_uploaded_at="2026-04-20T00:00:00+00:00",
    )
    assert signal_dormant_revival(prev, current) == []


def test_sc011_skipped_without_timestamps() -> None:
    prev = _snap(release_uploaded_at="")
    current = _snap(release_uploaded_at="")
    assert signal_dormant_revival(prev, current) == []


# ── SC-012 yanked or deprecated ─────────────────────────────────────


def test_sc012_fires_when_yanked() -> None:
    current = _snap(yanked=True)
    findings = signal_yanked_or_deprecated(current)
    assert len(findings) == 1
    assert findings[0].check_id == "SC-012"
    assert findings[0].severity == Severity.HIGH


def test_sc012_skipped_when_live() -> None:
    assert signal_yanked_or_deprecated(_snap(yanked=False)) == []


# ── Manifest parser ─────────────────────────────────────────────────


def test_parse_requirements_txt(tmp_path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# a comment\n"
        "requests==2.31.0\n"
        "click>=8.0\n"
        "rich\n"
        "Django[bcrypt]>=4.0\n"
        "-r other.txt\n"
        "\n",
        encoding="utf-8",
    )
    entries = parse_requirements_txt(req)
    names = [e.name for e in entries]
    assert names == ["requests", "click", "rich", "Django"]
    by_name = {e.name: e.constraint for e in entries}
    assert by_name["requests"] == "==2.31.0"
    assert by_name["click"] == ">=8.0"
    assert by_name["rich"] == ""
    assert by_name["Django"] == ">=4.0"


# ── End-to-end scan() ───────────────────────────────────────────────


def _requests_doc(version: str = "2.31.0", *, hour: int = 14, maintainer: str = "Kenneth Reitz", deps: list[str] | None = None) -> dict:
    upload = f"2023-05-22T{hour:02d}:30:00Z"
    return {
        "info": {
            "name": "requests",
            "version": version,
            "author": maintainer,
            "author_email": "me@example.com",
            "project_urls": {"Source": "https://github.com/psf/requests"},
            "requires_dist": deps or ["urllib3 (<3,>=1.21.1)", "certifi (>=2017.4.17)"],
        },
        "releases": {
            version: [{
                "packagetype": "sdist",
                "url": f"https://files.pythonhosted.org/packages/z/requests-{version}.tar.gz",
                "upload_time_iso_8601": upload,
            }],
        },
    }


def test_scan_init_records_snapshots_without_findings(install_fake_fetcher, store: Store) -> None:
    install_fake_fetcher({
        PYPI_JSON_URL.format(package="requests"): _requests_doc(),
    })
    entries = [ManifestEntry(name="requests", constraint="==2.31.0", source_line="requests==2.31.0")]
    result = scan(store, entries, mode="init", now=NOW)
    assert result.findings == []
    assert result.snapshots_recorded == 1
    assert store.latest_snapshot("pypi", "requests") is not None


def test_scan_emits_findings_after_init(install_fake_fetcher, store: Store) -> None:
    # 1) Init against a clean maintainer list.
    install_fake_fetcher({
        PYPI_JSON_URL.format(package="requests"): _requests_doc(),
    })
    entries = [ManifestEntry(name="requests", constraint="==2.31.0", source_line="requests==2.31.0")]
    scan(store, entries, mode="init", now=NOW)

    # 2) Scan with a new maintainer and an extra dep.
    new_doc = _requests_doc(
        version="2.32.0",
        maintainer="mallory",  # new maintainer
        deps=["urllib3 (<3,>=1.21.1)", "certifi (>=2017.4.17)", "evilmod (==1.0)"],
    )
    install_fake_fetcher({
        PYPI_JSON_URL.format(package="requests"): new_doc,
    })
    result = scan(store, entries, mode="scan", now=NOW + timedelta(days=1))
    check_ids = {f.check_id for f in result.findings}
    # New maintainer (SC-001 at MEDIUM since no GitHub probe) + new dep (SC-005).
    assert "SC-001" in check_ids
    assert "SC-005" in check_ids


def test_scan_missing_package_is_tracked_not_fatal(install_fake_fetcher, store: Store) -> None:
    import urllib.error

    from pipeline_watch.providers import pypi

    def fetch(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
    pypi.set_fetcher(fetch)
    try:
        entries = [ManifestEntry(name="does-not-exist", constraint="", source_line="does-not-exist")]
        result = scan(store, entries, mode="scan", now=NOW)
        assert result.packages_missing_from_registry == ["does-not-exist"]
        assert result.findings == []
    finally:
        pypi.set_fetcher(None)


def test_scan_invalid_mode() -> None:
    store = Store.__new__(Store)  # not actually used; mode is checked first
    with pytest.raises(ValueError):
        scan(store, [], mode="wrong")


def test_scan_invalid_ecosystem() -> None:
    store = Store.__new__(Store)
    with pytest.raises(ValueError):
        scan(store, [], ecosystem="cargo")


def test_sc006_uses_stored_manifest_constraint(install_fake_fetcher, store: Store) -> None:
    """SC-006 reaches across runs by reading the stored manifest constraint."""
    install_fake_fetcher({PYPI_JSON_URL.format(package="requests"): _requests_doc()})
    pinned = [ManifestEntry(name="requests", constraint="==2.31.0", source_line="requests==2.31.0")]
    scan(store, pinned, mode="init", now=NOW)

    install_fake_fetcher({
        PYPI_JSON_URL.format(package="requests"): _requests_doc(version="2.32.0"),
    })
    loosened = [ManifestEntry(name="requests", constraint=">=2.31", source_line="requests>=2.31")]
    result = scan(store, loosened, mode="scan", now=NOW + timedelta(days=1))
    assert any(f.check_id == "SC-006" for f in result.findings)
