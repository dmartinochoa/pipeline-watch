<div align="center">

# Pipeline-Watch

**Catch supply-chain attacks *as they happen* — before CVE databases catch up.**

The runtime companion to [pipeline-check](https://github.com/dmartinochoa/pipeline-check). Where pipeline-check audits what your pipeline is *configured* to do, pipeline-watch watches what it actually *does* — and flags every deviation from the learned baseline.

**8 behavioural signals** across a zero-infrastructure SQLite baseline — findings compatible with pipeline-check so one dashboard consumes both tools

[Quick start](#quick-start) |
[How it works](#behavioural-vs-static-analysis) |
[Zero-infrastructure](#zero-infrastructure) |
[Pairing with pipeline-check](#pairing-with-pipeline-check) |
[Real attacks caught](#real-attacks-this-would-have-caught) |
[Compliance](#compliance-mapping)

</div>

---

## Behavioural vs static analysis

Static scanners (pipeline-check, Snyk, Dependabot) read your configuration and tell you what *could* go wrong: a missing artifact signature, a known-vulnerable dependency, a misconfigured IAM role. They are indispensable — and they are blind to everything that looks correctly configured but is being used maliciously.

Most supply-chain attacks live in that blind spot. `event-stream` kept the same `package.json` the day it was weaponised. `ctx` on PyPI shipped malicious code through an account whose credentials had quietly changed hands. `node-ipc`'s maintainer stayed the maintainer — he just started shipping different code. No signature scanner flagged these at release; they were caught by humans noticing behaviour that felt wrong.

Pipeline-watch automates that instinct. It records a baseline of normal behaviour for every package, job, and audit actor — maintainer lists, release cadence, install-hook hashes, runtime network destinations — and emits a finding whenever new observations drift from it. You use it alongside a static scanner, not instead: pipeline-check checks the config, pipeline-watch watches the runtime.

---

## Zero-infrastructure

Pipeline-watch has **no server, no hosted service, and no network dependencies beyond the public registry APIs** (PyPI, npm, GitHub). All baseline state lives in a single SQLite file:

| Priority | Path | When to use |
|----------|------|-------------|
| 1 | `./.pipeline-watch/baseline.db` | Per-repo baseline — tracked intent lives with the code |
| 2 | `~/.pipeline-watch/baseline.db` | Global fallback — shared across all your repos |

This is deliberate. Security tools that require infrastructure to stand up don't get stood up. One binary, one SQLite file, no sidecars — drop it into CI and it runs. The schema carries four tables (`package_snapshots`, `pipeline_runs`, `audit_events`, `baseline_stats`); there's nothing to break.

```bash
# The full list of things you need to install
pip install pipeline_watch
```

No Redis. No Postgres. No message queue. No ORM. Just `sqlite3` from the standard library and three HTTP clients against public APIs.

---

## Quick start

```bash
pip install -e .                  # Python >= 3.10

# 1) Teach pipeline-watch what "normal" looks like for your repo.
pipeline_watch baseline init --manifest requirements.txt --ecosystem pypi

# 2) Run scans in CI — each run flags anything that deviates.
pipeline_watch scan deps --manifest requirements.txt --ecosystem pypi --output json

# 3) Inspect what the baseline actually holds.
pipeline_watch baseline show --package requests
pipeline_watch baseline stats
```

Exit codes mirror pipeline-check so the two tools can share a single CI gate:

| Code | Meaning |
|------|---------|
| 0 | No findings at or above `--fail-on` severity (default: HIGH) |
| 1 | Gate failed |
| 2 | Scanner failure (registry API error, malformed manifest, etc.) |

### Baseline management

```bash
pipeline_watch baseline init --manifest requirements.txt --ecosystem pypi
pipeline_watch baseline show --package requests
pipeline_watch baseline reset --scope package:requests
pipeline_watch baseline stats
```

### Supply chain scanning

```bash
pipeline_watch scan deps --manifest requirements.txt --ecosystem pypi
pipeline_watch scan all --manifest requirements.txt --output-file findings.json
```

### Gate knobs

```bash
# Gate only on HIGH and CRITICAL (default).
pipeline_watch scan deps --manifest requirements.txt --fail-on HIGH

# Offline / rate-limited — skip GitHub and npm probes, still get the baseline-
# diff signals (SC-002, SC-004, SC-005, SC-006, SC-007).
pipeline_watch scan deps --manifest requirements.txt --no-github --no-npm
```

---

## Detection modules

### Module 1 — supply-chain

Compares the live state of every package in your manifest against its last stored snapshot. Eight signals:

| ID | Severity | Signal |
|----|----------|--------|
| **SC-001** | HIGH / MED | New maintainer with no prior commits in the source repo |
| **SC-002** | MED | Release published outside the maintainer's historical hour window (90th-percentile) |
| **SC-003** | HIGH | Release version without a matching git tag in the source repo |
| **SC-004** | HIGH / MED | `setup.py` / `__init__.py` / `pyproject.toml` install-hook added or hashed-changed |
| **SC-005** | LOW | New transitive dependency added between snapshots |
| **SC-006** | LOW | Manifest constraint loosened from pinned (`==x.y.z`) to floating (`>=x.y`) |
| **SC-007** | HIGH | Two manifest packages within Levenshtein distance ≤ 2 (typosquat pair) |
| **SC-008** | MED | Same package name newly registered on the other ecosystem within 30 days |

Data sources: `https://pypi.org/pypi/{package}/json`, `https://registry.npmjs.org/{package}`, GitHub's public REST API.

### Module 2 — ci-runtime *(schema ready; detectors land next release)*

Consumes pipeline execution logs and compares against `baseline_stats`. The database schema is already in place — `pipeline_runs` stores network destinations, secret accesses, artifact checksums, and run duration. Detectors for new-network-destination, new-secret-access, artifact-drift-under-same-config-hash, and duration-outlier arrive in Module 2.

### Module 3 — vcs-audit *(schema ready; detectors land next release)*

Consumes GitHub/GitLab audit logs (`audit_events`). Detectors for force-push-to-protected-branch, first-time-deploy-key-creator, new-actor-IP, and secret-scanning-alert-dismissal spike arrive in Module 3.

---

## Pairing with pipeline-check

Both tools emit JSON in the same envelope so a dashboard ingests both through one decoder:

```json
{
  "schema_version": "1.0",
  "tool": "pipeline-watch",
  "tool_version": "0.1.0",
  "module": "supply-chain",
  "score": { "grade": "D", "total": 8, "summary": { … } },
  "findings": [
    {
      "tool": "pipeline-watch",
      "module": "supply-chain",
      "severity": "HIGH",
      "score": "D",
      "signal": "New maintainer 'mallory' published requests 2.32.0.",
      "baseline": "Previous maintainers for requests: alice.",
      "evidence": { "package": "requests", "new_maintainer": "mallory", "has_commits_in_source_repo": false },
      "timestamp": "2026-04-20T12:00:00+00:00",
      "remediation": "Freeze the dependency and verify the maintainer addition was announced upstream.",
      "check_id": "SC-001"
    }
  ]
}
```

A worked example lives at [`findings.json`](findings.json). The `score.grade` mapping matches pipeline-check's gate contract — any `D` fails the same CI step.

Typical combined CI step:

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

## Real attacks this would have caught

| Incident | Signal that fires | Evidence in the snapshot |
|----------|-------------------|--------------------------|
| **event-stream (2018)** — attacker added as co-maintainer, shipped `flatmap-stream` with malicious `__init__.js` | **SC-001** new-maintainer-no-commits + **SC-004** install-hook hash changed | Maintainer `right9ctrl` appears; `install_script_hash` differs between the last clean release and the compromised one |
| **node-ipc (2022)** — same maintainer starts shipping politically-motivated wipe payloads | **SC-002** release outside historical hour window + **SC-004** install-hook hash changed | The maintainer's release cadence and time-of-day diverged sharply from the 90% window |
| **PyPI `ctx` / `phpass` typosquats (2022)** — attacker registered similarly-named packages, added setup.py hooks | **SC-007** typosquat distance + **SC-001** new-maintainer + **SC-004** install-hook added | Levenshtein(`ctx`, `httx`) = 1; fresh registrar; setup.py with `requests.get('/env')` appears on first snapshot |
| **`colors` / `faker` (2022)** — maintainer self-sabotages by publishing an infinite-loop release | **SC-003** release without git tag + **SC-002** off-hours release | The 3AM release was pushed to npm with no matching tag in the GitHub repo |
| **Dependency-confusion campaigns** — attacker registers your internal name on a public registry | **SC-008** cross-ecosystem new registration | `internal-utils` shows up on PyPI within 30 days of a maintainer search |

Every signal records enough evidence in `findings.json` for incident response to reconstruct the decision without re-running the tool.

---

## Compliance mapping

Each signal evidences controls in the frameworks security teams already track. Pipeline-watch does not claim to *satisfy* these controls — it supplies evidence that can be mapped into a compliance workflow.

| Signal | SLSA level | OWASP Top 10 for CI/CD (2022) | ISO 27001 Annex A (2022) |
|--------|-----------|-------------------------------|---------------------------|
| SC-001 new maintainer | L3 — provenance / contributor identity | CICD-SEC-4: Insufficient PBAC | A.5.17 — Authentication information; A.5.24 — Incident mgmt |
| SC-002 off-hours release | L2 — build integrity | CICD-SEC-5: Insufficient flow control | A.5.24 — Incident mgmt |
| SC-003 release without git tag | L3 — source integrity | CICD-SEC-6: Insufficient credential hygiene | A.5.23 — Cloud services use; A.8.30 — Outsourced development |
| SC-004 install-hook change | L2 — build integrity | CICD-SEC-3: Dependency chain abuse | A.8.28 — Secure coding |
| SC-005 new transitive dependency | L2 — dependency tracking | CICD-SEC-3: Dependency chain abuse | A.8.26 — Application security requirements |
| SC-006 constraint loosened | L1 — reproducibility | CICD-SEC-3: Dependency chain abuse | A.8.9 — Configuration management |
| SC-007 typosquat | (operator-driven) | CICD-SEC-3: Dependency chain abuse | A.5.22 — Supplier services; A.8.1 — User endpoint devices |
| SC-008 cross-ecosystem new registration | (operator-driven) | CICD-SEC-3: Dependency chain abuse | A.5.22 — Supplier services |

---

## Provider support

| Ecosystem / platform | Module | Status |
|----------------------|--------|--------|
| PyPI | supply-chain | ✅ implemented |
| npm | supply-chain | schema ready (scan side arrives with Module 1b) |
| GitHub Actions logs | ci-runtime | schema ready |
| GitLab pipelines | ci-runtime | schema ready |
| Jenkins | ci-runtime | schema ready |
| GitHub audit log | vcs-audit | schema ready |
| GitLab audit log | vcs-audit | schema ready |

"Schema ready" means the baseline database already holds the right columns and indexes — adding the detector is additive, no migration or breaking change.

---

## How it works

```
  PyPI / npm / GitHub API
           |
           v
    +---------------+
    | Provider      |   urllib — swap the fetcher for tests.
    | (pypi, npm,   |
    |  github)      |
    +-------+-------+
            |
            v
    +---------------+
    | Detector      |   Compares current observation vs. stored snapshot.
    | (supply_chain)|   Emits pipeline_watch.output.Finding per deviation.
    +-------+-------+
            |
            v
    +---------------+
    | Store         |   sqlite3.  Appends snapshots.  Refreshes baseline_stats.
    | (baseline.db) |
    +-------+-------+
            |
            v
    +---------------+
    | Formatter     |   Rich tables (terminal) + JSON (schema_version: 1.0).
    +---------------+
```

---

## Contributing

```bash
make install   # editable install + dev deps
make test      # pytest
make lint      # ruff
make type      # mypy
```

All network calls go through injectable fetchers — run `pytest` with no extras to execute the full suite without touching the real network.

---

## License

MIT — see [LICENSE](LICENSE).
