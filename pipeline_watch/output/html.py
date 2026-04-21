"""Self-contained HTML report.

The output is a single HTML file that renders the same summary-card +
per-finding detail structure as the terminal report, but is readable
by people who live in email or ticketing systems rather than CI logs.

Design constraints
------------------
* **No JavaScript, no external assets.** The file has to load the
  same way off a mail attachment as off a pipeline artefact. All
  styling is inline CSS in ``<style>``.
* **Every user-supplied string is HTML-escaped** via :func:`html.escape`.
  Package names, maintainer emails, and evidence blobs all reach this
  renderer from the public registry — treating them as untrusted input
  is the only sane default.
* **Deterministic output.** No timestamps beyond those in the findings
  themselves, no random IDs. A given input produces a byte-identical
  file so diff-based audit workflows (``git diff findings.html``) stay
  usable.
"""
from __future__ import annotations

import html
import json
from typing import Any

from .schema import Finding, Severity, score_from_findings, severity_rank

_STYLE = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
       Roboto, Ubuntu, sans-serif; color: #1d1d1f; background: #fafafa;
       margin: 0; padding: 2rem; }
main { max-width: 960px; margin: 0 auto; }
h1 { margin: 0 0 0.25rem 0; font-size: 1.5rem; }
.sub { color: #6e6e73; margin: 0 0 1.5rem 0; }
.summary { display: flex; gap: 0.5rem; flex-wrap: wrap;
           padding: 1rem; border-radius: 12px;
           background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,0.06);
           margin-bottom: 1.5rem; }
.grade { font-size: 2rem; font-weight: 700; padding: 0 1rem;
         border-right: 1px solid #e5e5ea; line-height: 1; }
.grade-A { color: #1f9d55; }
.grade-B { color: #1f9d55; }
.grade-C { color: #b58900; }
.grade-D { color: #d73a49; }
.counts { display: flex; gap: 1rem; align-items: center; }
.count-pill { padding: 0.25rem 0.75rem; border-radius: 999px;
              font-size: 0.85rem; font-weight: 500; }
.sev-CRITICAL, .sev-HIGH { background: #fdecea; color: #b71c1c; }
.sev-MEDIUM { background: #fff8e1; color: #8a6d00; }
.sev-LOW { background: #e3f2fd; color: #0b4f8a; }
.finding { background: #fff; border-radius: 12px; padding: 1rem 1.25rem;
           box-shadow: 0 1px 2px rgba(0,0,0,0.06);
           margin-bottom: 0.75rem;
           border-left: 4px solid transparent; }
.finding.sev-CRITICAL, .finding.sev-HIGH { border-color: #d73a49; }
.finding.sev-MEDIUM { border-color: #dba500; }
.finding.sev-LOW { border-color: #2188ff; }
.finding h3 { margin: 0 0 0.25rem 0; font-size: 1rem;
              display: flex; align-items: center; gap: 0.5rem; }
.finding h3 .id { font-family: 'SFMono-Regular', Menlo, monospace;
                  background: #f1f2f4; padding: 0.1rem 0.4rem;
                  border-radius: 4px; font-size: 0.85rem; }
.finding .meta { color: #6e6e73; font-size: 0.85rem;
                 margin-bottom: 0.5rem; }
.finding dl { margin: 0.5rem 0 0 0; }
.finding dt { font-weight: 600; margin-top: 0.5rem; }
.finding dd { margin: 0.1rem 0 0 0; font-family: 'SFMono-Regular',
              Menlo, monospace; font-size: 0.85rem;
              word-break: break-all; }
.finding pre { background: #f6f7f8; padding: 0.5rem 0.75rem;
               border-radius: 6px; margin: 0.25rem 0 0 0;
               font-size: 0.8rem; overflow-x: auto; }
.empty { text-align: center; padding: 3rem; color: #6e6e73; }
""".strip()


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pretty_evidence(evidence: dict[str, Any]) -> str:
    if not evidence:
        return ""
    rendered = html.escape(json.dumps(evidence, indent=2, sort_keys=True))
    return f"<pre>{rendered}</pre>"


def to_html(
    findings: list[Finding],
    *,
    tool_version: str,
) -> str:
    """Render *findings* as a single self-contained HTML document."""
    score = score_from_findings(findings)
    grade = score["grade"]
    ordered = sorted(
        findings,
        key=lambda f: (-severity_rank(f.severity), f.check_id),
    )
    count_pills = []
    for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        n = score["summary"].get(severity.value, {}).get("count", 0)
        if n:
            count_pills.append(
                f'<span class="count-pill sev-{severity.value}">{n} {severity.value}</span>'
            )

    body_parts: list[str] = []
    if not ordered:
        body_parts.append(
            '<div class="empty">No behavioural deviations detected '
            'against the baseline.</div>'
        )
    for f in ordered:
        sev = f.severity.value
        body_parts.append(
            f'<div class="finding sev-{sev}">'
            f'<h3><span class="id">{_escape(f.check_id or "-")}</span>'
            f'<span>{_escape(f.signal)}</span></h3>'
            f'<div class="meta">'
            f'<span class="count-pill sev-{sev}">{sev}</span> · '
            f'{_escape(f.module.value)} · {_escape(f.timestamp)}'
            f'</div>'
            f'<dl>'
            f'<dt>Baseline</dt><dd>{_escape(f.baseline)}</dd>'
            f'<dt>Remediation</dt><dd>{_escape(f.remediation)}</dd>'
            f'<dt>Evidence</dt><dd>{_pretty_evidence(f.evidence)}</dd>'
            f'</dl>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pipeline-watch report</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<h1>pipeline-watch report</h1>
<p class="sub">tool version {_escape(tool_version)} · \
{len(findings)} finding(s)</p>
<div class="summary">
  <div class="grade grade-{grade}">{grade}</div>
  <div class="counts">{''.join(count_pills) or '<span class="count-pill sev-LOW">clean</span>'}</div>
</div>
{''.join(body_parts)}
</main>
</body>
</html>
"""
