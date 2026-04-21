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

## Why pipeline-watch?

Static scanners tell you which versions have CVEs. Pipeline-watch tells
you when a package *changed the way it behaves* — a new maintainer, an
install hook that wasn't there last week, a release published from an
unusual timezone. Most supply-chain compromises show up as behavioural
drift days or weeks before a CVE is filed.

The baseline is the whole point: a signal only fires when the current
observation differs from what was normal for **this specific package in
this specific repo**. That's what keeps the false-positive rate low
enough to run on every CI build.

---

## Quick start

### Install

```bash
pip install -e .                  # Python ≥ 3.10
```

Three runtime deps — `click`, `rich`, `Levenshtein`. No services to
stand up, no API keys required (GitHub probes work unauthenticated;
set `GITHUB_TOKEN` if you hit the rate limit).

### One-minute walkthrough

```bash
# 1) Teach pipeline-watch what "normal" looks like for this repo.
#    Run this once, then again any time you intentionally bump a pin.
pipeline_watch baseline init --manifest requirements.txt --ecosystem pypi

# 2) Scan on every CI run — findings mark anything that deviated.
#    Exit 1 when a HIGH (or above) finding appears; pipe --output json
#    to a dashboard or to pipeline-check's ingester.
pipeline_watch scan deps --manifest requirements.txt --output json

# 3) Inspect what the baseline knows.
pipeline_watch baseline show --package requests
pipeline_watch baseline stats
```

Supported manifests: `requirements.txt` (`--ecosystem pypi`) and
`package.json` (`--ecosystem npm`).

### Typical CI wiring

```yaml
# .github/workflows/deps.yml
- run: pipeline_watch baseline init --manifest requirements.txt
  if:  github.event_name == 'workflow_dispatch'   # manual re-baseline
- run: pipeline_watch scan deps --manifest requirements.txt --fail-on HIGH
```

The baseline SQLite file is committed to the repo (it lives under
`.pipeline-watch/`), so every clone sees the same "normal". Re-baseline
deliberately when you intend to accept new behaviour — a PR that bumps a
dependency should also update the baseline file.

### Exit codes

| Code | Meaning | When you'll see it |
|------|---------|---------------------|
| 0 | Gate passed | No finding at or above `--fail-on` severity (default HIGH) |
| 1 | Gate failed | One or more findings at or above the threshold |
| 2 | Scanner failure | Registry API error, malformed manifest, unreadable baseline |
| 3 | Lookup empty | `baseline show` / `baseline reset` found nothing to act on |

`--fail-on` mirrors pipeline-check's gate, so both tools can share a
single CI step.

### Useful flags

```bash
# Ecosystem is inferred from the manifest filename:
#   requirements*.txt → pypi
#   package.json      → npm
# Pass --ecosystem explicitly for non-standard filenames.

# Gate at MEDIUM instead of HIGH — stricter CI.
pipeline_watch scan deps --manifest requirements.txt --fail-on MEDIUM

# Skip GitHub calls (rate limits / offline).
#   SC-001 downgrades HIGH → MED (no commit-history confirmation).
#   SC-003 is skipped entirely (needs tag list).
pipeline_watch scan deps --manifest requirements.txt --no-github

# Skip the cross-ecosystem probe — disables SC-008 only.
pipeline_watch scan deps --manifest requirements.txt --no-cross-ecosystem

# Suppress noisy checks in CI without disabling them project-wide.
# Unknown IDs error — protects against typos re-enabling a signal.
pipeline_watch scan deps --manifest requirements.txt --skip SC-005,SC-014

# Emit SARIF 2.1.0 for GitHub Code Scanning / Azure DevOps annotations.
pipeline_watch scan deps --manifest requirements.txt \
    --output sarif --output-file findings.sarif

# Self-contained HTML report — email it to a security team.
pipeline_watch scan deps --manifest requirements.txt \
    --output html --output-file report.html

# '-' as --output-file writes to stdout (useful in shell pipelines).
pipeline_watch scan deps --manifest requirements.txt \
    --output json --output-file - | jq '.findings[].check_id'

# Accept the current registry state as the new normal — pairs with a
# re-baseline PR. Without this flag, findings persist across CI runs
# until a human reviews them (same deviation re-flags next run).
pipeline_watch scan deps --manifest requirements.txt --baseline-update

# Merge multiple reports into one envelope — dedupes on
# (check_id, signal, package). Useful in matrix jobs.
pipeline_watch ingest frontend.json backend.json \
    --output html --output-file combined.html

# One-command diagnostics for bug reports.
pipeline_watch doctor

# Quiet mode — stderr silent, exit code still reflects the gate.
pipeline_watch --quiet scan deps --manifest requirements.txt

# Verbose — [debug] lines on stderr showing every fetch / snapshot write.
pipeline_watch --verbose scan deps --manifest requirements.txt

# Point at a specific baseline file (useful for multi-manifest repos).
pipeline_watch --baseline-db ./.pipeline-watch/frontend.db \
    scan deps --manifest frontend/package.json

# Discover what signal IDs are valid for --skip.
pipeline_watch signals              # rich table
pipeline_watch signals -o json      # machine-readable
```

