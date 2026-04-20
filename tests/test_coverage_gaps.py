"""Targeted tests for remaining coverage gaps.

Each test here fills a specific uncovered branch identified from the
coverage report — grouped by the module they exercise rather than by
behavioural theme.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from pipeline_watch.baseline.store import (
    PackageSnapshot,
    Store,
    default_baseline_path,
)
from pipeline_watch.cli import (
    _github_probe_factory,
    _npm_probe_factory,
    _pypi_probe_factory,
    cli,
)
from pipeline_watch.detectors import supply_chain as sc
from pipeline_watch.providers import github as _github
from pipeline_watch.providers import npm as _npm
from pipeline_watch.providers import pypi as _pypi

# ── Store ───────────────────────────────────────────────────────────


def _snap(**overrides) -> PackageSnapshot:
    base = dict(
        ecosystem="pypi", package="requests", version="2.31.0",
        maintainers=[], dependencies={},
        recorded_at="2026-04-20T00:00:00Z",
    )
    base.update(overrides)
    return PackageSnapshot(**base)


def test_store_snapshot_to_dict_roundtrips_values(store) -> None:
    snap = _snap(manifest_constraint="==2.31.0", release_uploaded_at="2023-05-22T14:30:00Z")
    d = snap.to_dict()
    assert d["package"] == "requests"
    assert d["manifest_constraint"] == "==2.31.0"
    assert d["release_uploaded_at"] == "2023-05-22T14:30:00Z"


def test_store_from_row_tolerates_missing_columns(store) -> None:
    """A v1-era row (no manifest_constraint/release_uploaded_at/yanked) still loads."""
    # Insert via a raw write that omits the v2 columns.
    store.conn.execute(
        "INSERT INTO package_snapshots "
        "(ecosystem, package, version, maintainers, "
        " has_install_script, dependencies, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?);",
        ("pypi", "legacy", "1.0", "[]", 0, "{}", "2020-01-01T00:00:00Z"),
    )
    store.conn.commit()
    snap = store.latest_snapshot("pypi", "legacy")
    assert snap is not None
    assert snap.manifest_constraint == ""
    assert snap.release_uploaded_at == ""
    assert snap.yanked is False


def test_store_open_with_none_uses_default(monkeypatch, tmp_path) -> None:
    # Force Path.home() and cwd to tmp_path so the default lands there.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    s = Store.open(None)
    try:
        assert s.schema_version() == 2
        # default_baseline_path has created the file.
        assert default_baseline_path(cwd=tmp_path).exists()
    finally:
        s.close()


def test_store_transaction_commits_on_success(store) -> None:
    with store.transaction():
        store.conn.execute(
            "INSERT INTO package_snapshots "
            "(ecosystem, package, version, maintainers, dependencies, recorded_at) "
            "VALUES ('pypi', 'committed', '1.0', '[]', '{}', '2026-04-20T00:00:00Z');"
        )
    # Transaction committed — row is visible afterwards.
    row = store.conn.execute(
        "SELECT package FROM package_snapshots WHERE package = 'committed';"
    ).fetchone()
    assert row is not None


def test_store_snapshots_for_respects_limit(store) -> None:
    for i, version in enumerate(("1.0", "2.0", "3.0", "4.0")):
        store.record_snapshot(_snap(
            version=version,
            recorded_at=f"2026-04-{20 + i:02d}T00:00:00Z",
        ))
    rows = store.snapshots_for("pypi", "requests", limit=2)
    assert len(rows) == 2
    # Newest first → 4.0, 3.0.
    assert rows[0].version == "4.0"
    assert rows[1].version == "3.0"


def test_store_reset_scope_job_deletes_pipeline_runs(store) -> None:
    store.record_run(
        provider="github-actions", repo="myorg/myrepo", job_name="build",
        triggered_at="2026-04-20T09:00:00Z",
    )
    store.upsert_stat(
        "job:myorg/myrepo:build", "duration_seconds",
        mean=42.0, stddev=1.0, sample_count=5,
        updated_at="2026-04-20T09:00:00Z",
    )
    deleted = store.reset_scope("job:myorg/myrepo:build")
    assert deleted == 1
    assert store.get_stat("job:myorg/myrepo:build", "duration_seconds") is None


def test_store_reset_scope_org_deletes_audit_events(store) -> None:
    store.record_audit_event(
        platform="github", org="acme", event_type="deploy_key.added",
        actor="alice", recorded_at="2026-04-20T09:00:00Z",
    )
    store.record_audit_event(
        platform="github", org="acme", event_type="team.change",
        actor="bob", recorded_at="2026-04-20T10:00:00Z",
    )
    store.upsert_stat(
        "org:acme", "events_per_day",
        mean=2.0, stddev=0.0, sample_count=1,
        updated_at="2026-04-20T10:00:00Z",
    )
    deleted = store.reset_scope("org:acme")
    assert deleted == 2
    assert store.get_stat("org:acme", "events_per_day") is None


def test_store_explicit_memory_path_does_not_call_mkdir() -> None:
    s = Store.open(":memory:")
    try:
        assert s.schema_version() == 2
    finally:
        s.close()


# ── supply_chain: orchestration edges ───────────────────────────────


def test_parse_manifest_rejects_unsupported_ecosystem(tmp_path) -> None:
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported ecosystem"):
        sc.parse_manifest(manifest, "maven")


def test_parse_requirements_skips_pip_directives_and_blank_lines(tmp_path) -> None:
    manifest = tmp_path / "requirements.txt"
    manifest.write_text(
        "\n"
        "# a comment\n"
        "-r other.txt\n"
        "--find-links /tmp\n"
        "requests==2.31.0\n"
        "# trailing\n",
        encoding="utf-8",
    )
    entries = sc.parse_requirements_txt(manifest)
    assert [e.name for e in entries] == ["requests"]


def test_parse_requirements_skips_malformed_lines(tmp_path) -> None:
    manifest = tmp_path / "requirements.txt"
    # '=' is not a valid op, no name captured — the regex skips it.
    manifest.write_text("==1.0\nrequests==2.31.0\n", encoding="utf-8")
    entries = sc.parse_requirements_txt(manifest)
    assert [e.name for e in entries] == ["requests"]


def test_signal_new_maintainer_downgrades_when_github_errors() -> None:
    """GitHubError on the commit probe → severity MEDIUM (has_commits stays None)."""
    prev = _snap(maintainers=[{"name": "alice"}])
    current = _snap(maintainers=[{"name": "alice"}, {"name": "mallory"}])

    def raising_probe(owner: str, repo: str):  # noqa: ARG001
        raise _github.GitHubError("rate limited")

    findings = sc.signal_new_maintainer(
        prev, current,
        github_probe=raising_probe,
        source_repo="https://github.com/psf/requests",
    )
    assert len(findings) == 1
    assert findings[0].severity.value == "MEDIUM"
    assert findings[0].evidence["has_commits_in_source_repo"] is None


def test_signal_release_without_tag_swallows_github_error() -> None:
    def raising_probe(owner: str, repo: str):  # noqa: ARG001
        raise _github.GitHubError("rate limited")

    findings = sc.signal_release_without_tag(
        _snap(version="2.32.0"),
        github_probe=raising_probe,
        source_repo="https://github.com/psf/requests",
    )
    # Error → no finding.
    assert findings == []


def test_signal_release_without_tag_skipped_when_no_tags() -> None:
    def empty_probe(owner: str, repo: str):  # noqa: ARG001
        return True, []

    findings = sc.signal_release_without_tag(
        _snap(version="2.32.0"),
        github_probe=empty_probe,
        source_repo="https://github.com/psf/requests",
    )
    assert findings == []


def test_signal_release_without_tag_skipped_when_repo_unparseable() -> None:
    def probe(owner: str, repo: str):  # noqa: ARG001
        return True, ["v1.0"]

    findings = sc.signal_release_without_tag(
        _snap(version="2.32.0"),
        github_probe=probe,
        source_repo="https://example.com/not-a-repo",
    )
    assert findings == []


def test_signal_cross_ecosystem_swallows_npm_error() -> None:
    def bad_probe(name: str):  # noqa: ARG001
        raise _npm.NpmError("down")

    findings = sc.signal_cross_ecosystem(
        sc.ManifestEntry("abc", "", "abc"),
        ecosystem="pypi",
        npm_probe=bad_probe, pypi_probe=None,
        now=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert findings == []


def test_signal_cross_ecosystem_swallows_pypi_error() -> None:
    def bad_probe(name: str):  # noqa: ARG001
        raise _pypi.PyPIError("down")

    findings = sc.signal_cross_ecosystem(
        sc.ManifestEntry("abc", "", "abc"),
        ecosystem="npm",
        npm_probe=None, pypi_probe=bad_probe,
        now=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert findings == []


def test_signal_cross_ecosystem_no_releases_returns_no_finding() -> None:
    def empty_pypi(name: str):  # noqa: ARG001
        return _pypi.PyPIPackage(
            name="abc", latest_version="", maintainers=[], releases=[],
            dependencies={}, project_urls={},
        )

    findings = sc.signal_cross_ecosystem(
        sc.ManifestEntry("abc", "", "abc"),
        ecosystem="npm",
        npm_probe=None, pypi_probe=empty_pypi,
        now=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert findings == []


def test_signal_cross_ecosystem_unparseable_timestamp_no_finding() -> None:
    def bogus_npm(name: str):  # noqa: ARG001
        return _npm.NpmPackageInfo(name="abc", created_iso="not-a-date")

    findings = sc.signal_cross_ecosystem(
        sc.ManifestEntry("abc", "", "abc"),
        ecosystem="pypi",
        npm_probe=bogus_npm, pypi_probe=None,
        now=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert findings == []


def test_signal_cross_ecosystem_pypi_without_any_upload_time() -> None:
    """PyPI package exists but every release has empty upload_time → no finding."""
    def pypi_probe(name: str):  # noqa: ARG001
        return _pypi.PyPIPackage(
            name="abc", latest_version="1.0", maintainers=[],
            releases=[_pypi.PyPIRelease(
                version="1.0", upload_time_iso="", sdist_url=None,
                has_install_script=False, install_script_hash=None,
            )],
            dependencies={}, project_urls={},
        )

    findings = sc.signal_cross_ecosystem(
        sc.ManifestEntry("abc", "", "abc"),
        ecosystem="npm",
        npm_probe=None, pypi_probe=pypi_probe,
        now=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert findings == []


def test_signal_dormant_revival_skipped_when_prev_time_unparseable() -> None:
    prev = _snap(release_uploaded_at="not-a-date")
    current = _snap(version="2.32.0", release_uploaded_at="2026-04-20T00:00:00Z")
    assert sc.signal_dormant_revival(prev, current) == []


def test_scan_invalid_mode_raises() -> None:
    # Hit the mode validator (line 716 region).
    s = Store(sqlite3.connect(":memory:"))
    try:
        with pytest.raises(ValueError, match="mode must be"):
            sc.scan(s, [], mode="bogus")
    finally:
        s.close()


def test_scan_invalid_ecosystem_raises() -> None:
    s = Store(sqlite3.connect(":memory:"))
    try:
        with pytest.raises(ValueError, match="ecosystem must be"):
            sc.scan(s, [], ecosystem="maven")
    finally:
        s.close()


def test_version_tuple_handles_prerelease_tail() -> None:
    """_version_tuple orders pre-release suffixes after base, unparseable parts last."""
    # Non-numeric component falls into the (1, 0, s) bucket at the tail.
    tup = sc._version_tuple("1.0.rc1")
    # All three parts present.
    assert len(tup) == 3


def test_parse_iso_invalid_returns_none() -> None:
    assert sc._parse_iso("") is None
    assert sc._parse_iso("not-a-real-date") is None


def test_source_repo_helper_handles_objects_without_method() -> None:
    class Dummy:
        pass
    assert sc._source_repo(Dummy()) is None
    assert sc._source_repo(None) is None


# ── cli helpers ─────────────────────────────────────────────────────


def test_probe_factories_return_none_when_disabled() -> None:
    assert _github_probe_factory(False) is None
    assert _npm_probe_factory(False) is None
    assert _pypi_probe_factory(False) is None


def test_cli_verbose_with_quiet_is_still_quiet(tmp_path: Path) -> None:
    """--quiet wins over --verbose — debug output should be suppressed."""
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    _pypi.set_fetcher(lambda url, timeout: json.dumps({  # noqa: ARG005
        "info": {
            "name": "requests", "version": "2.31.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {"2.31.0": [{
            "packagetype": "sdist",
            "url": "https://example.invalid/r.tar.gz",
            "upload_time_iso_8601": "2023-05-22T14:30:00Z",
        }]},
    }).encode())
    try:
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "--verbose", "--quiet",
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        assert r.exit_code == 0
        stderr = r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else ""
        assert "[debug]" not in (r.output + stderr)
    finally:
        _pypi.set_fetcher(None)


def test_cli_scan_deps_invalid_ecosystem_rejected(tmp_path: Path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "scan", "deps", "--manifest", str(manifest),
        "--ecosystem", "maven",
    ], catch_exceptions=False)
    assert r.exit_code == 2


def test_cli_baseline_init_parse_error_reported(tmp_path: Path) -> None:
    """A package.json that isn't valid JSON surfaces as a UsageError, not a crash."""
    runner = CliRunner()
    manifest = tmp_path / "package.json"
    manifest.write_text("{not valid json", encoding="utf-8")
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "init",
        "--manifest", str(manifest),
        "--ecosystem", "npm",
    ], catch_exceptions=True)
    # JSON decode raises inside parse_manifest → surfaced through the
    # scanner failure branch as exit 2.
    assert r.exit_code == 2


