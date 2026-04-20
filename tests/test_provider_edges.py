"""Edge-case coverage for PyPI / npm / GitHub providers.

Exercises maintainer normalisation, repository URL variants, install-
hook signatures across archive shapes, and the paths where the registry
responds in unhelpful ways.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from pipeline_watch.providers import github as _github
from pipeline_watch.providers import npm as _npm
from pipeline_watch.providers import pypi as _pypi

# ── PyPI ────────────────────────────────────────────────────────────


def test_pypi_yanked_release_propagates_to_snapshot() -> None:
    doc = {
        "info": {
            "name": "pkg", "version": "1.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {
            "1.0": [{
                "packagetype": "sdist",
                "url": "https://example.invalid/pkg.tar.gz",
                "upload_time_iso_8601": "2023-01-01T12:00:00Z",
                "yanked": True,
                "yanked_reason": "bad release",
            }],
        },
    }

    def fetch(url, timeout):  # noqa: ARG001
        if "pypi.org" in url:
            return json.dumps(doc).encode()
        # sdist download — don't actually probe contents for this test.
        return b""
    _pypi.set_fetcher(fetch)
    try:
        pkg = _pypi.fetch_package("pkg", include_install_script_hash=False)
        assert pkg is not None
        snap = _pypi.snapshot_from_package(pkg, recorded_at="2026-04-20T00:00:00+00:00")
        assert snap.yanked is True
    finally:
        _pypi.set_fetcher(None)


def test_pypi_maintainers_dedup_and_normalise() -> None:
    info = {
        "author": "Alice",
        "author_email": "alice@example.com",
        "maintainer": "Alice",              # duplicate of author — should collapse
        "maintainer_email": "alice@example.com",
        "maintainers": [
            {"name": "Bob", "email": "bob@example.com"},
            {"username": "carol", "email": "carol@example.com"},
        ],
    }
    result = _pypi._collect_maintainers(info)
    names = {m["name"].lower() for m in result}
    assert names == {"alice", "bob", "carol"}


def test_pypi_parse_requires_dist_ignores_non_strings() -> None:
    parsed = _pypi._parse_requires_dist(["click (>=8)", None, 42, ""])  # type: ignore[list-item]
    assert parsed == {"click": ">=8"}


def test_pypi_source_repo_returns_none_without_match() -> None:
    pkg = _pypi.PyPIPackage(
        name="x", latest_version="1.0", maintainers=[], releases=[],
        dependencies={}, project_urls={"Homepage": "https://example.com"},
    )
    assert pkg.source_repo() is None


def test_pypi_install_script_probe_handles_wheel_zip() -> None:
    """A .whl sdist URL is probed through the zipfile branch."""
    # Build an in-memory wheel-shaped zip with a setup.py entry.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/setup.py", b"print('hi')")
        zf.writestr("pkg/other.txt", b"ignored")
    wheel_bytes = buf.getvalue()

    doc = {
        "info": {
            "name": "pkg", "version": "1.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {
            "1.0": [{
                "packagetype": "sdist",
                "url": "https://example.invalid/pkg-1.0.whl",
                "upload_time_iso_8601": "2026-04-20T09:00:00Z",
            }],
        },
    }

    def fetch(url, timeout):  # noqa: ARG001
        if "pypi.org" in url:
            return json.dumps(doc).encode()
        return wheel_bytes
    _pypi.set_fetcher(fetch)
    try:
        pkg = _pypi.fetch_package("pkg", include_install_script_hash=True)
        assert pkg is not None
        latest = pkg.latest_release()
        assert latest is not None
        assert latest.has_install_script is True
        assert latest.install_script_hash is not None
    finally:
        _pypi.set_fetcher(None)


def test_pypi_install_script_probe_skips_unknown_archive() -> None:
    """A non-tar/non-zip sdist URL returns no-hook rather than crashing."""
    doc = {
        "info": {
            "name": "pkg", "version": "1.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {
            "1.0": [{
                "packagetype": "sdist",
                "url": "https://example.invalid/pkg-1.0.exe",
                "upload_time_iso_8601": "2026-04-20T09:00:00Z",
            }],
        },
    }

    def fetch(url, timeout):  # noqa: ARG001
        if "pypi.org" in url:
            return json.dumps(doc).encode()
        return b"garbage"
    _pypi.set_fetcher(fetch)
    try:
        pkg = _pypi.fetch_package("pkg", include_install_script_hash=True)
        latest = pkg.latest_release()
        assert latest is not None
        assert latest.has_install_script is False
        assert latest.install_script_hash is None
    finally:
        _pypi.set_fetcher(None)


def test_pypi_non_json_response_raises() -> None:
    def fetch(url, timeout):  # noqa: ARG001
        return b"<html>not json</html>"
    _pypi.set_fetcher(fetch)
    try:
        with pytest.raises(_pypi.PyPIError):
            _pypi.fetch_package("x", include_install_script_hash=False)
    finally:
        _pypi.set_fetcher(None)


def test_pypi_network_error_raises() -> None:
    import urllib.error

    def fetch(url, timeout):  # noqa: ARG001
        raise urllib.error.URLError("dns fail")
    _pypi.set_fetcher(fetch)
    try:
        with pytest.raises(_pypi.PyPIError):
            _pypi.fetch_package("x", include_install_script_hash=False)
    finally:
        _pypi.set_fetcher(None)


# ── npm ─────────────────────────────────────────────────────────────


def test_npm_maintainer_string_form_is_parsed() -> None:
    assert _npm._maintainer_entry("Alice <alice@example.com>") == {
        "name": "Alice", "email": "alice@example.com", "first_seen": "",
    }
    assert _npm._maintainer_entry("solo") == {
        "name": "solo", "email": "", "first_seen": "",
    }
    # Anything unstructured collapses to empties.
    assert _npm._maintainer_entry(42)["name"] == ""


def test_npm_repository_url_strips_git_prefix_and_dotgit() -> None:
    url = _npm._extract_repository_url(
        {"repository": {"url": "git+https://github.com/foo/bar.git"}}, {},
    )
    assert url == "https://github.com/foo/bar"


def test_npm_repository_url_returns_none_when_unknown_host() -> None:
    url = _npm._extract_repository_url(
        {"repository": "https://example.com/foo/bar"}, {},
    )
    assert url is None


def test_npm_fetch_package_returns_none_on_404() -> None:
    import urllib.error

    def fetch(url, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
    _npm.set_fetcher(fetch)
    try:
        assert _npm.fetch_package("nope") is None
    finally:
        _npm.set_fetcher(None)


def test_npm_fetch_package_non_json_raises() -> None:
    def fetch(url, timeout):  # noqa: ARG001
        return b"<html>"
    _npm.set_fetcher(fetch)
    try:
        with pytest.raises(_npm.NpmError):
            _npm.fetch_package("x")
    finally:
        _npm.set_fetcher(None)


def test_npm_parse_package_json_ignores_non_dict_section(tmp_path) -> None:
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(json.dumps({
        "name": "x", "version": "1",
        "dependencies": {"ok": "1"},
        "devDependencies": ["not-a-dict"],
    }), encoding="utf-8")
    entries = _npm.parse_package_json(pkg_json)
    assert {e.name for e in entries} == {"ok"}


def test_npm_parse_package_json_skips_blank_keys(tmp_path) -> None:
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(json.dumps({
        "dependencies": {"": "1.0", "good": "2.0"},
    }), encoding="utf-8")
    entries = _npm.parse_package_json(pkg_json)
    assert {e.name for e in entries} == {"good"}


def test_npm_snapshot_with_no_release_time_leaves_hour_blank() -> None:
    doc = {
        "name": "x", "dist-tags": {"latest": "1.0.0"},
        "maintainers": [{"name": "m"}],
        "versions": {"1.0.0": {"dependencies": {}, "scripts": {}}},
        "time": {},
    }
    _npm.set_fetcher(lambda url, timeout: json.dumps(doc).encode())  # noqa: ARG005
    try:
        pkg = _npm.fetch_package("x")
        assert pkg is not None
        snap = _npm.snapshot_from_package(pkg, recorded_at="2026-04-20T00:00:00+00:00")
        assert snap.release_hour is None
        assert snap.release_weekday is None
        assert snap.release_uploaded_at == ""
    finally:
        _npm.set_fetcher(None)


def test_npm_package_info_without_created_time() -> None:
    _npm.set_fetcher(lambda url, timeout: json.dumps({"name": "x"}).encode())  # noqa: ARG005
    try:
        info = _npm.package_info("x")
        assert info is not None
        assert info.created_iso == ""
        assert info.created_date == ""
    finally:
        _npm.set_fetcher(None)


def test_npm_parse_iso_invalid_returns_none() -> None:
    assert _npm._parse_iso("not-a-date") is None
    assert _npm._parse_iso("") is None


# ── GitHub ──────────────────────────────────────────────────────────


def test_github_parse_repo_url_rejects_too_few_parts() -> None:
    assert _github.parse_repo_url("https://github.com/just-org") is None


def test_github_parse_repo_url_handles_trailing_slash() -> None:
    assert _github.parse_repo_url("https://github.com/org/repo/") == ("org", "repo")


def test_github_list_tags_returns_empty_on_404() -> None:
    import urllib.error

    def fetch(url, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
    _github.set_fetcher(fetch)
    try:
        assert _github.list_tags("org", "repo") == []
    finally:
        _github.set_fetcher(None)


def test_github_list_tags_returns_empty_when_not_a_list() -> None:
    _github.set_fetcher(lambda url, timeout: b'{"error": "denied"}')  # noqa: ARG005
    try:
        assert _github.list_tags("org", "repo") == []
    finally:
        _github.set_fetcher(None)


def test_github_fetch_json_other_5xx_raises() -> None:
    import urllib.error

    def fetch(url, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 500, "Server Error", hdrs=None, fp=None)
    _github.set_fetcher(fetch)
    try:
        with pytest.raises(_github.GitHubError):
            _github.list_tags("org", "repo")
    finally:
        _github.set_fetcher(None)


def test_github_fetch_json_network_error_raises() -> None:
    import urllib.error

    def fetch(url, timeout):  # noqa: ARG001
        raise urllib.error.URLError("dns fail")
    _github.set_fetcher(fetch)
    try:
        with pytest.raises(_github.GitHubError):
            _github.list_tags("org", "repo")
    finally:
        _github.set_fetcher(None)


def test_github_fetch_json_non_json_raises() -> None:
    _github.set_fetcher(lambda url, timeout: b"<html>")  # noqa: ARG005
    try:
        with pytest.raises(_github.GitHubError):
            _github.list_tags("org", "repo")
    finally:
        _github.set_fetcher(None)


def test_pypi_release_upload_datetime_invalid_returns_none() -> None:
    r = _pypi.PyPIRelease(
        version="1.0", upload_time_iso="garbage", sdist_url=None,
        has_install_script=False, install_script_hash=None,
    )
    assert r.upload_datetime() is None


def test_pypi_latest_release_falls_back_to_first_when_no_match() -> None:
    """If latest_version doesn't match any release, we fall back to releases[0]."""
    a = _pypi.PyPIRelease(
        version="1.0", upload_time_iso="2020-01-01T00:00:00Z", sdist_url=None,
        has_install_script=False, install_script_hash=None,
    )
    pkg = _pypi.PyPIPackage(
        name="x", latest_version="99.0",  # no matching release
        maintainers=[], releases=[a], dependencies={}, project_urls={},
    )
    assert pkg.latest_release() is a


