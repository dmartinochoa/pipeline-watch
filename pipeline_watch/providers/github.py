"""Minimal GitHub REST helpers used by the supply-chain detector.

Only two questions need answering for SC-001 and SC-003:

* "has *username* ever committed to *owner/repo*?" — distinguishes a
  legitimate maintainer (who would have landed at least one PR) from
  an account that was granted publish rights on a dormant package.
* "does *owner/repo* have a git tag for *version*?" — catches
  releases that were pushed to PyPI but never appeared in the
  source repo, the classic "attacker has PyPI creds but no git
  creds" signature.

PyGithub is pinned for Module 3's vcs-audit detector, which actually
benefits from its richer API. For Module 1 the two REST endpoints we
need are trivial, so we route them through the same injectable
``urllib`` fetcher pattern used by the PyPI provider — tests stay
consistent and zero-network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

GITHUB_API = "https://api.github.com"

_Fetcher = Callable[[str, float], bytes]


def _default_fetcher(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pipeline-watch/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


_fetcher: _Fetcher = _default_fetcher


def set_fetcher(fetcher: _Fetcher | None) -> None:
    global _fetcher
    _fetcher = fetcher or _default_fetcher


class GitHubError(RuntimeError):
    """Raised on any non-404 GitHub API failure."""


def _fetch_json(url: str, timeout: float = 10.0) -> Any:
    try:
        raw = _fetcher(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        # Rate-limited? Surface a distinct message so operators can
        # tell whether to retry or reauthenticate.
        if exc.code == 403:
            raise GitHubError(
                f"GitHub returned 403 for {url} — possibly rate-limited; "
                "set GITHUB_TOKEN and retry."
            ) from exc
        raise GitHubError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"network error fetching {url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"non-JSON response from {url}: {exc}") from exc


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from a GitHub URL, or ``None`` if it isn't one."""
    if "github.com/" not in url:
        return None
    tail = url.split("github.com/", 1)[1]
    # Strip trailing ``.git``, query strings, and fragments.
    tail = tail.split("#", 1)[0].split("?", 1)[0]
    if tail.endswith(".git"):
        tail = tail[:-4]
    parts = tail.strip("/").split("/")
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def user_has_commits(owner: str, repo: str, username: str) -> bool:
    """Return True when *username* has at least one commit in *owner/repo*.

    Queries ``/repos/{owner}/{repo}/commits?author={username}&per_page=1``;
    an empty array or a 404 both mean "no prior history" — which is the
    SC-001 signal. Any other failure raises so the detector can decide
    whether to continue without SC-001 for that package.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits?author={username}&per_page=1"
    data = _fetch_json(url)
    if data is None:
        return False
    return bool(isinstance(data, list) and data)


def list_tags(owner: str, repo: str) -> list[str]:
    """Return the tag names on *owner/repo* (first page only).

    The first 100 tags are enough for SC-003: a release that can't
    find itself in the most recent 100 tags is almost certainly an
    attack, not an obscure historical version.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/tags?per_page=100"
    data = _fetch_json(url)
    if data is None or not isinstance(data, list):
        return []
    return [str(t.get("name") or "") for t in data if isinstance(t, dict)]