---

## Signals

Eighteen behavioural checks compare the live registry + manifest against
the prior snapshot. Every finding ships with the full evidence object
that triggered it — raw numbers, before/after values, the exact package
names and versions — so a reviewer can decide without re-running the
scan.

| ID | Severity | What it catches | Needs |
|----|----------|------------------|-------|
| **SC-001** | HIGH · MED | New maintainer with no commits in the source repo | GitHub probe |
| **SC-002** | MED | Release published outside the maintainer's 90th-percentile hour window | ≥5 prior releases |
| **SC-003** | HIGH | Registry release without a matching git tag upstream | GitHub probe |
| **SC-004** | HIGH · MED | Install-hook appeared or its hash changed | Prior snapshot |
| **SC-005** | LOW | New transitive dependency since the last snapshot | Prior snapshot |
| **SC-006** | LOW | Manifest pin relaxed from `==x.y.z` to a floating range | Prior snapshot |
| **SC-007** | HIGH | Two manifest packages within Levenshtein distance ≤ 2 | — (pairwise) |
| **SC-008** | MED | Same name freshly registered on the other ecosystem (npm ↔ PyPI) | Cross-eco probe |
| **SC-009** | HIGH | Entire maintainer list replaced — no overlap with prior owners | Prior snapshot |
| **SC-010** | HIGH | Registry's advertised latest dropped below the recorded version | Prior snapshot |
| **SC-011** | MED | New release after a dormant period > 365 days | Prior `release_uploaded_at` |
| **SC-012** | HIGH | Latest release is yanked (PyPI) or deprecated (npm) | — (current-state) |
| **SC-013** | MED | Major version jumped ≥ 2 in a single release | Prior snapshot |
| **SC-014** | LOW | A dependency recorded in the prior snapshot silently disappeared | Prior snapshot |
| **SC-015** | LOW | Release landed on a weekday the maintainer has never used before | ≥5 prior releases |
| **SC-016** | MED | Registry advertises a pre-release (alpha/beta/rc/dev) as latest | — (current-state) |
| **SC-017** | MED | ≥3 releases within 24h when historical cadence is slow | ≥4 prior releases |
| **SC-020** | HIGH | Maintainer kept the same display name but the email changed | Prior snapshot |

**Confidence downgrades.** When corroborating data is unavailable,
severity drops rather than the finding disappearing. Example: SC-001
falls from HIGH to MEDIUM under `--no-github` because we can still see
the new maintainer, we just can't confirm they have no commits.

**First-run behaviour.** Signals that need a prior snapshot silently
return no findings the first time a package is seen; that's by design.
Signals marked *current-state* (SC-007, SC-008, SC-012, SC-016) fire
immediately.

### Reading a finding

```json
{
  "check_id": "SC-004",
  "severity": "HIGH",
  "signal": "requests 2.32.0 install-script hash changed.",
  "baseline": "Previous hash: 9f3b…ea21 (2 releases ago).",
  "evidence": {
    "package": "requests",
    "previous_hash": "9f3b…ea21",
    "current_hash": "1c8d…4aa2",
    "has_install_script": true
  },
  "remediation": "Diff the sdist between versions …",
  "timestamp": "2026-04-21T14:02:33+00:00"
}
```

`check_id` and `severity` are stable; `evidence` keys are stable per
signal (adding new keys is backwards compatible, renaming is not). The
terminal renderer shows the same data as a Rich panel — prefer `--output
json` for CI, `terminal` for humans, `both` when you want both.

---

## How it works

