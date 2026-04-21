"""Shared fixtures for pipeline-watch tests.

Path-finding for fixture JSONs lives here so individual test modules
don't each roll their own ``Path(__file__).parent / "fixtures"`` incant.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline_watch.baseline.store import Store
from pipeline_watch.providers import github as _github
from pipeline_watch.providers import npm as _npm
from pipeline_watch.providers import pypi as _pypi
from pipeline_watch.providers.pypi import PYPI_JSON_URL

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_provider_fetchers():
    """Reset every provider fetcher after each test.

    Many tests install a fake ``_fetcher`` via ``set_fetcher(...)`` and
    tear it down in a ``finally`` block. That pattern is correct but
    fragile — a new test added without the ``finally`` will silently
    leak the fake fetcher into every subsequent test in the file. This
    autouse fixture is the belt-and-braces: whatever the test did,
    we're back to the real default before the next one starts.
    """
    yield
    _pypi.set_fetcher(None)
    _npm.set_fetcher(None)
    _github.set_fetcher(None)


# ── Shared CLI helpers ──────────────────────────────────────────────
#
# ``_pypi_doc`` / ``_route_pypi`` were independently defined in
# test_cli.py and test_cli_extra.py, which drifts as signals evolve.
# Hoisting the canonical versions here keeps CI tests consistent with
# what baseline/scan actually fetches.


def pypi_doc(
    *,
    version: str = "2.31.0",
    package: str = "requests",
    maintainer: str = "Kenneth Reitz",
    extra_maintainer: str | None = None,
    deps: list[str] | None = None,
    upload_time_iso: str = "2023-05-22T14:30:00Z",
    project_urls: dict | None = None,
) -> dict:
    """Return a minimal PyPI JSON doc for *package*@*version*."""
    info: dict = {
        "name": package, "version": version,
        "author": maintainer, "author_email": "me@example.com",
        "project_urls": (
            project_urls if project_urls is not None
            else {"Source": f"https://github.com/psf/{package}"}
        ),
        "requires_dist": deps or [],
    }
    if extra_maintainer is not None:
        info["maintainer"] = extra_maintainer
        info["maintainer_email"] = f"{extra_maintainer.lower()}@example.com"
    return {
        "info": info,
        "releases": {
            version: [{
                "packagetype": "sdist",
                "url": f"https://files.pythonhosted.org/packages/z/{package}-{version}.tar.gz",
                "upload_time_iso_8601": upload_time_iso,
            }],
        },
    }


def route_pypi(routes: dict[str, dict]) -> None:
    """Install a PyPI fetcher that serves the given URL→body routes.

    Sdist URLs (``files.pythonhosted.org``) get an empty bytes response
    so the install-script probe short-circuits — callers that need the
    probe to run should pass tar/zip bytes explicitly.
    """
    encoded = {u: json.dumps(body).encode("utf-8") for u, body in routes.items()}

    def fetch(url: str, timeout: float):  # noqa: ARG001
        if url not in encoded:
            if "files.pythonhosted.org" in url:
                return b""
            raise AssertionError(f"test saw unexpected URL: {url}")
        return encoded[url]
    _pypi.set_fetcher(fetch)


@pytest.fixture
def pypi_doc_factory():
    """Factory returning :func:`pypi_doc`."""
    return pypi_doc


@pytest.fixture
def route_pypi_factory():
    """Factory returning :func:`route_pypi`."""
    return route_pypi


@pytest.fixture
def pypi_url_factory():
    """Return ``PYPI_JSON_URL`` for test convenience."""
    return PYPI_JSON_URL


def load_fixture(rel: str) -> dict:
    with (FIXTURES / rel).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def fixture_loader():
    return load_fixture


@pytest.fixture
def store():
    """Fresh in-memory Store per test — no filesystem side effects."""
    s = Store(sqlite3.connect(":memory:"))
    try:
        yield s
    finally:
        s.close()


class FakePyPIFetcher:
    """Route PyPI JSON URLs to local fixtures; raise on anything else."""

    def __init__(self, routes: dict[str, bytes | dict]) -> None:
        self._routes: dict[str, bytes] = {}
        for url, body in routes.items():
            if isinstance(body, dict):
                self._routes[url] = json.dumps(body).encode("utf-8")
            else:
                self._routes[url] = body

    def __call__(self, url: str, timeout: float) -> bytes:  # noqa: ARG002 - matches signature
        if url not in self._routes:
            raise AssertionError(
                f"FakePyPIFetcher saw unexpected URL {url!r}; "
                f"routes: {sorted(self._routes)}"
            )
        return self._routes[url]


@pytest.fixture
def install_fake_fetcher():
    """Install a FakePyPIFetcher and tear it down automatically.

    Usage::

        def test_x(install_fake_fetcher):
            install_fake_fetcher({PYPI_JSON_URL.format(package="x"): doc})
    """
    def _install(routes: dict[str, bytes | dict]) -> FakePyPIFetcher:
        fetcher = FakePyPIFetcher(routes)
        _pypi.set_fetcher(fetcher)
        return fetcher
    yield _install
    _pypi.set_fetcher(None)
