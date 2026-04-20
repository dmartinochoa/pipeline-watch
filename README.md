<div align="center">

# Pipeline-Watch

[![CI](https://github.com/dmartinochoa/pipeline-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/dmartinochoa/pipeline-watch/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![Coverage](https://img.shields.io/badge/coverage-97%25-brightgreen)](#contributing)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Behavioural companion to [pipeline-check](https://github.com/dmartinochoa/pipeline-check).
Pipeline-watch records a baseline of how your dependencies *actually*
behave and flags every deviation.

[Quick start](#quick-start) · [Signals](#signals) · [How it works](#how-it-works) · [Compliance](#compliance-mapping) · [Contributing](#contributing)

</div>

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
make install    # editable install + dev deps
make test       # pytest — 216 tests, fully offline
make coverage   # pytest + coverage report (min 92%)
make lint       # ruff
make type       # mypy
make check      # lint + type + coverage (same gates CI runs)
```

All network calls flow through injectable fetchers — `pytest` runs
the full suite offline in under two seconds.

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every
push and pull request:

- **test** — pytest + coverage across Python 3.10 / 3.11 / 3.12 / 3.13
  on Ubuntu, plus a 3.12 job on macOS and Windows
- **lint** — `ruff check`
- **typecheck** — `mypy`
- **build** — `python -m build` (sdist + wheel) uploaded as an artifact

Coverage is enforced at **92 %** via `fail_under` in
[`pyproject.toml`](pyproject.toml). A failing gate blocks merge.

## License

MIT — see [LICENSE](LICENSE).