def test_cli_default_output_json_flag_writes_findings_file(tmp_path: Path) -> None:
    """`scan deps --output json` without --output-file echoes JSON to stdout."""
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    doc = {
        "info": {
            "name": "requests", "version": "2.31.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {"2.31.0": [{
            "packagetype": "sdist",
            "url": "https://example.invalid/r.tar.gz",
            "upload_time_iso_8601": "2023-05-22T14:30:00Z",
        }]},
    }
    _pypi.set_fetcher(lambda url, timeout: json.dumps(doc).encode())  # noqa: ARG005
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json",
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        payload = json.loads(r.stdout)
        assert payload["tool"] == "pipeline-watch"
    finally:
        _pypi.set_fetcher(None)


def test_github_probe_factory_returns_tags_via_github_module() -> None:
    """The inner probe delegates to github.list_tags."""
    _github.set_fetcher(lambda url, timeout: json.dumps(  # noqa: ARG005
        [{"name": "v1.0"}, {"name": "v2.0"}]
    ).encode())
    try:
        probe = _github_probe_factory(True)
        assert probe is not None
        has_commits, tags = probe("org", "repo")
        # Factory answers has_commits=False (detector does its own lookup).
        assert has_commits is False
        assert tags == ["v1.0", "v2.0"]
    finally:
        _github.set_fetcher(None)


