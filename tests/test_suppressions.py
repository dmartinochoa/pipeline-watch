"""Unit tests for the suppression file loader + applier."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from pipeline_watch.output.schema import Finding, Module, Severity
from pipeline_watch.suppressions import (
    Suppression,
    apply_suppressions,
    default_suppression_path,
    load_suppressions,
)


def _finding(*, check_id: str = "SC-001", package: str = "requests") -> Finding:
    return Finding(
        module=Module.SUPPLY_CHAIN,
        severity=Severity.MEDIUM,
        signal="test signal",
        baseline="test baseline",
        remediation="test",
        evidence={"package": package},
        check_id=check_id,
    )


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    res = load_suppressions(tmp_path / "nope.json")
    assert res.suppressions == []
    assert res.warnings == []


def test_load_and_apply_suppression(tmp_path: Path) -> None:
    path = tmp_path / "ignore.json"
    path.write_text(json.dumps({
        "suppressions": [
            {"check_id": "SC-001", "package": "requests", "reason": "legit"},
        ],
    }), encoding="utf-8")
    res = load_suppressions(path)
    assert len(res.suppressions) == 1

    kept, suppressed = apply_suppressions(
        [_finding(), _finding(package="other")], res.suppressions,
    )
    assert [f.evidence["package"] for f in kept] == ["other"]
    assert len(suppressed) == 1


def test_load_reason_required(tmp_path: Path) -> None:
    path = tmp_path / "ignore.json"
    path.write_text(json.dumps({
        "suppressions": [{"check_id": "SC-001"}],  # missing reason
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="'reason' is required"):
        load_suppressions(path)


def test_load_needs_package_or_check_id(tmp_path: Path) -> None:
    path = tmp_path / "ignore.json"
    path.write_text(json.dumps({
        "suppressions": [{"reason": "too broad"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="must specify at least one"):
        load_suppressions(path)


def test_load_expired_emits_warning(tmp_path: Path) -> None:
    path = tmp_path / "ignore.json"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({
        "suppressions": [
            {"check_id": "SC-001", "reason": "old", "expires": yesterday},
        ],
    }), encoding="utf-8")
    res = load_suppressions(path)
    assert res.suppressions == []
    assert res.warnings and "expired" in res.warnings[0]


def test_load_invalid_expires_format(tmp_path: Path) -> None:
    path = tmp_path / "ignore.json"
    path.write_text(json.dumps({
        "suppressions": [
            {"check_id": "SC-001", "reason": "x", "expires": "tomorrow"},
        ],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        load_suppressions(path)


def test_load_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "ignore.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_suppressions(path)


def test_suppression_matches_package_only() -> None:
    s = Suppression(reason="x", package="requests")
    assert s.matches(_finding(package="requests"))
    assert not s.matches(_finding(package="urllib3"))


def test_suppression_matches_check_id_only() -> None:
    s = Suppression(reason="x", check_id="SC-001")
    assert s.matches(_finding(check_id="SC-001"))
    assert not s.matches(_finding(check_id="SC-005"))


def test_default_suppression_path_uses_baseline_dir(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "baseline.db"
    assert default_suppression_path(p) == tmp_path / "sub" / "ignore.json"
