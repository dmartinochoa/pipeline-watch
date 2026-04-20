"""npm registry probe tests."""
from __future__ import annotations

import json

import pytest

from pipeline_watch.providers import npm


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

    def fetch(url: str, timeout: float):  # noqa: ARG001
        return json.dumps(doc).encode()

    npm.set_fetcher(fetch)
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
