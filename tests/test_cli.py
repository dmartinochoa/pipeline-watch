"""End-to-end CLI tests — patch the registry fetchers, drive via Click's runner."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from pipeline_watch.cli import cli
from pipeline_watch.providers import npm as _npm
from pipeline_watch.providers import pypi as _pypi
from pipeline_watch.providers.pypi import PYPI_JSON_URL


def _pypi_doc(version: str = "2.31.0", *, maintainer: str = "Kenneth Reitz", deps: list[str] | None = None) -> dict:
    return {
        "info": {
            "name": "requests", "version": version,
            "author": maintainer, "author_email": "me@example.com",
            "project_urls": {"Source": "https://github.com/psf/requests"},
            "requires_dist": deps or ["urllib3 (<3,>=1.21.1)"],
        },
        "releases": {
            version: [{
                "packagetype": "sdist",
                "url": f"https://files.pythonhosted.org/packages/z/requests-{version}.tar.gz",
                "upload_time_iso_8601": "2023-05-22T14:30:00Z",
            }],
        },
    }


def _route_pypi(routes: dict[str, dict]):
    """Install a fetcher that serves fixture docs for the given PyPI URLs."""
    encoded = {url: json.dumps(body).encode("utf-8") for url, body in routes.items()}

    def fetch(url: str, timeout: float):  # noqa: ARG001
        if url not in encoded:
            # Sdist URLs trigger the install-script probe; returning empty
            # bytes makes the probe skip hashing (not a tar/zip).
            if "files.pythonhosted.org" in url:
                return b""
            raise AssertionError(f"CLI test saw unexpected URL: {url}")
        return encoded[url]
    _pypi.set_fetcher(fetch)


def test_cli_baseline_init_and_scan(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    _route_pypi({PYPI_JSON_URL.format(package="requests"): _pypi_doc()})
    # Disable npm probe — SC-008 isn't on the critical path for this test.
    _npm.set_fetcher(lambda url, timeout: (_ for _ in ()).throw(AssertionError("npm not expected")))

    try:
        # 1) init — no findings, snapshot recorded.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init",
            "--manifest", str(manifest),
            "--ecosystem", "pypi",
        ], catch_exceptions=False)
        assert r.exit_code == 0, r.output

        # 2) show — package is in the baseline.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "show",
            "--package", "requests",
        ], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        assert "2.31.0" in r.output

        # 3) scan deps with --no-github --no-npm — baseline unchanged, no findings.
        out = tmp_path / "findings.json"
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps",
            "--manifest", str(manifest),
            "--ecosystem", "pypi",
            "--output", "json",
            "--output-file", str(out),
            "--no-github", "--no-npm",
        ], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == "1.0"
        assert payload["tool"] == "pipeline-watch"
        assert payload["score"]["grade"] == "A"
        assert payload["findings"] == []

        # 4) scan deps after PyPI flipped to a new maintainer → SC-001 fires.
        _route_pypi({PYPI_JSON_URL.format(package="requests"):
                     _pypi_doc(version="2.32.0", maintainer="mallory")})
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps",
            "--manifest", str(manifest),
            "--ecosystem", "pypi",
            "--output", "json",
            "--output-file", str(out),
            "--no-github", "--no-npm",
            "--fail-on", "HIGH",
        ], catch_exceptions=False)
        # MEDIUM (no github probe) → gate at HIGH still passes.
        assert r.exit_code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert any(f["check_id"] == "SC-001" for f in payload["findings"])

        # 5) Reset the package, re-init against alice, then a fresh scan
        #    with mallory should fire SC-001 *and* fail the MEDIUM gate.
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "reset", "--scope", "package:requests",
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"): _pypi_doc()})
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init",
            "--manifest", str(manifest),
        ], catch_exceptions=False)
        _route_pypi({PYPI_JSON_URL.format(package="requests"):
                     _pypi_doc(version="2.32.0", maintainer="mallory")})
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "scan", "deps",
            "--manifest", str(manifest),
            "--ecosystem", "pypi",
            "--output", "json",
            "--output-file", str(out),
            "--no-github", "--no-npm",
            "--fail-on", "MEDIUM",
        ], catch_exceptions=False)
        assert r.exit_code == 1, r.output
    finally:
        _pypi.set_fetcher(None)
        _npm.set_fetcher(None)


def test_cli_show_without_baseline_errors_clearly(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "show", "--package", "requests",
    ], catch_exceptions=False)
    assert r.exit_code == 3
    assert "no snapshot" in r.output or "no snapshot" in r.stderr_bytes.decode(errors="replace")


def test_cli_init_missing_manifest(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(cli, [
        "--baseline-db", str(tmp_path / "baseline.db"),
        "baseline", "init",
        "--manifest", str(tmp_path / "nope.txt"),
    ], catch_exceptions=False)
    assert r.exit_code == 2
    assert "not found" in r.output


def test_cli_reset_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("requests==2.31.0\n", encoding="utf-8")
    baseline_db = tmp_path / "baseline.db"

    _route_pypi({PYPI_JSON_URL.format(package="requests"): _pypi_doc()})
    try:
        runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "init",
            "--manifest", str(manifest),
        ], catch_exceptions=False)
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "reset", "--scope", "package:requests",
        ], catch_exceptions=False)
        assert r.exit_code == 0
        # After reset, show has nothing.
        r = runner.invoke(cli, [
            "--baseline-db", str(baseline_db),
            "baseline", "show", "--package", "requests",
        ], catch_exceptions=False)
        assert r.exit_code == 3
    finally:
        _pypi.set_fetcher(None)
