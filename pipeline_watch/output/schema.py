"""Findings schema — the shared contract with pipeline-check.

pipeline-watch and pipeline-check are intentionally shaped so a single
dashboard can ingest JSON from both. The ``tool`` field distinguishes
the producer; every other field follows the same conventions (severity
values, A/B/C/D score scale, ISO8601 timestamps).

Signal / baseline are the watch-specific fields. Where pipeline-check
says *"this configuration violates CIS-1.2.3"*, pipeline-watch says
*"this observation deviates from the baseline for this scope"* — the
``signal`` describes the deviation, ``baseline`` describes normal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

JSON_SCHEMA_VERSION = "1.0"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def severity_rank(s: Severity) -> int:
    return _SEVERITY_RANK[s]


class Module(str, Enum):
    SUPPLY_CHAIN = "supply-chain"
    CI_RUNTIME = "ci-runtime"
    VCS_AUDIT = "vcs-audit"


TOOL_NAME = "pipeline-watch"


@dataclass
class Finding:
    """Single behavioural deviation flagged by a detector.

    Field names match the schema documented in the README verbatim so
    a dashboard can consume pipeline-check and pipeline-watch output
    through one JSON decoder.
    """
    module: Module
    severity: Severity
    signal: str
    baseline: str
    remediation: str
    evidence: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    #: Optional stable identifier for the detector rule (e.g. ``SC-001``).
    #: Populated by detectors so the CLI can filter / suppress by ID.
    check_id: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        # Ordering matches the README's documented schema so the emitted
        # JSON is readable by a human scanning it in a diff.
        return {
            "tool": TOOL_NAME,
            "module": self.module.value,
            "severity": self.severity.value,
            "score": _severity_to_score(self.severity),
            "signal": self.signal,
            "baseline": self.baseline,
            "evidence": self.evidence,
            "timestamp": self.timestamp,
            "remediation": self.remediation,
            "check_id": self.check_id,
        }


def _severity_to_score(sev: Severity) -> str:
    """Map a single finding's severity to its per-finding grade letter.

    An aggregate grade across a run is computed by :func:`score_from_findings`.
    """
    return {
        Severity.LOW: "B",
        Severity.MEDIUM: "C",
        Severity.HIGH: "D",
        Severity.CRITICAL: "D",
    }[sev]


def score_from_findings(findings: list[Finding]) -> dict[str, Any]:
    """Reduce a list of findings to an aggregate grade.

    The grade mapping is the one documented in the README:

    * **A** — no findings
    * **B** — LOW severity findings only
    * **C** — MEDIUM or multiple LOW findings
    * **D** — HIGH or CRITICAL findings (gate-compatible with pipeline-check)

    The shape of the returned dict mirrors pipeline-check's
    ``ScoreResult`` so a dashboard's summary-card renderer can reuse
    the same code path.
    """
    summary: dict[str, dict[str, int]] = {
        s.value: {"count": 0} for s in Severity
    }
    for f in findings:
        summary[f.severity.value]["count"] += 1

    crit = summary[Severity.CRITICAL.value]["count"]
    high = summary[Severity.HIGH.value]["count"]
    med = summary[Severity.MEDIUM.value]["count"]
    low = summary[Severity.LOW.value]["count"]

    if not findings:
        grade = "A"
    elif crit or high:
        grade = "D"
    elif med or low >= 2:
        grade = "C"
    else:
        grade = "B"
    return {
        "grade": grade,
        "summary": summary,
        "total": len(findings),
    }


def findings_envelope(
    findings: list[Finding],
    *,
    tool_version: str,
    module: str | None = None,
) -> dict[str, Any]:
    """Wrap *findings* in the top-level payload emitted to JSON output.

    The envelope carries ``schema_version`` (bumped on breaking format
    changes) and ``tool_version`` so downstream consumers can version-
    branch without guessing. Identical in spirit to pipeline-check's
    report_json envelope.
    """
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": tool_version,
        "module": module,
        "score": score_from_findings(findings),
        "findings": [f.to_dict() for f in findings],
    }


def dumps(payload: dict[str, Any]) -> str:
    """JSON-dump helper with stable indentation — one place to change the format."""
    return json.dumps(payload, indent=2, sort_keys=False)


def validate_finding_dict(d: dict[str, Any]) -> list[str]:
    """Return a list of validation errors for *d*; empty means valid.

    Used by tests and by any import path that accepts JSON produced
    by another process (e.g. a future ``pipeline-watch ingest`` that
    reads findings.json from a runner).
    """
    errors: list[str] = []
    required = ("tool", "module", "severity", "signal", "baseline",
                "evidence", "timestamp", "remediation")
    for key in required:
        if key not in d:
            errors.append(f"missing field: {key}")
    if d.get("tool") not in (TOOL_NAME, "pipeline-check"):
        errors.append(f"unexpected tool: {d.get('tool')!r}")
    if "severity" in d and d["severity"] not in {s.value for s in Severity}:
        errors.append(f"invalid severity: {d['severity']!r}")
    if "module" in d and d["module"] not in {m.value for m in Module}:
        errors.append(f"invalid module: {d['module']!r}")
    if "evidence" in d and not isinstance(d["evidence"], dict):
        errors.append("evidence must be an object")
    return errors
