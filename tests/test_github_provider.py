"""Tiny smoke tests for the GitHub REST helpers."""
from __future__ import annotations

import json

import pytest

from pipeline_watch.providers import github as gh


def test_parse_repo_url_accepts_trailing_git() -> None:
    assert gh.parse_repo_url("https://github.com/org/repo.git") == ("org", "repo")


def test_parse_repo_url_rejects_non_github() -> None:
    assert gh.parse_repo_url("https://gitlab.com/org/repo") is None


def test_parse_repo_url_handles_query_and_fragment() -> None:
    assert gh.parse_repo_url("https://github.com/a/b?tab=readme#x") == ("a", "b")


def test_user_has_commits_true_when_list_non_empty(monkeypatch) -> None:
    def fetch(url: str, timeout: float):  # noqa: ARG001
        return json.dumps([{"sha": "deadbeef"}]).encode()
    gh.set_fetcher(fetch)
    try:
        assert gh.user_has_commits("org", "repo", "alice") is True
    finally:
        gh.set_fetcher(None)


def test_user_has_commits_false_when_empty() -> None:
    def fetch(url: str, timeout: float):  # noqa: ARG001
        return b"[]"
    gh.set_fetcher(fetch)
    try:
        assert gh.user_has_commits("org", "repo", "alice") is False
    finally:
        gh.set_fetcher(None)


def test_user_has_commits_false_on_404() -> None:
    import urllib.error

    def fetch(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
    gh.set_fetcher(fetch)
    try:
        assert gh.user_has_commits("org", "repo", "alice") is False
    finally:
        gh.set_fetcher(None)


def test_list_tags_returns_names() -> None:
    def fetch(url: str, timeout: float):  # noqa: ARG001
        return json.dumps([{"name": "v1.0"}, {"name": "v2.0"}]).encode()
    gh.set_fetcher(fetch)
    try:
        assert gh.list_tags("org", "repo") == ["v1.0", "v2.0"]
    finally:
        gh.set_fetcher(None)


def test_rate_limit_403_surfaces_distinct_error() -> None:
    import urllib.error

    def fetch(url: str, timeout: float):  # noqa: ARG001
        raise urllib.error.HTTPError(url, 403, "Rate Limited", hdrs=None, fp=None)
    gh.set_fetcher(fetch)
    try:
        with pytest.raises(gh.GitHubError) as exc:
            gh.user_has_commits("org", "repo", "alice")
        assert "rate-limited" in str(exc.value)
    finally:
        gh.set_fetcher(None)


def test_default_fetcher_issues_request(monkeypatch) -> None:
    """Exercise the real urllib code path with a stubbed urlopen."""
    from unittest.mock import MagicMock

    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    # The default fetcher is the one installed at module load.
    result = gh._default_fetcher("https://api.github.com/test", timeout=1.0)
    assert result == b'{"ok": true}'
    assert captured["url"] == "https://api.github.com/test"
    # Headers dict keys are canonicalised by urllib — tolerate either form.
    keys_lower = {k.lower() for k in captured["headers"]}
    assert "user-agent" in keys_lower
    assert "accept" in keys_lower
    # Keep MagicMock import referenced so flake8 doesn't strip it.
    assert MagicMock is not None
