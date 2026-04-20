"""npm registry probe tests."""
from __future__ import annotations

import json

import pytest

from pipeline_watch.providers import npm


def _route(doc: dict):
    def fetch(url: str, timeout: float):  # noqa: ARG001
        return json.dumps(doc).encode()
    return fetch


def test_package_info_returns_none_on_404() -> None:
    import urllib.error

    def fetch(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
    npm.set_fetcher(fetch)
    try:
        assert npm.package_info("no-such-pkg") is None
    finally:
        npm.set_fetcher(None)


def test_package_info_parses_created_time() -> None:
    doc = {"name": "left-pad", "time": {"created": "2014-03-23T15:10:00.000Z"}}
    npm.set_fetcher(_route(doc))
    try:
        info = npm.package_info("left-pad")
        assert info is not None
        assert info.name == "left-pad"
        assert info.created_iso.startswith("2014-03-23")
        assert info.created_date == "2014-03-23"
    finally:
        npm.set_fetcher(None)


def test_5xx_raises_npm_error() -> None:
    import urllib.error

    def fetch(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", hdrs=None, fp=None)

    npm.set_fetcher(fetch)
    try:
        with pytest.raises(npm.NpmError):
            npm.package_info("x")
    finally:
        npm.set_fetcher(None)


def test_fetch_package_extracts_latest_metadata() -> None:
    doc = {
        "name": "left-pad",
        "dist-tags": {"latest": "1.3.0"},
        "maintainers": [{"name": "camwest", "email": "c@example.com"}],
        "versions": {
            "1.2.0": {
                "dependencies": {},
                "scripts": {},
            },
            "1.3.0": {
                "author": {"name": "camwest", "email": "c@example.com"},
                "dependencies": {"foo": "^1.0.0"},
                "repository": {"url": "git+https://github.com/stevemao/left-pad.git"},
                "scripts": {"postinstall": "node ./hook.js"},
                "deprecated": "use the platform String.padStart",
            },
        },
        "time": {
            "1.2.0": "2015-01-01T00:00:00.000Z",
            "1.3.0": "2016-01-01T12:34:00.000Z",
        },
    }
    npm.set_fetcher(_route(doc))
    try:
        pkg = npm.fetch_package("left-pad")
        assert pkg is not None
        assert pkg.latest_version == "1.3.0"
        assert any(m["name"] == "camwest" for m in pkg.maintainers)
        assert pkg.dependencies == {"foo": "^1.0.0"}
        assert pkg.source_repo() == "https://github.com/stevemao/left-pad"
        latest = pkg.latest_release()
        assert latest is not None
        assert latest.has_install_script is True
        assert latest.install_script_hash is not None
        assert latest.deprecated.startswith("use the platform")
    finally:
        npm.set_fetcher(None)


def test_snapshot_from_package_populates_yanked_from_deprecated() -> None:
    doc = {
        "name": "foo",
        "dist-tags": {"latest": "1.0.0"},
        "maintainers": [{"name": "a"}],
        "versions": {
            "1.0.0": {
                "dependencies": {},
                "scripts": {},
                "deprecated": "please migrate",
            }
        },
        "time": {"1.0.0": "2024-09-01T08:00:00.000Z"},
    }
    npm.set_fetcher(_route(doc))
    try:
        pkg = npm.fetch_package("foo")
        assert pkg is not None
        snap = npm.snapshot_from_package(pkg, recorded_at="2026-04-20T00:00:00+00:00")
        assert snap.ecosystem == "npm"
        assert snap.version == "1.0.0"
        assert snap.yanked is True
        assert snap.release_uploaded_at.startswith("2024-09-01")
    finally:
        npm.set_fetcher(None)


def test_parse_package_json_collects_deps_and_dev_deps(tmp_path) -> None:
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(json.dumps({
        "name": "my-app",
        "version": "1.0.0",
        "dependencies": {"react": "^18", "left-pad": "1.3.0"},
        "devDependencies": {"jest": "^29"},
        "optionalDependencies": {"fsevents": "^2"},
    }), encoding="utf-8")
    entries = npm.parse_package_json(pkg_json)
    names = {e.name for e in entries}
    assert names == {"react", "left-pad", "jest", "fsevents"}
    by_name = {e.name: e.constraint for e in entries}
    assert by_name["react"] == "^18"
    assert by_name["left-pad"] == "1.3.0"
