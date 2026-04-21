"""Additional CLI coverage — stats, output modes, verbose/quiet, errors, npm, etc."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pipeline_watch.cli import cli
from pipeline_watch.providers import npm as _npm
from pipeline_watch.providers import pypi as _pypi
from pipeline_watch.providers.pypi import PYPI_JSON_URL


def _pypi_doc(version: str = "2.31.0", *, upload_iso: str = "2023-05-22T14:30:00Z") -> dict:
    return {
        "info": {
            "name": "requests", "version": version,
            "author": "Kenneth Reitz", "author_email": "me@example.com",
            "project_urls": {},
            "requires_dist": [],
        },
        "releases": {
            version: [{
                "packagetype": "sdist",
                "url": f"https://files.pythonhosted.org/packages/z/requests-{version}.tar.gz",
                "upload_time_iso_8601": upload_iso,
            }],
        },
    }


def _route_pypi(routes: dict[str, dict]) -> None:
    encoded = {url: json.dumps(body).encode("utf-8") for url, body in routes.items()}

    def fetch(url: str, timeout: float):  # noqa: ARG001
        if url not in encoded:
            if "files.pythonhosted.org" in url:
                return b""
            raise AssertionError(f"CLI test saw unexpected URL: {url}")
        return encoded[url]
    _pypi.set_fetcher(fetch)


def _install_runner(tmp_path: Path) -> tuple[CliRunner, Path, Path]:
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"
    _route_pypi({PYPI_JSON_URL.format(package="requests"): _pypi_doc()})
    _npm.set_fetcher(
        lambda url, timeout: (_ for _ in ()).throw(AssertionError("npm not expected"))
    )
    return runner, manifest, baseline_db


def test_help_lists_all_subcommands() -> None:
    runner = CliRunner()
    r = runner.invoke(cli, ["--help"], catch_exceptions=False)
    assert r.exit_code == 0
    for expected in ("baseline", "scan"):
        assert expected in r.output


def test_baseline_help() -> None:
    runner = CliRunner()
    r = runner.invoke(cli, ["baseline", "--help"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "init" in r.output
    assert "reset" in r.output
    assert "show" in r.output
    assert "stats" in r.output


def test_version_flag() -> None:
    runner = CliRunner()
    r = runner.invoke(cli, ["--version"], catch_exceptions=False)
    assert r.exit_code == 0
    assert "pipeline_watch" in r.output


def test_baseline_show_all_lists_packages(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "show",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        assert "requests" in r.output
        assert "2.31.0" in r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_baseline_show_all_empty_exits_three(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "show",
    ], catch_exceptions=False)
    assert r.exit_code == 3


def test_baseline_stats_empty_says_so(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "stats",
    ], catch_exceptions=False)
    assert r.exit_code == 0
    # "no precomputed stats" hint goes to stderr; CliRunner surfaces it in .output by default.
    combined = r.output + (r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else "")
    assert "no precomputed stats" in combined


def test_baseline_stats_shows_rows_after_scan(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        # scan run triggers refresh_package_hour_stats
        out = tmp_path / "findings.json"
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--ecosystem", "pypi",
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "stats",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        assert "release_hour" in r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_baseline_reset_rejects_bad_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "reset", "--scope", "bogus:something",
    ], catch_exceptions=False)
    assert r.exit_code == 2


def test_scan_deps_missing_manifest_exits_two(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "scan", "deps", "--manifest", str(tmp_path / "nope.txt"),
    ], catch_exceptions=False)
    assert r.exit_code == 2


def test_scan_deps_empty_baseline_runs_as_init(tmp_path: Path) -> None:
    """With an empty baseline, scan falls into init-mode rather than crashing."""
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    try:
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["findings"] == []
        assert payload["score"]["grade"] == "A"
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_scan_deps_verbose_emits_debug(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "--verbose",
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        assert r.exit_code == 0
        combined = r.output + (r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else "")
        assert "[debug]" in combined
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_scan_deps_quiet_suppresses_stderr(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "--quiet",
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        # With --quiet and a file output, stdout should be empty.
        assert r.output.strip() == ""
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_scan_deps_output_both_emits_panel_and_json(tmp_path: Path) -> None:
    """'both' sends the Rich panel to stderr and the JSON envelope to stdout."""
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "both",
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        # stdout carries the JSON envelope; stderr carries the Rich panel + gate line.
        assert r.stdout.strip().startswith("{")
        payload = json.loads(r.stdout)
        assert payload["tool"] == "pipeline-watch"
        assert "pipeline-watch" in r.stderr
        assert "[gate] PASS" in r.stderr
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_scan_deps_fail_on_low_escalates_any_finding(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    try:
        # Init with original maintainer.
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        # Loosen pin → SC-006 LOW fires on rescan.
        manifest.write_text("requests>=2.31.0\n", encoding="utf-8")
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--fail-on", "LOW",
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert any(f["check_id"] == "SC-006" for f in payload["findings"])
        # Gate at LOW → a LOW finding fails the gate.
        assert r.exit_code == 1
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_scan_all_writes_default_findings_file(tmp_path: Path, monkeypatch) -> None:
    # scan_all enables --no-cross-ecosystem=False, so the npm probe must
    # answer (not raise). A 404 stub keeps SC-008 from firing without
    # breaking the scan.
    import urllib.error

    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"
    _route_pypi({PYPI_JSON_URL.format(package="requests"): _pypi_doc()})

    def npm_404(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
    _npm.set_fetcher(npm_404)
    monkeypatch.chdir(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "all", "--manifest", str(manifest),
        ], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        assert (tmp_path / "findings.json").exists()
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_scan_deps_registry_error_exits_two(tmp_path: Path) -> None:
    """A registry error during scan exits 2, not 1."""
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    # Fetcher that blows up with a 500 should surface as PyPIError → exit 2.
    import urllib.error

    def boom(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 500, "Internal Error", hdrs=None, fp=None)

    _pypi.set_fetcher(boom)
    try:
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        assert r.exit_code == 2
        combined = r.output + (r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else "")
        assert "baseline init failed" in combined or "PyPIError" in combined
    finally:
        _pypi.set_fetcher(None)


def test_scan_deps_unknown_package_is_tracked_not_fatal(tmp_path: Path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("ghostlib==1.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    import urllib.error

    def not_found(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    _pypi.set_fetcher(not_found)
    try:
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        assert r.exit_code == 0
        combined = r.output + (r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else "")
        assert "ghostlib" in combined
    finally:
        _pypi.set_fetcher(None)


def test_init_empty_manifest_notes_nothing_recorded(tmp_path: Path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("# just a comment\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"
    r = runner.invoke(cli, [
        "--baseline-db", str(baseline_db),
        "baseline", "init", "--manifest", str(manifest),
    ], catch_exceptions=False)
    assert r.exit_code == 0
    combined = r.output + (r.stderr_bytes.decode(errors="replace") if r.stderr_bytes else "")
    assert "no packages" in combined


def test_init_invalid_ecosystem_rejected(tmp_path: Path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "init", "--manifest", str(manifest),
        "--ecosystem", "maven",
    ], catch_exceptions=False)
    # Click's Choice rejects invalid — usage error, exit 2.
    assert r.exit_code == 2


def test_npm_scan_init_and_diff(tmp_path: Path) -> None:
    runner = CliRunner()
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(json.dumps({
        "name": "my-app", "version": "1.0.0",
        "dependencies": {"left-pad": "1.3.0"},
    }), encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    def _npm_doc(version: str, *, scripts: dict | None = None) -> dict:
        return {
            "name": "left-pad",
            "dist-tags": {"latest": version},
            "maintainers": [{"name": "camwest"}],
            "versions": {
                version: {
                    "author": {"name": "camwest"},
                    "dependencies": {},
                    "scripts": scripts or {},
                    "repository": {"url": "git+https://github.com/stevemao/left-pad.git"},
                },
            },
            "time": {version: "2016-01-01T00:00:00.000Z"},
        }

    # Disable PyPI cross-check entirely.
    _pypi.set_fetcher(
        lambda url, timeout: (_ for _ in ()).throw(AssertionError("pypi not expected"))
    )

    try:
        _npm.set_fetcher(
            lambda url, timeout: json.dumps(_npm_doc("1.3.0")).encode(),  # noqa: ARG005
        )
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init",
            "--manifest", str(pkg_json),
            "--ecosystem", "npm",
        ], catch_exceptions=False)
        assert r.exit_code == 0

        # Scan with an install-script hook added → SC-004 HIGH fires.
        _npm.set_fetcher(
            lambda url, timeout: json.dumps(  # noqa: ARG005
                _npm_doc("1.3.0", scripts={"postinstall": "node ./evil.js"})
            ).encode(),
        )
        out = tmp_path / "findings.json"
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps",
            "--manifest", str(pkg_json),
            "--ecosystem", "npm",
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 1  # HIGH trips default --fail-on
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert any(f["check_id"] == "SC-004" for f in payload["findings"])
    finally:
        _npm.set_fetcher(None)
        _pypi.set_fetcher(None)


# ── Usability: ecosystem inference ──────────────────────────────────


@pytest.mark.parametrize("filename,ecosystem,content", [
    # requirements-style → pypi
    ("requirements.txt", "pypi", "requests==2.31.0\n"),
    ("requirements-dev.txt", "pypi", "requests==2.31.0\n"),
    # package.json → npm
    ("package.json", "npm", '{"dependencies": {}}'),
])
def test_cli_infers_ecosystem_from_manifest_filename(
    tmp_path: Path, filename: str, ecosystem: str, content: str,
) -> None:
    """Ecosystem is picked from the filename; explicit flag still wins elsewhere."""
    runner = CliRunner()
    manifest = tmp_path / filename
    manifest.write_text(content, encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    if ecosystem == "pypi":
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _pypi_doc()})
    else:
        _npm.set_fetcher(lambda url, timeout: b"{}")

    r = runner.invoke(cli, [
        "--baseline-db", str(baseline_db),
        "baseline", "init",
        "--manifest", str(manifest),
    ], catch_exceptions=False)
    assert r.exit_code == 0, r.output


def test_cli_unknown_manifest_name_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "deps.yaml"
    manifest.write_text("", encoding="utf-8")
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "init",
        "--manifest", str(manifest),
    ], catch_exceptions=False)
    assert r.exit_code == 2  # click.UsageError
    assert "could not infer --ecosystem" in r.output


# ── Usability: signals subcommand ───────────────────────────────────


def test_cli_signals_terminal_lists_every_check() -> None:
    runner = CliRunner()
    r = runner.invoke(cli, ["signals"], catch_exceptions=False)
    assert r.exit_code == 0
    # Every SC-XXX ID should appear in the listing.
    from pipeline_watch.detectors.supply_chain import SIGNAL_CATALOGUE
    for sc_id in SIGNAL_CATALOGUE:
        assert sc_id in r.output, f"{sc_id} missing from 'signals' output"


def test_cli_signals_json_is_parseable() -> None:
    runner = CliRunner()
    r = runner.invoke(cli, ["signals", "--output", "json"], catch_exceptions=False)
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["schema_version"] == "1.0"
    ids = {s["id"] for s in payload["signals"]}
    from pipeline_watch.detectors.supply_chain import SIGNAL_CATALOGUE
    assert ids == set(SIGNAL_CATALOGUE)
    # Each entry has the required keys.
    first = payload["signals"][0]
    assert set(first) == {"id", "slug", "severity", "description"}


# ── Usability: --skip filter ────────────────────────────────────────


def _doc_with_new_maintainer(version: str) -> dict:
    return {
        "info": {
            "name": "requests", "version": version,
            "author": "Kenneth Reitz", "author_email": "me@example.com",
            "maintainer": "mallory", "maintainer_email": "mallory@example.com",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {version: [{
            "packagetype": "sdist",
            "url": f"https://files.pythonhosted.org/packages/z/requests-{version}.tar.gz",
            "upload_time_iso_8601": "2023-05-22T14:30:00Z",
        }]},
    }


def test_cli_skip_suppresses_named_check(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        # Next scan: new maintainer triggers SC-001 (MEDIUM with --no-github).
        _route_pypi({PYPI_JSON_URL.format(package="requests"):
                     _doc_with_new_maintainer("2.32.0")})

        # Without --skip, SC-001 appears.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        ids = {f["check_id"] for f in json.loads(out.read_text())["findings"]}
        assert "SC-001" in ids

        # With --skip SC-001, the finding is suppressed.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
            "--skip", "SC-001",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        ids = {f["check_id"] for f in json.loads(out.read_text())["findings"]}
        assert "SC-001" not in ids
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_skip_unknown_id_errors(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--no-github", "--no-cross-ecosystem",
            "--skip", "SC-999",
        ], catch_exceptions=False)
        assert r.exit_code == 2
        assert "SC-999" in r.output
        assert "unknown check ID" in r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


# ── Usability: suppression file ─────────────────────────────────────


def test_cli_suppression_file_silences_findings(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    ignore = tmp_path / "ignore.json"
    ignore.write_text(json.dumps({
        "suppressions": [
            {"check_id": "SC-001", "reason": "known ownership reshuffle 2026-04"},
        ],
    }), encoding="utf-8")
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _doc_with_new_maintainer("2.32.0")})

        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
            "--ignore-file", str(ignore),
            "--fail-on", "MEDIUM",
        ], catch_exceptions=False)
        # SC-001 suppressed → MEDIUM gate passes.
        assert r.exit_code == 0
        ids = {f["check_id"] for f in json.loads(out.read_text())["findings"]}
        assert "SC-001" not in ids
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_suppression_missing_reason_errors(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    ignore = tmp_path / "ignore.json"
    ignore.write_text(json.dumps({
        "suppressions": [{"check_id": "SC-001"}],  # missing reason
    }), encoding="utf-8")
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--no-github", "--no-cross-ecosystem",
            "--ignore-file", str(ignore),
        ], catch_exceptions=False)
        assert r.exit_code == 2
        assert "'reason' is required" in r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_no_ignore_bypasses_suppression_file(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    # Place ignore.json in the default location (next to baseline_db).
    ignore = baseline_db.parent / "ignore.json"
    ignore.write_text(json.dumps({
        "suppressions": [{"check_id": "SC-001", "reason": "default policy"}],
    }), encoding="utf-8")
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _doc_with_new_maintainer("2.32.0")})

        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
            "--no-ignore",
        ], catch_exceptions=False)
        # Default policy bypassed → SC-001 survives.
        ids = {f["check_id"] for f in json.loads(out.read_text())["findings"]}
        assert "SC-001" in ids
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


# ── Usability: SARIF output ─────────────────────────────────────────


def test_cli_sarif_output_has_correct_shape(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.sarif"
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _doc_with_new_maintainer("2.32.0")})

        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "sarif", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["version"] == "2.1.0"
        assert payload["runs"][0]["tool"]["driver"]["name"] == "pipeline-watch"
        rule_ids = {r["id"] for r in payload["runs"][0]["tool"]["driver"]["rules"]}
        assert "SC-001" in rule_ids
        # Result level reflects SC-001 MEDIUM (no-github) → warning.
        results = payload["runs"][0]["results"]
        sc001 = next(r for r in results if r["ruleId"] == "SC-001")
        assert sc001["level"] == "warning"
        assert sc001["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == str(manifest)
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


# ── Usability: baseline diff ────────────────────────────────────────


def test_cli_baseline_diff_shows_version_change(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        # Registry now advertises a newer version.
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _pypi_doc(version="2.32.0")})

        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "diff", "--manifest", str(manifest),
        ], catch_exceptions=False)
        assert r.exit_code == 0
        # Both old and new version should appear in the diff rendering.
        assert "2.31.0" in r.output
        assert "2.32.0" in r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_baseline_diff_reports_no_changes(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "diff", "--manifest", str(manifest),
        ], catch_exceptions=False)
        assert r.exit_code == 0
        assert "No differences" in r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


# ── HTML output ─────────────────────────────────────────────────────


def test_cli_html_output_is_self_contained(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "report.html"
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _doc_with_new_maintainer("2.32.0")})

        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "html", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        text = out.read_text(encoding="utf-8")
        assert text.startswith("<!DOCTYPE html>"), (
            "HTML report must start with a valid doctype for browsers to "
            f"render in standards mode; got: {text[:80]!r}"
        )
        assert "pipeline-watch report" in text
        assert "SC-001" in text, (
            "SC-001 finding should be rendered in the HTML body"
        )
        # Strict isolation: the report must be self-contained.
        # No <script>, no remote stylesheets, no external asset refs.
        assert "<script" not in text, "report must not embed JavaScript"
        assert "src=" not in text, "report must not reference external assets"
        assert "href=" not in text, "report must not link external CSS"
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_output_file_dash_writes_to_stdout(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", "-",
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        # Stronger than "schema_version in output": pull out the JSON
        # object and validate the envelope shape. Mixing stderr noise
        # into r.output means we can't parse the whole thing, but we
        # can extract the top-level object by bracket-matching.
        start = r.output.index("{")
        depth = 0
        end = start
        for i, ch in enumerate(r.output[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        payload = json.loads(r.output[start:end])
        assert payload["schema_version"] == "1.0"
        assert payload["tool"] == "pipeline-watch"
        assert "findings" in payload and isinstance(payload["findings"], list)
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


# ── ingest command ──────────────────────────────────────────────────


def test_cli_ingest_merges_and_dedupes(tmp_path: Path) -> None:
    runner = CliRunner()
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    base_finding = {
        "tool": "pipeline-watch", "module": "supply-chain",
        "severity": "HIGH", "score": "D",
        "signal": "maintainer changed", "baseline": "prior set",
        "evidence": {"package": "requests"},
        "timestamp": "2026-04-20T00:00:00+00:00",
        "remediation": "confirm",
        "check_id": "SC-001",
    }
    a.write_text(json.dumps({"findings": [base_finding]}), encoding="utf-8")
    # Same finding in b (dedup), plus a distinct one.
    b.write_text(json.dumps({
        "findings": [
            base_finding,
            {**base_finding, "check_id": "SC-004",
             "signal": "install hash flipped"},
        ],
    }), encoding="utf-8")
    out = tmp_path / "merged.json"
    r = runner.invoke(cli, [
        "ingest", str(a), str(b),
        "--output", "json", "--output-file", str(out),
        "--fail-on", "CRITICAL",
    ], catch_exceptions=False)
    assert r.exit_code == 0
    payload = json.loads(out.read_text())
    ids = [f["check_id"] for f in payload["findings"]]
    assert sorted(ids) == ["SC-001", "SC-004"]


def test_cli_ingest_gates_on_severity(tmp_path: Path) -> None:
    runner = CliRunner()
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"findings": [{
        "tool": "pipeline-watch", "module": "supply-chain",
        "severity": "HIGH", "score": "D",
        "signal": "x", "baseline": "y",
        "evidence": {"package": "requests"},
        "timestamp": "2026-04-20T00:00:00+00:00",
        "remediation": "z",
        "check_id": "SC-001",
    }]}), encoding="utf-8")
    r = runner.invoke(cli, [
        "ingest", str(a), "--output", "json", "--output-file", "-",
        "--fail-on", "HIGH",
    ], catch_exceptions=False)
    assert r.exit_code == 1


def test_cli_ingest_rejects_malformed_json(tmp_path: Path) -> None:
    runner = CliRunner()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    r = runner.invoke(cli, ["ingest", str(bad)], catch_exceptions=False)
    assert r.exit_code == 2
    assert "not valid JSON" in r.output


# ── doctor command ──────────────────────────────────────────────────


def test_cli_doctor_reports_on_empty_baseline(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "doctor",
    ], catch_exceptions=False)
    assert r.exit_code == 0
    assert "pipeline-watch version" in r.output
    assert "baseline path" in r.output


def test_cli_doctor_reports_on_populated_baseline(tmp_path: Path) -> None:
    runner, manifest, baseline_db = _install_runner(tmp_path)
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "doctor",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        assert "schema version" in r.output
        assert "total packages" in r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


# ── Usability: --baseline-update flag ───────────────────────────────


def test_cli_scan_keeps_prior_snapshot_by_default(tmp_path: Path) -> None:
    """Without --baseline-update a finding re-flags on the next run."""
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _doc_with_new_maintainer("2.32.0")})

        for _ in range(2):
            r = runner.invoke(cli, [
                "--baseline-db", str(baseline_db),
                "scan", "deps", "--manifest", str(manifest),
                "--output", "json", "--output-file", str(out),
                "--no-github", "--no-cross-ecosystem",
            ], catch_exceptions=False)
            assert r.exit_code == 0
            ids = {f["check_id"] for f in json.loads(out.read_text())["findings"]}
            assert "SC-001" in ids, (
                "SC-001 should persist across scans without --baseline-update"
            )
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_baseline_update_accepts_new_state(tmp_path: Path) -> None:
    """--baseline-update records the new snapshot → same run next time is clean."""
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _doc_with_new_maintainer("2.32.0")})

        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
            "--baseline-update",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        # Second scan: mallory is now in the baseline, SC-001 quiet.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
        ], catch_exceptions=False)
        ids = {f["check_id"] for f in json.loads(out.read_text())["findings"]}
        assert "SC-001" not in ids
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_skip_changes_gate_result(tmp_path: Path) -> None:
    """--skip must also remove findings from the gate, not just the report."""
    runner, manifest, baseline_db = _install_runner(tmp_path)
    out = tmp_path / "findings.json"
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init", "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"):
                     _doc_with_new_maintainer("2.32.0")})

        # MEDIUM gate without skip: SC-001 fires → gate fails.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
            "--fail-on", "MEDIUM",
        ], catch_exceptions=False)
        assert r.exit_code == 1

        # Same gate with --skip SC-001: passes.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps", "--manifest", str(manifest),
            "--output", "json", "--output-file", str(out),
            "--no-github", "--no-cross-ecosystem",
            "--fail-on", "MEDIUM",
            "--skip", "SC-001",
        ], catch_exceptions=False)
        assert r.exit_code == 0
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)