```
  PyPI / npm / GitHub API
           │
           ▼
   ┌───────────────┐    urllib-based fetchers, swappable in tests.
   │ Providers     │    One public fetch call per package per run;
   │ pypi · npm    │    GitHub calls are opt-in per --no-github flag.
   │ github        │
   └───────┬───────┘
           ▼
   ┌───────────────┐    18 pure signal functions over (prev, current)
   │ Detector      │    snapshots. A per-package loop records the new
   │ supply_chain  │    snapshot, then SC-007/SC-008 run pairwise.
   └───────┬───────┘
           ▼
   ┌───────────────┐    SQLite — four tables, additive migrations.
   │ Store         │    Project-local .pipeline-watch/baseline.db if the
   │ baseline.db   │    dir exists, else ~/.pipeline-watch/baseline.db.
   └───────┬───────┘
           ▼
   ┌───────────────┐    Rich tables on stdout; stable JSON envelope
   │ Formatter     │    compatible with pipeline-check's reporter.
   └───────────────┘
```

**Zero infrastructure.** Standard-library `sqlite3`, stdlib
`urllib.request`, three dependencies (`click`, `rich`, `Levenshtein`).
No Redis, no Postgres, no message queue, no ORM — one binary, one
SQLite file.

**Design principles.**

- **Pure signal functions** — each detector is `(prev, current, …) →
  list[Finding]`, so unit-testing a signal means constructing two
  snapshots and calling the function. See `tests/test_supply_chain.py`
  for ~60 examples.
- **Injectable fetchers** — providers expose a module-level `_fetcher`
  the test suite swaps for a fake. Every network path is exercised
  without touching the real registry.
- **Evidence over inference** — detectors ship the raw numbers that
  justified the finding; consumers are expected to check them before
  acting. No detector "knows" a compromise happened.
- **Additive schema** — SQLite migrations only ever add columns / tables,
  never rewrite. An older baseline file keeps working when the detector
  grows a new signal (it just won't have historical data for the new
  dimension yet).

### Baseline database

The store lives in `baseline.db`, a SQLite file with four tables:

| Table | Purpose |
|-------|---------|
| `package_snapshots` | One row per (ecosystem, package, version) observation — the substrate for SC-001 through SC-016 |
| `package_stats` | Precomputed per-package distributions (release hour percentiles, etc.) — refreshed after every `scan` |
| `pipeline_runs` | Schema-ready for the ci-runtime module (build durations, step failures) |
| `audit_events` | Schema-ready for the vcs-audit module (permission changes, force-pushes) |

**Path resolution.** `./.pipeline-watch/baseline.db` when that directory
exists at cwd (the repo-local case); otherwise
`~/.pipeline-watch/baseline.db`. Override with `--baseline-db PATH`.
Commit the repo-local file so CI and every developer see the same
"normal".

**Re-baselining.** `baseline init` is idempotent — it upserts snapshots
without erasing history. To clear data for a specific scope use
`baseline reset --scope package:NAME` (or `org:NAME`, `job:REPO:JOB`).

---

## Baseline commands

| Command | Purpose |
|---------|---------|
| `baseline init --manifest PATH [--ecosystem …]` | Populate the baseline from a manifest without emitting findings. Run once per project; re-run when you deliberately accept new behaviour. |
| `baseline diff --manifest PATH [--ecosystem …]` | Dry-run comparison of the registry against the baseline — field-level diff, no writes, no gate. Use it to preview a re-baseline PR. |
| `baseline show [--package NAME] [--ecosystem …]` | Render the latest snapshot(s). With `--package` shows every field for one package; without, lists all recorded packages. |
| `baseline reset --scope {package:NAME\|job:REPO:JOB\|org:NAME}` | Delete every record for *scope*. Useful when a package is intentionally replaced and you want the next scan to treat it as new. |
| `baseline stats` | Show every precomputed statistic (release-hour mean / stddev, sample counts). |

```bash
# Inspect a single package fully.
pipeline_watch baseline show --package requests

# List all npm packages in the baseline.
pipeline_watch baseline show --ecosystem npm

# Start fresh for one package (e.g. you swapped requests for httpx).
pipeline_watch baseline reset --scope package:requests
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
| SC-013 | L2 — build integrity | CICD-SEC-6 | A.8.25 |
| SC-014 | L2 — dependency tracking | CICD-SEC-3 | A.8.26 |
| SC-015 | L2 — build integrity | CICD-SEC-5 | A.5.24 |
| SC-016 | L1 — reproducibility | CICD-SEC-3 | A.8.9 |
| SC-017 | L2 — build integrity | CICD-SEC-5 | A.5.24 |
| SC-020 | L3 — provenance | CICD-SEC-4 | A.5.17 · A.5.24 |

---

## pre-commit integration

A `.pre-commit-hooks.yaml` manifest ships with the package, so
[`pre-commit`](https://pre-commit.com/) users can wire pipeline-watch
into the same framework they use for `ruff` / `black`:

```yaml
# .pre-commit-config.yaml
- repo: https://github.com/dmartinochoa/pipeline-watch
  rev: v0.1.0
  hooks:
    - id: pipeline-watch-scan
