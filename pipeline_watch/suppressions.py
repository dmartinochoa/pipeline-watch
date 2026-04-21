"""Project-scoped suppression file support.

A ``.pipeline-watch/ignore.json`` file next to the baseline can silence
known-good findings without passing ``--skip`` on every CI invocation.
The format is JSON (not YAML) to avoid a runtime dependency — the
fields a security-posture file needs fit comfortably in JSON, and CI
pipelines already have JSON toolchains everywhere.

File shape
----------

.. code-block:: json

    {
      "suppressions": [
        {
          "package": "requests",
          "check_id": "SC-005",
          "reason": "Known-good refactor in v2.31",
          "expires": "2026-06-01"
        },
        {
          "check_id": "SC-014",
          "reason": "We don't care about dep removals in this project"
        }
      ]
    }

Semantics
---------

* A suppression with only ``check_id`` applies to every package.
* A suppression with only ``package`` applies to every check for that
  package (rare, but supported — useful for a vendored fork that ships
  with known irregularities).
* ``reason`` is **required**. A silent ignore is a footgun; making
  operators justify it in the file keeps security reviews honest.
* ``expires`` is optional (``YYYY-MM-DD``). A suppression past its
  expiry is ignored and a warning is emitted so stale suppressions
  don't linger indefinitely.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .output.schema import Finding


@dataclass(frozen=True)
class Suppression:
    reason: str
    package: str | None = None
    check_id: str | None = None
    expires: date | None = None

    def matches(self, finding: Finding) -> bool:
        if self.check_id and finding.check_id != self.check_id:
            return False
        if self.package:
            pkg = str(finding.evidence.get("package") or "")
            if pkg != self.package:
                return False
        return True


@dataclass
class SuppressionLoadResult:
    suppressions: list[Suppression]
    warnings: list[str]


def load_suppressions(path: Path | str) -> SuppressionLoadResult:
    """Read and validate a suppression file.

    Missing file returns an empty result (not an error) — absence of a
    policy file is the common case and shouldn't require a flag. Any
    malformed entry raises ``ValueError`` naming the offending index so
    the operator can find it fast.
    """
    p = Path(path)
    if not p.is_file():
        return SuppressionLoadResult(suppressions=[], warnings=[])

    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p}: not valid JSON ({exc.msg})") from exc
    raw_entries = payload.get("suppressions", [])
    if not isinstance(raw_entries, list):
        raise ValueError(f"{p}: 'suppressions' must be a list")

    today = date.today()
    out: list[Suppression] = []
    warnings: list[str] = []
    for i, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{p}[{i}]: entry must be an object")
        reason = entry.get("reason")
        if not reason or not isinstance(reason, str):
            raise ValueError(f"{p}[{i}]: 'reason' is required and must be a string")
        package = entry.get("package")
        check_id = entry.get("check_id")
        if not package and not check_id:
            raise ValueError(
                f"{p}[{i}]: must specify at least one of 'package' or 'check_id'"
            )
        expires_raw = entry.get("expires")
        expires: date | None = None
        if expires_raw is not None:
            try:
                expires = datetime.strptime(expires_raw, "%Y-%m-%d").date()
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{p}[{i}]: 'expires' must be YYYY-MM-DD; got {expires_raw!r}"
                ) from exc
            if expires < today:
                warnings.append(
                    f"suppression {check_id or '*'}/{package or '*'} expired on "
                    f"{expires.isoformat()} — ignoring (reason: {reason!r})"
                )
                continue
        out.append(Suppression(
            reason=reason, package=package, check_id=check_id, expires=expires,
        ))
    return SuppressionLoadResult(suppressions=out, warnings=warnings)


def apply_suppressions(
    findings: list[Finding],
    suppressions: list[Suppression],
) -> tuple[list[Finding], list[tuple[Finding, Suppression]]]:
    """Partition *findings* into (kept, suppressed-with-rule).

    The *suppressed* list retains the pairing so verbose mode can print
    which rule silenced which finding — "why is this gate passing?" is
    a frequent audit question and an unsuppressed log is useless for it.
    """
    kept: list[Finding] = []
    suppressed: list[tuple[Finding, Suppression]] = []
    for f in findings:
        match = next((s for s in suppressions if s.matches(f)), None)
        if match is None:
            kept.append(f)
        else:
            suppressed.append((f, match))
    return kept, suppressed


def default_suppression_path(baseline_db: Path) -> Path:
    """Return the suppression path that sits next to *baseline_db*.

    Keeping the policy file in the same directory as the baseline keeps
    the project's security posture self-contained — one directory to
    commit, one directory to audit.
    """
    return baseline_db.parent / "ignore.json"
