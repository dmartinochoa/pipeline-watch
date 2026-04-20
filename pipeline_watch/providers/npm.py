"""Minimal npm registry helpers — Module 1 only needs one call.

Supply-chain signal SC-008 flags when a package name from the PyPI
manifest has been newly registered on npm (and vice versa in later
modules). All we need is "does the name exist on npm, and if so,
when was it first published?".

Full npm parsing (for scan deps --ecosystem npm) arrives in Module 1b,
reusing the same fetcher pattern used here and in ``providers.pypi``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

NPM_REGISTRY_URL = "https://registry.npmjs.org/{package}"

_Fetcher = Callable[[str, float], bytes]


def _default_fetcher(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "pipeline-watch/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


_fetcher: _Fetcher = _default_fetcher


def set_fetcher(fetcher: _Fetcher | None) -> None:
    global _fetcher
    _fetcher = fetcher or _default_fetcher


class NpmError(RuntimeError):
    """Raised on any non-404 npm registry failure."""


def _fetch_json(url: str, timeout: float = 10.0) -> Any:
    try:
        raw = _fetcher(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise NpmError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise NpmError(f"network error fetching {url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NpmError(f"non-JSON response from {url}: {exc}") from exc


@dataclass
class NpmPackageInfo:
    name: str
    created_iso: str  # ``time.created`` — when the package first appeared

    @property
    def created_date(self) -> str:
        return self.created_iso[:10]


def package_info(name: str) -> NpmPackageInfo | None:
    """Return registration info for *name*, or ``None`` if npm has no such package.

    Used only by SC-008. Returns just the ``time.created`` field — the
    detector compares this to "now" and emits a finding when it's
    within the recent-registration window.
    """
    doc = _fetch_json(NPM_REGISTRY_URL.format(package=name))
    if doc is None:
        return None
    created = str((doc.get("time") or {}).get("created", ""))
    return NpmPackageInfo(name=str(doc.get("name") or name), created_iso=created)