def test_pypi_pick_sdist_returns_none_when_only_wheels() -> None:
    assert _pypi._pick_sdist([{"packagetype": "bdist_wheel"}]) is None


def test_pypi_pick_yanked_returns_false_when_no_files_yanked() -> None:
    assert _pypi._pick_yanked([{"packagetype": "sdist"}]) == (False, "")


def test_pypi_pick_upload_time_returns_earliest() -> None:
    earliest = _pypi._pick_upload_time([
        {"upload_time_iso_8601": "2023-05-22T14:30:00Z"},
        {"upload_time_iso_8601": "2023-05-22T10:00:00Z"},
        {"upload_time": "2023-05-22T12:00:00Z"},
    ])
    assert earliest == "2023-05-22T10:00:00Z"


def test_pypi_pick_upload_time_empty_list() -> None:
    assert _pypi._pick_upload_time([]) == ""


def test_npm_collect_dependencies_handles_non_dict() -> None:
    assert _npm._collect_dependencies({"dependencies": ["not", "a", "dict"]}) == {}
    assert _npm._collect_dependencies("not even a dict") == {}  # type: ignore[arg-type]


def test_npm_collect_maintainers_drops_entries_without_name_or_email() -> None:
    doc = {"maintainers": [{"name": "", "email": ""}, {"name": "good"}]}
    latest = {"maintainers": [{"name": "", "email": ""}]}
    result = _npm._collect_maintainers(doc, latest)
    assert [m["name"] for m in result] == ["good"]