```

`pipeline-watch-scan` runs on every commit touching `requirements*.txt`
or `package.json`. `pipeline-watch-baseline-init` is `stage: manual` so
it only runs on `pre-commit run --hook-stage manual
pipeline-watch-baseline-init` — the explicit re-baseline step.

---

## Suppression file

Commit a policy file at `.pipeline-watch/ignore.json` (next to the
baseline) to silence known-good findings without maintaining a
`--skip` list in CI:

```json
{
  "suppressions": [
    {
      "package": "requests",
      "check_id": "SC-005",
      "reason": "v2.31 intentionally pulled in charset-normalizer",
      "expires": "2026-12-31"
    },
    {
      "check_id": "SC-014",
      "reason": "Project policy: removed deps are reviewed at PR time"
    }
  ]
}
```

Rules:
- `reason` is **required** — silent ignores hide the "why" an auditor
  needs. Making operators justify the suppression in-tree keeps the
  policy honest.
- A suppression with only `check_id` applies to every package.
- `expires` (`YYYY-MM-DD`) is optional; past-dated entries are ignored
  with a warning so dead suppressions surface themselves.
- Unknown suppression fields are accepted silently to allow custom
  annotations (e.g. a Jira ticket). Invalid JSON or missing `reason`
  exits 2.

Bypass the file for one run with `--no-ignore`. Point at an alternate
file with `--ignore-file PATH`.

A JSON Schema ships at
[`pipeline_watch/suppressions.schema.json`](pipeline_watch/suppressions.schema.json)
— point your editor at it for inline validation:

```json
// .vscode/settings.json
{
  "json.schemas": [
    {
      "fileMatch": [".pipeline-watch/ignore.json"],
      "url": "./pipeline_watch/suppressions.schema.json"
    }
  ]
}
```

---

## Troubleshooting

**"baseline is empty; treating this run as a fresh init."** You ran
`scan deps` without `baseline init` first. The scan now records
snapshots instead of flagging against nothing; re-run after a real
release to see findings.

**SC-001 / SC-003 report `source_repo_missing`.** The package's registry
metadata didn't advertise a GitHub URL. pipeline-watch won't guess — add
`Home-page` / `Project-URL: Source` to the upstream project's metadata,
or accept that these two signals can't run for that package.

**Rate-limited by GitHub.** Set `GITHUB_TOKEN` in the environment; the
probe reads it automatically. Or pass `--no-github` and accept the
SC-001 / SC-003 downgrades.

**Windows box-drawing glyphs render as `?`.** The CLI reconfigures
stdout/stderr with `errors='replace'` to avoid crashes on cp1252
consoles. Switch to Windows Terminal + UTF-8 (`chcp 65001`) for full
glyphs.

**Baseline keeps growing.** Expected — it's an append-log of snapshots.
A typical project sits at a few hundred KB even after a year. Use
`baseline reset --scope org:NAME` to drop everything under one org.

---

## Contributing

```bash
make install    # editable install + dev deps
make test       # pytest — 291 tests, fully offline
make coverage   # pytest + coverage report (min 92%)
make lint       # ruff
make type       # mypy
make check      # lint + type + coverage (same gates CI runs)
```

All network calls flow through injectable fetchers — `pytest` runs
the full suite offline in under two seconds.

### Adding a new signal

1. Add the ID to `SIGNAL_IDS` in
   [`pipeline_watch/detectors/supply_chain.py`](pipeline_watch/detectors/supply_chain.py).
2. Write a pure `signal_*` function over `(prev, current, …)` that
   returns `list[Finding]`. Use an existing signal as a template —
   SC-004 (hash change) and SC-013 (version jump) are both short.
3. Wire it into the `scan()` orchestrator alongside the other
   `findings += signal_*(…)` lines.
4. Add tests covering fires / doesn't-fire / first-run paths.
5. Extend the README signal table and compliance mapping.

Evidence keys should be stable — if you need to rename one, add the new
key alongside the old for one release before removing.

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