def test_npm_probe_factory_delegates_to_package_info() -> None:
    _npm.set_fetcher(lambda url, timeout: json.dumps({  # noqa: ARG005
        "name": "left-pad", "time": {"created": "2014-03-23T15:10:00.000Z"},
    }).encode())
    try:
        probe = _npm_probe_factory(True)
        assert probe is not None
        info = probe("left-pad")
        assert info is not None
        assert info.created_date == "2014-03-23"
    finally:
        _npm.set_fetcher(None)


def test_pypi_probe_factory_skips_install_script_hash() -> None:
    """Cross-ecosystem probe must not download sdists — perf + correctness."""
    calls: list[str] = []

    def fetch(url: str, timeout: float):  # noqa: ARG001
        calls.append(url)
        return json.dumps({
            "info": {
                "name": "abc", "version": "1.0",
                "author": "x", "author_email": "x@x",
                "project_urls": {}, "requires_dist": [],
            },
            "releases": {"1.0": [{
                "packagetype": "sdist",
                "url": "https://example.invalid/abc.tar.gz",
                "upload_time_iso_8601": "2026-04-01T00:00:00Z",
            }]},
        }).encode()
    _pypi.set_fetcher(fetch)
    try:
        probe = _pypi_probe_factory(True)
        assert probe is not None
        pkg = probe("abc")
        assert pkg is not None
        # Exactly one call — the JSON endpoint. No sdist download.
        assert len(calls) == 1
        assert "pypi.org" in calls[0]
    finally:
        _pypi.set_fetcher(None)


