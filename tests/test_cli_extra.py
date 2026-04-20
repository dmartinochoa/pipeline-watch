"""Additional CLI coverage — stats, output modes, verbose/quiet, errors, npm, etc."""
from __future__ import annotations

import json
from pathlib import Path

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
