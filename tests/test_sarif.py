"""Unit tests for SARIF rendering."""
from __future__ import annotations

import json

from pipeline_watch.output.sarif import to_sarif
from pipeline_watch.output.schema import Finding, Module, Severity


def _finding(severity: Severity, check_id: str = "SC-001") -> Finding:
    return Finding(
        module=Module.SUPPLY_CHAIN,
        severity=severity,
        signal="a signal",
        baseline="baseline line",
        remediation="do the thing",
        evidence={"package": "requests", "version": "2.32.0"},
        check_id=check_id,
    )


def test_sarif_empty_has_well_formed_envelope() -> None:
    payload = json.loads(to_sarif([], tool_version="0.1.0"))
    assert payload["version"] == "2.1.0"
    assert payload["runs"][0]["tool"]["driver"]["name"] == "pipeline-watch"
    assert payload["runs"][0]["results"] == []
    # Every SC-XXX rule is advertised even when nothing fires.
    rule_ids = {r["id"] for r in payload["runs"][0]["tool"]["driver"]["rules"]}
    assert "SC-001" in rule_ids and "SC-017" in rule_ids


def test_sarif_severity_maps_to_level() -> None:
    findings = [
        _finding(Severity.CRITICAL, "SC-004"),
        _finding(Severity.HIGH, "SC-003"),
        _finding(Severity.MEDIUM, "SC-002"),
        _finding(Severity.LOW, "SC-005"),
    ]
    payload = json.loads(to_sarif(findings, tool_version="0.1.0"))
    levels = {r["ruleId"]: r["level"] for r in payload["runs"][0]["results"]}
    assert levels["SC-004"] == "error"
    assert levels["SC-003"] == "error"
    assert levels["SC-002"] == "warning"
    assert levels["SC-005"] == "note"


def test_sarif_includes_manifest_location() -> None:
    payload = json.loads(to_sarif(
        [_finding(Severity.HIGH)],
        tool_version="0.1.0",
        manifest="requirements.txt",
    ))
    loc = payload["runs"][0]["results"][0]["locations"][0]
    assert loc["physicalLocation"]["artifactLocation"]["uri"] == "requirements.txt"


def test_sarif_omits_locations_without_manifest() -> None:
    payload = json.loads(to_sarif([_finding(Severity.HIGH)], tool_version="0.1.0"))
    assert "locations" not in payload["runs"][0]["results"][0]


def test_sarif_evidence_preserved_in_properties() -> None:
    payload = json.loads(to_sarif([_finding(Severity.HIGH)], tool_version="0.1.0"))
    props = payload["runs"][0]["results"][0]["properties"]
    assert props["evidence"]["package"] == "requests"
    assert props["severity"] == "HIGH"
