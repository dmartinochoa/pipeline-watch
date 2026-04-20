"""PyPI provider tests — every HTTP call routed through a fake fetcher."""
from __future__ import annotations

import pytest

from pipeline_watch.providers.pypi import (
    PYPI_JSON_URL,
    PyPIError,
    _parse_requires_dist,
    fetch_package,
    snapshot_from_package,
)


def test_fetch_package_parses_fixture(fixture_loader, install_fake_fetcher) -> None:
    doc = fixture_loader("pypi/requests_v1.json")
    install_fake_fetcher({PYPI_JSON_URL.format(package="requests"): doc})
    pkg = fetch_package("requests", include_install_script_hash=False)
    assert pkg is not None
    assert pkg.name == "requests"
    assert pkg.latest_version == "2.31.0"
    # author/author_email collapse into one maintainer entry.
    assert any(m["name"] == "Kenneth Reitz" for m in pkg.maintainers)
    # Dependencies collapse across env markers, losing the extras-only PySocks.
    assert pkg.dependencies["urllib3"] == "<3,>=1.21.1"
    assert pkg.dependencies["certifi"] == ">=2017.4.17"
    assert pkg.dependencies["PySocks"].startswith("!=1.5.7")
    # Latest release is first in the sorted list and has the sdist URL.
    latest = pkg.latest_release()
    assert latest is not None
    assert latest.version == "2.31.0"
    assert latest.sdist_url and latest.sdist_url.endswith(".tar.gz")


def test_snapshot_from_package_fills_time_fields(fixture_loader, install_fake_fetcher) -> None:
    doc = fixture_loader("pypi/requests_v1.json")
    install_fake_fetcher({PYPI_JSON_URL.format(package="requests"): doc})
    pkg = fetch_package("requests", include_install_script_hash=False)
    assert pkg is not None
    snap = snapshot_from_package(pkg, recorded_at="2026-04-20T00:00:00+00:00")
    assert snap.ecosystem == "pypi"
    assert snap.package == "requests"
    assert snap.version == "2.31.0"
    assert snap.release_hour == 14
    # 2023-05-22 was a Monday (weekday 0).
    assert snap.release_weekday == 0


def test_fetch_package_returns_none_on_404(install_fake_fetcher, monkeypatch) -> None:
    import urllib.error

    def raise_404(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    from pipeline_watch.providers import pypi as _pypi
    _pypi.set_fetcher(raise_404)
    try:
        assert fetch_package("does-not-exist", include_install_script_hash=False) is None
    finally:
        _pypi.set_fetcher(None)


def test_fetch_package_raises_on_5xx() -> None:
    import urllib.error

    def raise_500(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 500, "Internal Error", hdrs=None, fp=None)

    from pipeline_watch.providers import pypi as _pypi
    _pypi.set_fetcher(raise_500)
    try:
        with pytest.raises(PyPIError):
            fetch_package("anything", include_install_script_hash=False)
    finally:
        _pypi.set_fetcher(None)


def test_parse_requires_dist_handles_missing_constraints() -> None:
    parsed = _parse_requires_dist(["click", "rich (>=13)", "typer; extra=='cli'"])
    assert parsed == {"click": "", "rich": ">=13", "typer": ""}


def test_install_script_hashing_reads_sdist(install_fake_fetcher, tmp_path) -> None:
    """Build a tiny .tar.gz with setup.py + __init__.py, confirm the hash is stable."""
    import io
    import tarfile

    setup_py = b"from setuptools import setup\nsetup(name='pkg')\n"
    init_py = b"import os, subprocess  # pretend postinstall\n"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("pkg-1.0/setup.py", setup_py), ("pkg-1.0/pkg/__init__.py", init_py)):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    sdist_bytes = buf.getvalue()

    doc = {
        "info": {
            "name": "pkg", "version": "1.0",
            "author": "x", "author_email": "x@x",
            "project_urls": {}, "requires_dist": [],
        },
        "releases": {
            "1.0": [{
                "packagetype": "sdist",
                "url": "https://files.pythonhosted.org/packages/zz/pkg-1.0.tar.gz",
                "upload_time_iso_8601": "2026-04-20T09:00:00Z",
            }],
        },
    }
    install_fake_fetcher({
        PYPI_JSON_URL.format(package="pkg"): doc,
        "https://files.pythonhosted.org/packages/zz/pkg-1.0.tar.gz": sdist_bytes,
    })
    pkg = fetch_package("pkg", include_install_script_hash=True)
    assert pkg is not None
    latest = pkg.latest_release()
    assert latest is not None
    assert latest.has_install_script is True
    assert latest.install_script_hash is not None
    assert len(latest.install_script_hash) == 64  # sha256 hex


def test_source_repo_prefers_github() -> None:
    from pipeline_watch.providers.pypi import PyPIPackage
    pkg = PyPIPackage(
        name="x", latest_version="1.0", maintainers=[], releases=[],
        dependencies={},
        project_urls={
            "Homepage": "https://example.com",
            "Source": "https://github.com/org/x",
        },
    )
    assert pkg.source_repo() == "https://github.com/org/x"
