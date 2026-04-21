"""SARIF 2.1.0 output for pipeline-watch findings.

GitHub Code Scanning, Azure DevOps, and most IDE integrations consume
SARIF (Static Analysis Results Interchange Format). Emitting SARIF
alongside our native JSON lets a pipeline-watch run annotate a PR
directly without dashboard plumbing.

Spec reference: https://docs.oasis-open.org/sarif/sarif/v2.1.0/.

Mapping decisions
-----------------
* One SARIF ``run`` per invocation; the tool driver advertises every
  SC-XXX rule so a viewer shows full descriptions on hover even when a
  given run didn't fire that rule.
* Severity → SARIF ``level`` — CRITICAL/HIGH → ``error`` (blocks CI),
  MEDIUM → ``warning``, LOW → ``note``.
* The manifest path is the result's physical location so findings
  annotate the exact file reviewers edit. pipeline-watch doesn't know
  the line number of an individual dependency, so we leave ``region``
  out rather than guessing.
* Evidence is carried in ``properties`` with the same keys as the
  native JSON, so downstream tools that already read our envelope
  don't need a second decoder.
"""
from __future__ import annotations

import json
from typing import Any

from ..detectors.supply_chain import SIGNAL_CATALOGUE
from .schema import Finding, Severity

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/"
    "Schemata/sarif-schema-2.1.0.json"
)

_LEVEL_BY_SEVERITY: dict[Severity, str] = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
}


def _rules() -> list[dict[str, Any]]:
    """Advertise the full SC-XXX rule catalogue in the tool driver."""
    return [
        {
            "id": sc_id,
            "name": slug,
            "shortDescription": {"text": description},
            "fullDescription": {"text": description},
            "defaultConfiguration": {"level": _LEVEL_BY_SEVERITY[severity]},
            "properties": {
                "severity": severity.value,
                "tags": ["supply-chain", "pipeline-watch"],
            },
        }
        for sc_id, (slug, severity, description) in SIGNAL_CATALOGUE.items()
    ]


def _result(finding: Finding, *, manifest: str | None) -> dict[str, Any]:
    level = _LEVEL_BY_SEVERITY.get(finding.severity, "warning")
    result: dict[str, Any] = {
        "ruleId": finding.check_id or "SC-000",
        "level": level,
        "message": {
            "text": f"{finding.signal} Baseline: {finding.baseline} "
                    f"Remediation: {finding.remediation}",
        },
        "properties": {
            "severity": finding.severity.value,
            "baseline": finding.baseline,
            "remediation": finding.remediation,
            "timestamp": finding.timestamp,
            "evidence": finding.evidence,
        },
    }
    if manifest:
        result["locations"] = [{
            "physicalLocation": {
                "artifactLocation": {"uri": manifest},
            },
        }]
    return result


def to_sarif(
    findings: list[Finding],
    *,
    tool_version: str,
    manifest: str | None = None,
) -> str:
    """Render *findings* as a SARIF 2.1.0 log (pretty-printed JSON)."""
    log = {
        "version": SARIF_VERSION,
        "$schema": SARIF_SCHEMA,
        "runs": [{
            "tool": {
                "driver": {
                    "name": "pipeline-watch",
                    "version": tool_version,
                    "informationUri": "https://github.com/dmartinochoa/pipeline-watch",
                    "rules": _rules(),
                },
            },
            "results": [_result(f, manifest=manifest) for f in findings],
        }],
    }
    return json.dumps(log, indent=2)