def test_npm_maintainers_from_latest_view_are_included() -> None:
    doc = {"maintainers": []}
    latest = {"maintainers": [{"name": "alice"}]}
    result = _npm._collect_maintainers(doc, latest)
    assert any(m["name"] == "alice" for m in result)


def test_npm_extract_repository_url_ignores_non_dict_source() -> None:
    # Non-dict, non-string repository field → skip that source.
    assert _npm._extract_repository_url({"repository": 42}, {}) is None


def test_npm_fetch_package_empty_versions_handled() -> None:
    doc = {
        "name": "x", "dist-tags": {"latest": "1.0.0"},
        "maintainers": [{"name": "m"}],
        "versions": {},
        "time": {},
    }
    _npm.set_fetcher(lambda url, timeout: json.dumps(doc).encode())  # noqa: ARG005
    try:
        pkg = _npm.fetch_package("x")
        assert pkg is not None
        assert pkg.releases == []
        assert pkg.latest_release() is None
    finally:
        _npm.set_fetcher(None)


def test_github_set_fetcher_reset_restores_default() -> None:
    # Set then reset — confirms the ``None`` branch assigns _default_fetcher.
    _github.set_fetcher(lambda url, timeout: b"[]")  # noqa: ARG005
    _github.set_fetcher(None)
    # After reset, private module var points at the default.
    assert _github._fetcher is _github._default_fetcher