def test_cli_scan_deps_parse_error_surfaces_as_exit_two(tmp_path: Path) -> None:
    """A malformed package.json during `scan deps` is caught, not a crash."""
    runner = CliRunner()
    manifest = tmp_path / "package.json"
    manifest.write_text("{broken", encoding="utf-8")
    # Init a baseline first so scan mode engages; but the parse will fail before that.
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "scan", "deps", "--manifest", str(manifest),
        "--ecosystem", "npm",
    ], catch_exceptions=False)
    assert r.exit_code == 2


def test_cli_scan_deps_registry_failure_during_scan_exits_two(tmp_path: Path) -> None:
    """Fetcher that blows up *during scan* (after a healthy init) exits 2."""
    import urllib.error

    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    doc = {
        "info": {
            "name": "requests", "version": "2.31.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {"2.31.0": [{
            "packagetype": "sdist",
            "url": "https://example.invalid/r.tar.gz",
            "upload_time_iso_8601": "2023-05-22T14:30:00Z",
        }]},
    }
    _pypi.set_fetcher(lambda url, timeout: json.dumps(doc).encode())  # noqa: ARG005
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        # Now swap fetcher to blow up on the scan request.
        def boom(url: str, timeout: float):  # noqa: ARG001
            raise urllib.error.HTTPError(url, 500, "Server", hdrs=None, fp=None)
        _pypi.set_fetcher(boom)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 2
        combined = r.output + (r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else "")
        assert "scan failed" in combined
    finally:
        _pypi.set_fetcher(None)


