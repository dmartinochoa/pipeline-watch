<div align="center">

# Pipeline-Watch

**Catch supply-chain attacks as they happen — before CVE databases catch up.**

Behavioural companion to [pipeline-check](https://github.com/dmartinochoa/pipeline-check).
Pipeline-check audits what your pipeline is *configured* to do.
Pipeline-watch records a baseline of how your dependencies *actually*
behave and flags every deviation.

[Quick start](#quick-start) · [Signals](#signals) · [How it works](#how-it-works) · [CI integration](#ci-integration) · [Compliance](#compliance-mapping)

</div>

---

## Why behavioural

Static scanners (pipeline-check, Snyk, Dependabot) read configuration
and tell you what *could* go wrong. They are blind to packages that
look correctly configured but are being used maliciously —
`event-stream`, `ctx`, `node-ipc`, `ua-parser-js`. No signature
scanner flagged those at release; they were caught by humans noticing
behaviour that felt wrong.

Pipeline-watch automates that instinct. It snapshots every package in
your manifest — maintainers, release cadence, install-hook hashes,
dependency graph, publish status — and emits a finding whenever new
observations drift from the baseline.

---

## Quick start

```bash
pip install -e .                  # Python ≥ 3.10

# 1) Teach pipeline-watch what "normal" looks like.
pipeline_watch baseline init --manifest requirements.txt --ecosystem pypi

# 2) Scan on every CI run — findings mark anything that deviated.
pipeline_watch scan deps --manifest requirements.txt --output json

# 3) Inspect what the baseline knows.
pipeline_watch baseline show --package requests
pipeline_watch baseline stats
```

Supported manifests: `requirements.txt` (`--ecosystem pypi`) and
`package.json` (`--ecosystem npm`).

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | No findings at or above `--fail-on` severity (default HIGH) |
| 1 | Gate failed |
| 2 | Scanner failure (registry error, malformed manifest) |
| 3 | Baseline lookup found nothing to return |

`--fail-on` mirrors pipeline-check's gate, so both tools can share a
single CI step.

### Useful flags

```bash
# Gate at MEDIUM instead of HIGH.
pipeline_watch scan deps --manifest requirements.txt --fail-on MEDIUM

# Skip GitHub calls (rate limits / offline). SC-001 and SC-003 downgrade.
pipeline_watch scan deps --manifest requirements.txt --no-github

# Skip the cross-ecosystem probe — disables SC-008.
pipeline_watch scan deps --manifest requirements.txt --no-cross-ecosystem
```

---

## Signals

Twelve behavioural checks compare the live registry + manifest against
the prior snapshot. Every finding ships with the full evidence object
that triggered it.

| ID | Severity | What it catches |
|----|----------|------------------|
| **SC-001** | HIGH · MED | New maintainer with no commits in the source repo |
| **SC-002** | MED | Release published outside the maintainer's 90th-percentile hour window |
| **SC-003** | HIGH | Registry release without a matching git tag upstream |
| **SC-004** | HIGH · MED | Install-hook appeared or its hash changed |
| **SC-005** | LOW | New transitive dependency since the last snapshot |
| **SC-006** | LOW | Manifest pin relaxed from `==x.y.z` to a floating range |
| **SC-007** | HIGH | Two manifest packages within Levenshtein distance ≤ 2 |
| **SC-008** | MED | Same name freshly registered on the other ecosystem (npm ↔ PyPI) |
| **SC-009** | HIGH | Entire maintainer list replaced — no overlap with prior owners |
| **SC-010** | HIGH | Registry's advertised latest dropped below the recorded version |
| **SC-011** | MED | New release after a dormant period > 365 days |
| **SC-012** | HIGH | Latest release is yanked (PyPI) or deprecated (npm) |

Severity downgrades happen when pipeline-watch lacks corroborating
data — e.g. SC-001 falls from HIGH to MEDIUM when `--no-github`
removes the "no prior commits" confirmation.

---

## Real attacks this would have caught

| Incident | Signals that fire |
|----------|-------------------|
| **event-stream (2018)** — co-maintainer added, `__init__.js` payload shipped | SC-001 + SC-004 |
| **node-ipc (2022)** — maintainer publishes politically-motivated wipe payload | SC-002 + SC-004 |
| **ctx / phpass typosquats (2022)** — similarly-named packages with `setup.py` hooks | SC-001 + SC-004 + SC-007 |
| **colors / faker (2022)** — maintainer sabotage release | SC-002 + SC-003 |
| **ua-parser-js (2021)** — hijacked publish token, new owner, install-hook added | SC-001 + SC-004 + SC-009 |
| **Dependency-confusion campaigns** — internal name newly registered upstream | SC-008 |
| **Rollback / unpublish attacks** — attacker re-publishes an older "latest" | SC-010 |
| **`left-pad` / dormant-pkg revival** — long-quiet package suddenly publishes | SC-011 |

A worked example is at [`findings.json`](findings.json).

---

## CI integration

Both tools emit the same JSON envelope so a dashboard can ingest both
through one decoder:

```json
{
  "schema_version": "1.0",
  "tool": "pipeline-watch",
  "tool_version": "0.1.0",
  "module": "supply-chain",
  "score": { "grade": "D", "total": 8 },
  "findings": [
    {
      "tool": "pipeline-watch",
      "module": "supply-chain",
      "severity": "HIGH",
      "signal": "New maintainer 'mallory' published requests 2.32.0.",
      "baseline": "Previous maintainers for requests: alice.",
      "evidence": { "package": "requests", "new_maintainer": "mallory" },
      "remediation": "Freeze the dependency and verify the addition …",
      "check_id": "SC-001",
      "timestamp": "2026-04-20T12:00:00+00:00"
    }
  ]
}
```

`score.grade` is **A** (clean), **B** (LOW only), **C** (MEDIUM or
≥ 2 LOW), **D** (HIGH or CRITICAL) — gate-compatible with
pipeline-check. Typical combined step:

```yaml
- run: pipeline_check --pipeline github --output json --output-file check.json
- run: pipeline_watch scan all --manifest requirements.txt --output-file watch.json
- run: |
    jq -s '.[0].findings + .[1].findings | {tool: "combined", findings: .}' \
       check.json watch.json > findings.json
- uses: actions/upload-artifact@v4
  with: { name: security, path: findings.json }
```

---

## How it works

```
  PyPI / npm / GitHub API
           │
           ▼
   ┌───────────────┐    urllib-based fetchers, swappable in tests.
   │ Providers     │
   │ pypi · npm    │
   │ github        │
   └───────┬───────┘
           ▼
   ┌───────────────┐    12 pure signal functions over (prev, current)
   │ Detector      │    snapshots. A per-package loop records the new
   │ supply_chain  │    snapshot, then SC-007/SC-008 run pairwise.
   └───────┬───────┘
           ▼
   ┌───────────────┐    SQLite — four tables, additive migrations.
   │ Store         │    Project-local .pipeline-watch/baseline.db if the
   │ baseline.db   │    dir exists, else ~/.pipeline-watch/baseline.db.
   └───────┬───────┘
           ▼
   ┌───────────────┐    Rich tables on stdout; stable JSON envelope.
   │ Formatter     │
   └───────────────┘
```

**Zero infrastructure.** Standard-library `sqlite3`, stdlib
`urllib.request`, three dependencies (`click`, `rich`,
`Levenshtein`). No Redis, no Postgres, no message queue, no ORM — one
binary, one SQLite file.

**Baseline path resolution.** `./.pipeline-watch/baseline.db` when
that directory exists at cwd (the repo-local case); otherwise
`~/.pipeline-watch/baseline.db`.

---

## Baseline commands

```bash
pipeline_watch baseline init  --manifest PATH --ecosystem {pypi|npm}
pipeline_watch baseline show  [--package NAME] [--ecosystem ...]
pipeline_watch baseline reset --scope {package:NAME|job:REPO:JOB|org:NAME}
pipeline_watch baseline stats
```

---

## Provider support

| Ecosystem | Module | Status |
|-----------|--------|--------|
| PyPI | supply-chain | ✅ full scan |
| npm | supply-chain | ✅ full scan |
| GitHub REST (tags + commit history) | supply-chain probe | ✅ |
| GitHub Actions logs | ci-runtime | schema ready |
| GitLab pipelines | ci-runtime | schema ready |
| Jenkins | ci-runtime | schema ready |
| GitHub / GitLab audit logs | vcs-audit | schema ready |

"Schema ready" means the SQLite tables (`pipeline_runs`,
`audit_events`) already hold the columns the detector needs —
adding the scanner is additive, no migration required.

---

## Compliance mapping

Pipeline-watch does not *satisfy* these controls — it supplies
evidence for them.

| Signal | SLSA level | OWASP Top 10 CI/CD | ISO 27001 Annex A |
|--------|-----------|---------------------|--------------------|
| SC-001 | L3 — provenance | CICD-SEC-4 | A.5.17 · A.5.24 |
| SC-002 | L2 — build integrity | CICD-SEC-5 | A.5.24 |
| SC-003 | L3 — source integrity | CICD-SEC-6 | A.5.23 · A.8.30 |
| SC-004 | L2 — build integrity | CICD-SEC-3 | A.8.28 |
| SC-005 | L2 — dependency tracking | CICD-SEC-3 | A.8.26 |
| SC-006 | L1 — reproducibility | CICD-SEC-3 | A.8.9 |
| SC-007 | operator-driven | CICD-SEC-3 | A.5.22 · A.8.1 |
| SC-008 | operator-driven | CICD-SEC-3 | A.5.22 |
| SC-009 | L3 — provenance | CICD-SEC-4 | A.5.16 · A.5.24 |
| SC-010 | L2 — build integrity | CICD-SEC-6 | A.8.25 |
| SC-011 | L2 — build integrity | CICD-SEC-3 | A.5.24 |
| SC-012 | L1 — dependency tracking | CICD-SEC-3 | A.8.9 |

---

## Contributing

```bash
make install   # editable install + dev deps
make test      # pytest
make lint      # ruff
make type      # mypy
```

All network calls flow through injectable fetchers — `pytest` runs
the full suite offline.

## License

MIT — see [LICENSE](LICENSE).
