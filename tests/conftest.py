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
from pipeline_watch.providers import pypi as _pypi

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(rel: str) -> dict:
    with (FIXTURES / rel).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def fixture_loader():
    return load_fixture


@pytest.fixture
def store() -> Store:
    """Fresh in-memory Store per test — no filesystem side effects."""
    return Store(sqlite3.connect(":memory:"))


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