def test_cli_scan_deps_reports_missing_packages(tmp_path: Path) -> None:
    """scan after init where a pkg goes 404 — missing-packages branch fires."""
    import urllib.error

    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\nvanished==1.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    requests_doc = {
        "info": {
            "name": "requests", "version": "2.31.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {"2.31.0": [{
            "packagetype": "sdist",
            "url": "https://example.invalid/r.tar.gz",
            "upload_time_iso_8601": "2023-05-22T14:30:00Z",
        }]},
    }

    def fetch(url: str, timeout: float):  # noqa: ARG001
        if "vanished" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
        if "pypi.org" in url:
            return json.dumps(requests_doc).encode()
        return b""
    _pypi.set_fetcher(fetch)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        combined = r.output + (r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else "")
        assert "vanished" in combined
    finally:
        _pypi.set_fetcher(None)


def test_cli_baseline_show_without_fetcher_init_then_show_package(tmp_path: Path) -> None:
    """Initialize then show snapshot details — exercises _render_snapshot."""
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    doc = {
        "info": {
            "name": "requests", "version": "2.31.0",
            "author": "Kenneth Reitz", "author_email": "k@example.com",
            "maintainer": "Alt", "maintainer_email": "a@example.com",
            "project_urls": {"Source": "https://github.com/psf/requests"},
            "requires_dist": ["urllib3 (>=1.21.1)"],
        },
        "releases": {"2.31.0": [{
            "packagetype": "sdist",
            "url": "https://example.invalid/r.tar.gz",
            "upload_time_iso_8601": "2023-05-22T14:30:00Z",
        }]},
    }

    def fetch(url: str, timeout: float):  # noqa: ARG001
        if "pypi.org" in url:
            return json.dumps(doc).encode()
        return b""
    _pypi.set_fetcher(fetch)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "show", "--package", "requests",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        assert "2.31.0" in r.output
        assert "Kenneth Reitz" in r.output
        # Dependencies rendered too.
        assert "urllib3" in r.output
    finally:
        _pypi.set_fetcher(None)
