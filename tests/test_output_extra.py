"""Additional tests for output schema / formatter edge paths."""
from __future__ import annotations

import json

from pipeline_watch.output.formatter import report_json, report_terminal
from pipeline_watch.output.schema import (
    TOOL_NAME,
    Finding,
    Module,
    Severity,
    dumps,
    score_from_findings,
    severity_rank,
    validate_finding_dict,
)


def _make(sev: Severity, **overrides) -> Finding:
    base = dict(
        module=Module.SUPPLY_CHAIN,
        severity=sev,
        signal="signal",
        baseline="baseline",
        remediation="remediation",
        evidence={"package": "x"},
        timestamp="2026-04-20T12:00:00+00:00",
        check_id="SC-001",
    )
    base.update(overrides)
    return Finding(**base)


def test_severity_rank_is_ordered() -> None:
    # CRITICAL outranks HIGH outranks MEDIUM outranks LOW.
    ranks = [severity_rank(s) for s in
             (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)]
    assert ranks == sorted(ranks)
    assert ranks == [0, 1, 2, 3]


def test_validate_finding_dict_rejects_non_dict_evidence() -> None:
    d = _make(Severity.HIGH).to_dict()
    d["evidence"] = ["not", "a", "dict"]
    errors = validate_finding_dict(d)
    assert any("evidence must be an object" in e for e in errors)


def test_validate_finding_dict_rejects_unknown_tool() -> None:
    d = _make(Severity.HIGH).to_dict()
    d["tool"] = "not-our-tool"
    errors = validate_finding_dict(d)
    assert any("unexpected tool" in e for e in errors)


def test_dumps_is_stable_indent() -> None:
    text = dumps({"a": 1, "b": [2, 3]})
    payload = json.loads(text)
    assert payload == {"a": 1, "b": [2, 3]}
    # Stable 2-space indent.
    assert "  " in text


def test_report_terminal_uses_default_console_when_none_supplied(capsys) -> None:
    # Hits the "console is None" branch — the formatter must build its own.
    report_terminal(
        [_make(Severity.LOW)],
        score_from_findings([_make(Severity.LOW)]),
    )
    captured = capsys.readouterr()
    assert "Grade B" in captured.out


def test_report_terminal_mixed_severities_ordered_critical_first(capsys) -> None:
    findings = [
        _make(Severity.LOW, check_id="SC-L"),
        _make(Severity.CRITICAL, check_id="SC-C"),
        _make(Severity.MEDIUM, check_id="SC-M"),
    ]
    report_terminal(findings, score_from_findings(findings))
    out = capsys.readouterr().out
    # Critical panel appears before Low panel.
    assert out.index("SC-C") < out.index("SC-L")


def test_report_json_preserves_evidence_payload() -> None:
    f = _make(Severity.HIGH, evidence={"package": "requests", "maintainer": "mallory"})
    text = report_json([f], tool_version="0.1.0", module="supply-chain")
    payload = json.loads(text)
    assert payload["findings"][0]["evidence"]["maintainer"] == "mallory"
    assert payload["tool"] == TOOL_NAME
